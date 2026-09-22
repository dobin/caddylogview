from __future__ import annotations

import hashlib
import gzip
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from .db import visitor_key
from .models import Event, ImportState
from .parser import ParseError, parse_line

PREFIX_SIZE = 4096
BATCH_SIZE = 1000
LOG_PATTERNS = ("*.log", "*.log.gz")


@dataclass(slots=True)
class ImportResult:
    path: Path
    read: int = 0
    imported: int = 0
    errors: int = 0
    incomplete: bool = False


def _prefix_hash(file) -> str:  # type: ignore[no-untyped-def]
    position = file.tell()
    file.seek(0)
    digest = hashlib.sha256(file.readline(PREFIX_SIZE)).hexdigest()
    file.seek(position)
    return digest


def discover_logs(path: str | Path) -> list[Path]:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        return [source]
    files = {
        candidate.resolve()
        for pattern in LOG_PATTERNS
        for candidate in source.glob(pattern)
        if candidate.is_file()
    }
    return sorted(files, key=lambda candidate: (candidate.name == "access.log", candidate.name))


def _open_log(source: Path):  # type: ignore[no-untyped-def]
    if source.suffix == ".gz":
        return gzip.open(source, "rb")
    return source.open("rb")


def import_log(
    factory: sessionmaker[Session], path: str | Path, *, strict: bool = False
) -> ImportResult:
    source = Path(path).expanduser().resolve()
    result = ImportResult(path=source)
    compressed = source.suffix == ".gz"
    with _open_log(source) as file:
        stat = os.fstat(file.fileno())
        prefix = _prefix_hash(file)

        with factory() as session:
            state = session.get(ImportState, str(source))
            resume = bool(
                state
                and state.device == stat.st_dev
                and state.inode == stat.st_ino
                and (compressed or stat.st_size >= state.offset)
                and state.prefix_hash == prefix
            )
            start_offset = state.offset if resume and state else 0
            key = visitor_key(session)

        file.seek(start_offset)
        batch: list[dict[str, object]] = []
        committed_offset = start_offset

        while True:
            line_start = file.tell()
            line = file.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                result.incomplete = True
                file.seek(line_start)
                break
            result.read += 1
            try:
                event = parse_line(line, key)
            except ParseError:
                result.errors += 1
                if strict:
                    raise
            else:
                batch.append(
                    {
                        "timestamp": event.timestamp,
                        "host": event.host,
                        "path": event.path,
                        "first_path": event.first_path,
                        "status": event.status,
                        "bytes_sent": event.bytes_sent,
                        "visitor": event.visitor,
                        "record_key": hmac.digest(key, line, "sha256"),
                    }
                )
            committed_offset = file.tell()
            if len(batch) >= BATCH_SIZE:
                result.imported += _commit_batch(
                    factory, batch, source, stat, prefix, committed_offset
                )
                batch.clear()

        result.imported += _commit_batch(factory, batch, source, stat, prefix, committed_offset)
    return result


def _commit_batch(
    factory: sessionmaker[Session],
    batch: list[dict[str, object]],
    source: Path,
    stat: os.stat_result,
    prefix: str,
    offset: int,
) -> int:
    with factory.begin() as session:
        imported = 0
        if batch:
            statement = insert(Event).values(batch).on_conflict_do_nothing().returning(Event.id)
            imported = len(session.scalars(statement).all())
        state = session.get(ImportState, str(source))
        values = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "offset": offset,
            "prefix_hash": prefix,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if state is None:
            session.add(ImportState(path=str(source), **values))
        else:
            for name, value in values.items():
                setattr(state, name, value)
        return imported