from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .hll import HyperLogLog
from .models import AggregateBounds, HourlyAggregate, HourlyReferrerAggregate

RANGES = ("1d", "1m", "6m", "12m", "all")
RANGE_LABELS = {
    "1d": "1 day",
    "1m": "1 month",
    "6m": "6 months",
    "12m": "12 months",
    "all": "All",
}


@dataclass(frozen=True, slots=True)
class Window:
    key: str
    start: float
    end: float
    bucket_seconds: int


def _subtract_months(timestamp: float, months: int) -> float:
    value = datetime.fromtimestamp(timestamp, timezone.utc)
    index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day).timestamp()


def _floor_hour(timestamp: float) -> int:
    return int(timestamp // 3_600) * 3_600


def build_windows(session: Session) -> dict[str, Window]:
    bounds = session.get(AggregateBounds, 1)
    if bounds is None:
        return {}
    minimum = _floor_hour(bounds.minimum_timestamp)
    end = _floor_hour(bounds.maximum_timestamp) + 3_600
    starts = {
        "1d": end - 86_400,
        "1m": _floor_hour(_subtract_months(end, 1)),
        "6m": _floor_hour(_subtract_months(end, 6)),
        "12m": _floor_hour(_subtract_months(end, 12)),
        "all": float(minimum),
    }
    buckets = {"1d": 3_600, "1m": 86_400, "6m": 604_800, "12m": 604_800}
    span = end - minimum
    if span <= 5 * 86_400:
        buckets["all"] = 3_600
    elif span <= 120 * 86_400:
        buckets["all"] = 86_400
    elif span <= 120 * 604_800:
        buckets["all"] = 604_800
    elif span <= 120 * 2_678_400:
        buckets["all"] = 2_678_400
    else:
        buckets["all"] = 31_536_000
    return {key: Window(key, starts[key], float(end), buckets[key]) for key in RANGES}


def _rows(session: Session, window: Window) -> list[HourlyAggregate]:
    return list(
        session.scalars(
            select(HourlyAggregate).where(
                HourlyAggregate.hour_start >= window.start,
                HourlyAggregate.hour_start < window.end,
            )
        )
    )


def _merge(target: HyperLogLog, blob: bytes) -> None:
    target.merge(HyperLogLog.from_bytes(blob))


def domain_summary(session: Session, window: Window) -> list[dict[str, Any]]:
    grouped: dict[str, tuple[int, int, HyperLogLog]] = {}
    for row in _rows(session, window):
        hits, byte_count, visitors = grouped.setdefault(row.host, (0, 0, HyperLogLog()))
        _merge(visitors, row.visitor_hll)
        grouped[row.host] = (hits + row.hits, byte_count + row.bytes_sent, visitors)
    return sorted(
        (
            {"domain": host, "hits": hits, "visitors": visitors.count(), "bytes": byte_count}
            for host, (hits, byte_count, visitors) in grouped.items()
        ),
        key=lambda row: (-row["hits"], row["domain"]),
    )


def top_content(session: Session, window: Window) -> dict[str, dict[str, list[dict[str, Any]]]]:
    rows = _rows(session, window)
    dimensions: dict[str, dict[str, tuple[int, HyperLogLog]]] = {"domains": {}, "sections": {}}
    for row in rows:
        for name, label in (("domains", row.host), ("sections", row.host + row.first_path)):
            hits, visitors = dimensions[name].setdefault(label, (0, HyperLogLog()))
            _merge(visitors, row.visitor_hll)
            dimensions[name][label] = (hits + row.hits, visitors)

    output: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, grouped in dimensions.items():
        values = [
            {"label": label, "hits": hits, "visitors": visitors.count()}
            for label, (hits, visitors) in grouped.items()
        ]
        output[name] = {
            metric: sorted(values, key=lambda row: (-row[metric], row["label"]))[:10]
            for metric in ("hits", "visitors")
        }
    return output


def top_referrers(
    session: Session, window: Window, *, site_domain: str
) -> list[dict[str, Any]]:
    grouped: dict[str, int] = {}
    rows = session.scalars(
        select(HourlyReferrerAggregate).where(
            HourlyReferrerAggregate.hour_start >= window.start,
            HourlyReferrerAggregate.hour_start < window.end,
        )
    )
    for row in rows:
        if row.referrer_host == site_domain or row.referrer_host.endswith(f".{site_domain}"):
            continue
        grouped[row.referrer_host] = grouped.get(row.referrer_host, 0) + row.hits
    return [
        {"referrer": referrer, "hits": hits}
        for referrer, hits in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))[:20]
    ]


def _empty_series(window: Window) -> dict[str, list[Any]]:
    count = max(1, math.ceil((window.end - window.start) / window.bucket_seconds))
    labels = []
    for index in range(count):
        timestamp = window.start + index * window.bucket_seconds
        value = datetime.fromtimestamp(timestamp, timezone.utc)
        labels.append(value.strftime("%Y-%m-%d %H:%M" if window.bucket_seconds < 86_400 else "%Y-%m-%d"))
    return {"labels": labels, "hits": [0] * count, "visitors": [0] * count}


def domain_time_series(
    session: Session, window: Window, hosts: list[str]
) -> dict[str, dict[str, list[Any]]]:
    output = {host: _empty_series(window) for host in hosts}
    sketches = {
        host: [HyperLogLog() for _ in output[host]["hits"]]
        for host in hosts
    }
    all_series = _empty_series(window)
    all_sketches = [HyperLogLog() for _ in all_series["hits"]]
    for row in _rows(session, window):
        index = int((row.hour_start - window.start) // window.bucket_seconds)
        if index < 0 or index >= len(all_series["hits"]):
            continue
        all_series["hits"][index] += row.hits
        _merge(all_sketches[index], row.visitor_hll)
        if row.host in output:
            output[row.host]["hits"][index] += row.hits
            _merge(sketches[row.host][index], row.visitor_hll)
    all_series["visitors"] = [sketch.count() for sketch in all_sketches]
    for host in hosts:
        output[host]["visitors"] = [sketch.count() for sketch in sketches[host]]
    return {"all": all_series, **output}


def time_series(
    session: Session, window: Window, host: str | None = None
) -> dict[str, list[Any]]:
    if host is None:
        return domain_time_series(session, window, [])["all"]
    return domain_time_series(session, window, [host])[host]
