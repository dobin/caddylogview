from __future__ import annotations

import secrets
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Metadata

SCHEMA_VERSION = "2"


def _migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    if "events" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("events")}
    if "record_key" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE events ADD COLUMN record_key BLOB"))
            connection.execute(
                text("CREATE UNIQUE INDEX ix_events_record_key ON events (record_key)")
            )
            connection.execute(
                text("UPDATE metadata SET value = '2' WHERE key = 'schema_version'")
            )


def create_database(path: str | Path) -> tuple[Engine, sessionmaker[Session]]:
    database = Path(path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database}")

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    _migrate(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        version = session.get(Metadata, "schema_version")
        if version is None:
            session.add(Metadata(key="schema_version", value=SCHEMA_VERSION))
        elif version.value != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported database schema {version.value}; expected {SCHEMA_VERSION}"
            )
        if session.get(Metadata, "visitor_key") is None:
            session.add(Metadata(key="visitor_key", value=secrets.token_hex(32)))
    return engine, factory


def visitor_key(session: Session) -> bytes:
    value = session.scalar(select(Metadata.value).where(Metadata.key == "visitor_key"))
    if value is None:
        raise RuntimeError("Database visitor key is missing")
    return bytes.fromhex(value)