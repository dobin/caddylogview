import gzip
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from caddylogview.aggregate import build_windows, domain_summary, time_series, top_content
from caddylogview.cli import main
from caddylogview.db import create_database
from caddylogview.ingest import discover_logs, import_log
from caddylogview.models import ConsumedFile, HourlyAggregate
from caddylogview.parser import ParseError
from caddylogview.report import build_report


def make_record(ts, ip="192.0.2.1", host="example.test", uri="/docs/a", size=100):
    return json.dumps(
        {
            "ts": ts,
            "request": {
                "remote_ip": ip,
                "host": host,
                "uri": uri,
                "headers": {"X-Auth-Password": ["testing12"]},
            },
            "status": 200,
            "size": size,
        }
    ).encode() + b"\n"


def write_rotation(path: Path, records: list[bytes]) -> None:
    with gzip.GzipFile(filename=path, mode="wb") as file:
        file.write(b"".join(records))


def test_discovers_only_completed_gzip_rotations(tmp_path):
    write_rotation(tmp_path / "access-1.log.gz", [])
    (tmp_path / "access.log").write_bytes(b"")
    (tmp_path / "access-2.log").write_bytes(b"")
    assert [path.name for path in discover_logs(tmp_path)] == ["access-1.log.gz"]
    with pytest.raises(ValueError):
        discover_logs(tmp_path / "access.log")


def test_hourly_aggregation_idempotence_privacy_and_report(tmp_path):
    newest = 1_700_010_000
    rotation = tmp_path / "access-1.log.gz"
    records = [
        make_record(newest - 4_000, uri="/docs/a?secret=yes", size=20),
        make_record(newest - 3_900, uri="/docs/b", size=30),
        make_record(newest, ip="192.0.2.2", host="other.test", uri="/", size=50),
    ]
    write_rotation(rotation, records)
    database = tmp_path / "stats.sqlite3"
    engine, factory = create_database(database)

    first = import_log(factory, rotation)
    second = import_log(factory, rotation)
    assert (first.imported, first.consumed) == (3, True)
    assert (second.imported, second.duplicate) == (0, True)

    renamed = tmp_path / "renamed.log.gz"
    write_rotation(renamed, records)
    assert import_log(factory, renamed).duplicate

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumedFile)) == 1
        assert session.scalar(select(func.sum(HourlyAggregate.hits))) == 3
        assert session.scalar(select(func.sum(HourlyAggregate.bytes_sent))) == 100
        windows = build_windows(session)
        summary = domain_summary(session, windows["all"])
        assert sum(row["hits"] for row in summary) == 3
        assert summary[0]["visitors"] >= 1
        assert set(top_content(session, windows["all"])) == {"domains", "sections"}
        assert sum(time_series(session, windows["all"])["hits"]) == 3

    report = build_report(factory, tmp_path / "report")
    manifest = json.loads((report / "data" / "manifest.json").read_text())
    assert set(manifest["summaries"]) == {"1d", "1m", "6m", "12m", "all"}
    report_text = "".join(path.read_text(errors="ignore") for path in report.rglob("*") if path.is_file())
    for secret in ("testing12", "192.0.2.1", "secret=yes", "/docs/a"):
        assert secret not in report_text
    assert "Full URLs" not in report_text
    assert "Est. visitors" in report_text

    connection = sqlite3.connect(database)
    dump = "\n".join(connection.iterdump())
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert "events" not in tables
    for secret in ("testing12", "192.0.2.1", "secret=yes", "/docs/a"):
        assert secret not in dump
    engine.dispose()


def test_duplicate_lines_count_but_duplicate_rotation_does_not(tmp_path):
    record = make_record(1_700_000_000)
    rotation = tmp_path / "access-1.log.gz"
    write_rotation(rotation, [record, record])
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    assert import_log(factory, rotation).imported == 2
    assert import_log(factory, rotation).imported == 0
    with factory() as session:
        assert session.scalar(select(func.sum(HourlyAggregate.hits))) == 2
    engine.dispose()


def test_malformed_non_strict_is_consumed_but_strict_rolls_back(tmp_path):
    rotation = tmp_path / "access-1.log.gz"
    write_rotation(rotation, [make_record(1_700_000_000), b"not json\n"])

    engine, factory = create_database(tmp_path / "non-strict.sqlite3")
    result = import_log(factory, rotation)
    assert (result.imported, result.errors, result.consumed) == (1, 1, True)
    engine.dispose()

    engine, factory = create_database(tmp_path / "strict.sqlite3")
    with pytest.raises(ParseError):
        import_log(factory, rotation, strict=True)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumedFile)) == 0
        assert session.scalar(select(func.count()).select_from(HourlyAggregate)) == 0
    engine.dispose()


def test_corrupt_gzip_and_empty_rotation(tmp_path):
    corrupt = tmp_path / "corrupt.log.gz"
    write_rotation(corrupt, [make_record(1_700_000_000)])
    corrupt.write_bytes(corrupt.read_bytes()[:-4])
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    with pytest.raises((EOFError, OSError)):
        import_log(factory, corrupt)

    empty = tmp_path / "empty.log.gz"
    write_rotation(empty, [])
    result = import_log(factory, empty)
    assert result.consumed and result.imported == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumedFile)) == 1
        assert build_windows(session) == {}
    engine.dispose()


def test_hour_aligned_ranges_and_section_visitor_union(tmp_path):
    newest = 1_700_000_000
    rotation = tmp_path / "access-1.log.gz"
    write_rotation(
        rotation,
        [
            make_record(newest - 40 * 86_400, uri="/old", ip="192.0.2.1"),
            make_record(newest - 3_600, uri="/docs/a", ip="192.0.2.1"),
            make_record(newest, uri="/api/a", ip="192.0.2.1"),
        ],
    )
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, rotation)
    with factory() as session:
        windows = build_windows(session)
        assert windows["1d"].end % 3_600 == 0
        assert windows["1d"].start % 3_600 == 0
        assert sum(row["hits"] for row in domain_summary(session, windows["1d"])) == 2
        assert domain_summary(session, windows["1d"])[0]["visitors"] == 1
        assert sum(row["hits"] for row in domain_summary(session, windows["6m"])) == 3
    engine.dispose()


def test_cli_imports_directory_once(tmp_path, capsys):
    logs = tmp_path / "caddy"
    logs.mkdir()
    write_rotation(logs / "access-1.log.gz", [make_record(1_700_000_000)])
    (logs / "access.log").write_bytes(make_record(1_700_000_001))
    database = tmp_path / "stats.sqlite3"
    main(["import", str(logs), "--database", str(database)])
    assert "Imported 1 new records." in capsys.readouterr().out
    main(["import", str(logs), "--database", str(database)])
    output = capsys.readouterr().out
    assert "already consumed" in output
    assert "Imported 0 new records." in output
