from __future__ import annotations

import secrets
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from .hll import union_bytes
from .models import Base, Metadata

SCHEMA_VERSION = "4"


def _check_schema(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    if not tables or "metadata" not in tables:
        return
    with engine.connect() as connection:
        version = connection.execute(
            select(Metadata.value).where(Metadata.key == "schema_version")
        ).scalar_one_or_none()
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported database schema {version or 'unknown'}; expected {SCHEMA_VERSION}. "
            "Move or delete the old database and replay retained .log.gz rotations."
        )


def create_database(path: str | Path) -> tuple[Engine, sessionmaker[Session]]:
    database = Path(path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database}")

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
        dbapi_connection.create_function("hll_union", 2, union_bytes, deterministic=True)

    _check_schema(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        version = session.get(Metadata, "schema_version")
        if version is None:
            session.add(Metadata(key="schema_version", value=SCHEMA_VERSION))
        if session.get(Metadata, "visitor_key") is None:
            session.add(Metadata(key="visitor_key", value=secrets.token_hex(32)))
    return engine, factory


def visitor_key(session: Session) -> bytes:
    value = session.scalar(select(Metadata.value).where(Metadata.key == "visitor_key"))
    if value is None:
        raise RuntimeError("Database visitor key is missing")
    return bytes.fromhex(value)