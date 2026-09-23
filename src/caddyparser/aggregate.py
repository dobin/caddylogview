from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Select, distinct, func, or_, select
from sqlalchemy.orm import Session

from .models import Event

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


def build_windows(session: Session) -> dict[str, Window]:
    bounds = session.execute(select(func.min(Event.timestamp), func.max(Event.timestamp))).one()
    minimum, maximum = bounds
    if minimum is None or maximum is None:
        return {}
    end = math.floor(float(maximum)) + 1.0
    starts = {
        "1d": end - 86_400,
        "1m": _subtract_months(end, 1),
        "6m": _subtract_months(end, 6),
        "12m": _subtract_months(end, 12),
        "all": float(minimum),
    }
    buckets = {"1d": 3_600, "1m": 86_400, "6m": 604_800, "12m": 604_800}
    span = end - float(minimum)
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
    return {key: Window(key, starts[key], end, buckets[key]) for key in RANGES}


def _windowed(statement: Select[Any], window: Window) -> Select[Any]:
    return statement.where(
        Event.timestamp >= window.start,
        Event.timestamp < window.end,
    )


def domain_summary(session: Session, window: Window) -> list[dict[str, Any]]:
    statement = _windowed(
        select(
            Event.host,
            func.count().label("hits"),
            func.count(distinct(Event.visitor)).label("visitors"),
            func.coalesce(func.sum(Event.bytes_sent), 0).label("bytes"),
        ).group_by(Event.host),
        window,
    ).order_by(func.count().desc(), Event.host)
    return [
        {"domain": host, "hits": hits, "visitors": visitors, "bytes": byte_count}
        for host, hits, visitors, byte_count in session.execute(statement)
    ]


def top_content(session: Session, window: Window) -> dict[str, dict[str, list[dict[str, Any]]]]:
    dimensions = {
        "domains": (Event.host, Event.host),
        "sections": (Event.host, Event.first_path),
        "urls": (Event.host, Event.path),
    }
    output: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, columns in dimensions.items():
        if name == "domains":
            label = Event.host
            group_columns = (Event.host,)
        else:
            label = Event.host + columns[1]
            group_columns = columns
        grouped = _windowed(
            select(
                label.label("label"),
                func.count().label("hits"),
                func.count(distinct(Event.visitor)).label("visitors"),
            ).group_by(*group_columns),
            window,
        ).cte()
        ranked = select(
            grouped,
            func.row_number()
            .over(order_by=(grouped.c.hits.desc(), grouped.c.label))
            .label("hit_rank"),
            func.row_number()
            .over(order_by=(grouped.c.visitors.desc(), grouped.c.label))
            .label("visitor_rank"),
        ).subquery()
        statement = select(ranked).where(
            or_(ranked.c.hit_rank <= 10, ranked.c.visitor_rank <= 10)
        )
        rows = list(session.execute(statement))
        output[name] = {
            "hits": [
                {"label": row.label, "hits": row.hits, "visitors": row.visitors}
                for row in sorted(
                    (row for row in rows if row.hit_rank <= 10),
                    key=lambda row: row.hit_rank,
                )
            ],
            "visitors": [
                {"label": row.label, "hits": row.hits, "visitors": row.visitors}
                for row in sorted(
                    (row for row in rows if row.visitor_rank <= 10),
                    key=lambda row: row.visitor_rank,
                )
            ],
        }
    return output


def time_series(
    session: Session, window: Window, host: str | None = None
) -> dict[str, list[Any]]:
    bucket = func.floor((Event.timestamp - window.start) / window.bucket_seconds)
    statement = _windowed(
        select(
            bucket.label("bucket"),
            func.count().label("hits"),
            func.count(distinct(Event.visitor)).label("visitors"),
        ).group_by(bucket),
        window,
    )
    if host is not None:
        statement = statement.where(Event.host == host)
    rows = {int(row.bucket): (row.hits, row.visitors) for row in session.execute(statement)}
    count = max(1, math.ceil((window.end - window.start) / window.bucket_seconds))
    labels: list[str] = []
    hits: list[int] = []
    visitors: list[int] = []
    for index in range(count):
        timestamp = window.start + index * window.bucket_seconds
        value = datetime.fromtimestamp(timestamp, timezone.utc)
        labels.append(value.strftime("%Y-%m-%d %H:%M" if window.bucket_seconds < 86_400 else "%Y-%m-%d"))
        row_hits, row_visitors = rows.get(index, (0, 0))
        hits.append(row_hits)
        visitors.append(row_visitors)
    return {"labels": labels, "hits": hits, "visitors": visitors}


def domain_time_series(
    session: Session, window: Window, hosts: Iterable[str]
) -> dict[str, dict[str, list[Any]]]:
    host_list = list(hosts)
    bucket_count = max(1, math.ceil((window.end - window.start) / window.bucket_seconds))
    labels = [
        datetime.fromtimestamp(
            window.start + index * window.bucket_seconds, timezone.utc
        ).strftime("%Y-%m-%d %H:%M" if window.bucket_seconds < 86_400 else "%Y-%m-%d")
        for index in range(bucket_count)
    ]
    output = {
        host: {
            "labels": labels.copy(),
            "hits": [0] * bucket_count,
            "visitors": [0] * bucket_count,
        }
        for host in host_list
    }
    if not host_list:
        return output

    bucket = func.floor((Event.timestamp - window.start) / window.bucket_seconds)
    statement = _windowed(
        select(
            Event.host,
            bucket.label("bucket"),
            func.count().label("hits"),
            func.count(distinct(Event.visitor)).label("visitors"),
        ).group_by(Event.host, bucket),
        window,
    ).where(Event.host.in_(host_list))
    for row in session.execute(statement):
        index = int(row.bucket)
        output[row.host]["hits"][index] = row.hits
        output[row.host]["visitors"][index] = row.visitors
    return output