from __future__ import annotations

import gzip
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from .db import visitor_key
from .hll import HyperLogLog
from .models import AggregateBounds, ConsumedFile, HourlyAggregate, HourlyReferrerAggregate
from .parser import ParseError, parse_line

LOG_PATTERN = "*.log.gz"


@dataclass(slots=True)
class ImportResult:
    path: Path
    read: int = 0
    imported: int = 0
    errors: int = 0
    consumed: bool = False
    duplicate: bool = False


@dataclass(slots=True)
class _Delta:
    hits: int
    bytes_sent: int
    visitors: HyperLogLog


def discover_logs(path: str | Path) -> list[Path]:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        if not source.name.endswith(".log.gz"):
            raise ValueError(f"Only completed .log.gz rotations are supported: {source}")
        return [source]
    return sorted(candidate.resolve() for candidate in source.glob(LOG_PATTERN) if candidate.is_file())


def import_log(
    factory: sessionmaker[Session], path: str | Path, *, strict: bool = False
) -> ImportResult:
    source = Path(path).expanduser().resolve()
    if not source.name.endswith(".log.gz"):
        raise ValueError(f"Only completed .log.gz rotations are supported: {source}")
    result = ImportResult(path=source)
    stat = source.stat()
    digest = hashlib.sha256()
    deltas: dict[tuple[int, str, str], _Delta] = {}
    referrer_deltas: dict[tuple[int, str, str], int] = {}
    minimum: float | None = None
    maximum: float | None = None

    with factory() as session:
        key = visitor_key(session)

    with gzip.open(source, "rb") as file:
        while line := file.readline():
            digest.update(line)
            result.read += 1
            try:
                event = parse_line(line, key)
            except ParseError:
                result.errors += 1
                if strict:
                    raise
                continue
            hour_start = int(event.timestamp // 3_600) * 3_600
            aggregate_key = (hour_start, event.host, event.first_path)
            delta = deltas.get(aggregate_key)
            if delta is None:
                delta = _Delta(0, 0, HyperLogLog())
                deltas[aggregate_key] = delta
            delta.hits += 1
            delta.bytes_sent += event.bytes_sent
            delta.visitors.add(event.visitor)
            if event.referrer_host is not None:
                referrer_key = (hour_start, event.host, event.referrer_host)
                referrer_deltas[referrer_key] = referrer_deltas.get(referrer_key, 0) + 1
            minimum = event.timestamp if minimum is None else min(minimum, event.timestamp)
            maximum = event.timestamp if maximum is None else max(maximum, event.timestamp)

    content_sha256 = digest.digest()
    with factory.begin() as session:
        claimed = session.execute(
            insert(ConsumedFile)
            .values(
                content_sha256=content_sha256,
                path=str(source),
                compressed_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                processed_at=time.time(),
                records_read=result.read,
                records_imported=result.read - result.errors,
                errors=result.errors,
            )
            .on_conflict_do_nothing(index_elements=[ConsumedFile.content_sha256])
            .returning(ConsumedFile.content_sha256)
        ).scalar_one_or_none()
        if claimed is None:
            result.duplicate = True
            return result

        for (hour_start, host, first_path), delta in deltas.items():
            statement = insert(HourlyAggregate).values(
                hour_start=hour_start,
                host=host,
                first_path=first_path,
                hits=delta.hits,
                bytes_sent=delta.bytes_sent,
                visitor_hll=delta.visitors.to_bytes(),
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    HourlyAggregate.hour_start,
                    HourlyAggregate.host,
                    HourlyAggregate.first_path,
                ],
                set_={
                    "hits": HourlyAggregate.hits + statement.excluded.hits,
                    "bytes_sent": HourlyAggregate.bytes_sent + statement.excluded.bytes_sent,
                    "visitor_hll": func.hll_union(
                        HourlyAggregate.visitor_hll, statement.excluded.visitor_hll
                    ),
                },
            )
            session.execute(statement)

        for (hour_start, host, referrer_host), hits in referrer_deltas.items():
            statement = insert(HourlyReferrerAggregate).values(
                hour_start=hour_start,
                host=host,
                referrer_host=referrer_host,
                hits=hits,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        HourlyReferrerAggregate.hour_start,
                        HourlyReferrerAggregate.host,
                        HourlyReferrerAggregate.referrer_host,
                    ],
                    set_={"hits": HourlyReferrerAggregate.hits + statement.excluded.hits},
                )
            )

        if minimum is not None and maximum is not None:
            statement = insert(AggregateBounds).values(
                id=1, minimum_timestamp=minimum, maximum_timestamp=maximum
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AggregateBounds.id],
                    set_={
                        "minimum_timestamp": func.min(
                            AggregateBounds.minimum_timestamp, statement.excluded.minimum_timestamp
                        ),
                        "maximum_timestamp": func.max(
                            AggregateBounds.maximum_timestamp, statement.excluded.maximum_timestamp
                        ),
                    },
                )
            )

    result.imported = result.read - result.errors
    result.consumed = True
    return result
