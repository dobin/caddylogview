from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Index, Integer, LargeBinary, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class HourlyAggregate(Base):
    __tablename__ = "hourly_aggregates"

    hour_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String, primary_key=True)
    first_path: Mapped[str] = mapped_column(String, primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    visitor_hll: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        CheckConstraint("hits >= 0", name="ck_hourly_aggregates_hits"),
        CheckConstraint("bytes_sent >= 0", name="ck_hourly_aggregates_bytes"),
        Index("ix_hourly_aggregates_host_hour", "host", "hour_start"),
    )


class ConsumedFile(Base):
    __tablename__ = "consumed_files"

    content_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    path: Mapped[str] = mapped_column(String, nullable=False)
    compressed_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[float] = mapped_column(Float, nullable=False)
    records_read: Mapped[int] = mapped_column(Integer, nullable=False)
    records_imported: Mapped[int] = mapped_column(Integer, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, nullable=False)


class AggregateBounds(Base):
    __tablename__ = "aggregate_bounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    minimum_timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    maximum_timestamp: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (CheckConstraint("id = 1", name="ck_aggregate_bounds_singleton"),)


class Metadata(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)