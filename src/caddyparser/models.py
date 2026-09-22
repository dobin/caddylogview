from __future__ import annotations

from sqlalchemy import Float, Index, Integer, LargeBinary, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    first_path: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    visitor: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    record_key: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)

    __table_args__ = (
        Index("ix_events_timestamp", "timestamp"),
        Index("ix_events_host_timestamp", "host", "timestamp"),
        Index("ix_events_first_path_timestamp", "first_path", "timestamp"),
        Index("ix_events_path_timestamp", "path", "timestamp"),
    )


class ImportState(Base):
    __tablename__ = "import_states"

    path: Mapped[str] = mapped_column(String, primary_key=True)
    device: Mapped[int] = mapped_column(Integer, nullable=False)
    inode: Mapped[int] = mapped_column(Integer, nullable=False)
    offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prefix_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Metadata(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)