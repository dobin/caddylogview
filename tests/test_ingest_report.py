import gzip
import json
import sqlite3

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select

from caddyparser.aggregate import (
    build_windows,
    domain_summary,
    domain_time_series,
    time_series,
    top_content,
)
from caddyparser.cli import main
from caddyparser.db import create_database
from caddyparser.ingest import BATCH_SIZE, import_log
from caddyparser.models import Event
from caddyparser.report import build_report


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


def test_incremental_import_privacy_and_report(tmp_path):
    log = tmp_path / "access.log"
    log.write_bytes(make_record(1_700_000_000) + make_record(1_700_000_100, uri="/docs/b?secret=yes"))
    database = tmp_path / "stats.sqlite3"
    engine, factory = create_database(database)

    first = import_log(factory, log)
    second = import_log(factory, log)
    assert first.imported == 2
    assert second.imported == 0

    with log.open("ab") as file:
        file.write(make_record(1_700_000_200, ip="192.0.2.2", host="other.test", uri="/"))
    assert import_log(factory, log).imported == 1

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 3
        windows = build_windows(session)
        summary = domain_summary(session, windows["all"])
        assert sum(row["hits"] for row in summary) == 3
        assert summary[0]["visitors"] >= 1
        assert len(time_series(session, windows["all"])["hits"]) <= 120
        assert top_content(session, windows["all"])["sections"]["hits"][0]["hits"] == 2

    report = build_report(factory, tmp_path / "report")
    assert (report / "index.html").exists()
    assert (report / "data" / "manifest.json").exists()
    report_text = "".join(path.read_text(errors="ignore") for path in report.rglob("*") if path.is_file())
    assert "testing12" not in report_text
    assert "192.0.2.1" not in report_text
    assert "secret=yes" not in report_text

    connection = sqlite3.connect(database)
    dump = "\n".join(connection.iterdump())
    connection.close()
    assert "testing12" not in dump
    assert "192.0.2.1" not in dump
    assert "secret=yes" not in dump
    engine.dispose()


def test_incomplete_line_is_deferred(tmp_path):
    log = tmp_path / "access.log"
    complete = make_record(1_700_000_000)
    partial = make_record(1_700_000_001)[:-1]
    log.write_bytes(complete + partial)
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    result = import_log(factory, log)
    assert result.imported == 1
    assert result.incomplete
    with log.open("ab") as file:
        file.write(b"\n")
    assert import_log(factory, log).imported == 1
    engine.dispose()


def test_directory_imports_plain_and_gzip_logs_without_duplicates(tmp_path, capsys):
    logs = tmp_path / "caddy"
    logs.mkdir()
    rotated_record = make_record(1_700_000_000)
    with gzip.open(logs / "access-2026-09-21T09-15-38.580.log.gz", "wb") as file:
        file.write(rotated_record)
    (logs / "access.log").write_bytes(rotated_record + make_record(1_700_000_100))
    (logs / "README.txt").write_text("not a log")
    database = tmp_path / "stats.sqlite3"

    main(["import", str(logs), "--database", str(database)])
    output = capsys.readouterr().out
    assert "Imported 2 new records." in output

    main(["import", str(logs), "--database", str(database)])
    assert "Imported 0 new records." in capsys.readouterr().out

    engine, factory = create_database(database)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 2
    engine.dispose()


def test_large_batches_count_duplicates_exactly(tmp_path):
    log = tmp_path / "access.log"
    unique_records = [make_record(1_700_000_000 + index) for index in range(BATCH_SIZE + 5)]
    log.write_bytes(b"".join(unique_records + [unique_records[0], unique_records[-1]]))
    engine, factory = create_database(tmp_path / "stats.sqlite3")

    first = import_log(factory, log)
    assert first.read == BATCH_SIZE + 7
    assert first.imported == BATCH_SIZE + 5

    second = import_log(factory, log)
    assert second.imported == 0

    with log.open("ab") as file:
        file.write(unique_records[1])
        file.write(make_record(1_800_000_000))
    appended = import_log(factory, log)
    assert appended.read == 2
    assert appended.imported == 1

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == BATCH_SIZE + 6
    engine.dispose()


def test_grouped_domain_series_matches_individual_queries(tmp_path):
    newest = 1_700_000_000
    log = tmp_path / "access.log"
    log.write_bytes(
        make_record(newest - 90_000, host="old.test")
        + make_record(newest - 3_600, host="a.test", ip="192.0.2.1")
        + make_record(newest - 3_500, host="a.test", ip="192.0.2.2")
        + make_record(newest - 3_400, host="b.test", ip="192.0.2.1")
        + make_record(newest, host="b.test", ip="192.0.2.3")
    )
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, log)

    hosts = ["a.test", "b.test", "old.test"]
    with factory() as session:
        window = build_windows(session)["1d"]
        grouped = domain_time_series(session, window, hosts)
        assert list(grouped) == hosts
        for host in hosts:
            assert grouped[host] == time_series(session, window, host)
        assert not any(grouped["old.test"]["hits"])
    engine.dispose()


def test_top_content_limits_and_orders_both_rankings(tmp_path):
    log = tmp_path / "access.log"
    records = []
    timestamp = 1_700_000_000
    for index in range(12):
        path = f"/item-{index:02d}"
        hit_count = 24 - index
        visitor_count = index + 1
        for hit in range(hit_count):
            records.append(
                make_record(
                    timestamp + index * 100 + hit,
                    uri=path,
                    ip=f"192.0.{index}.{hit % visitor_count + 1}",
                )
            )
    log.write_bytes(b"".join(records))
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, log)

    with factory() as session:
        top = top_content(session, build_windows(session)["all"])["urls"]
        assert len(top["hits"]) == 10
        assert len(top["visitors"]) == 10
        assert [row["label"] for row in top["hits"]] == [
            f"example.test/item-{index:02d}" for index in range(10)
        ]
        assert [row["label"] for row in top["visitors"]] == [
            f"example.test/item-{index:02d}" for index in range(11, 1, -1)
        ]
    engine.dispose()


def test_optimized_aggregations_use_fixed_query_counts(tmp_path):
    log = tmp_path / "access.log"
    log.write_bytes(
        make_record(1_700_000_000, host="a.test")
        + make_record(1_700_000_100, host="b.test")
        + make_record(1_700_000_200, host="c.test")
    )
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, log)
    statements = []

    def record_select(*args):
        statement = args[2]
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", record_select)
    try:
        with factory() as session:
            window = build_windows(session)["all"]
            statements.clear()
            domain_time_series(session, window, ["a.test", "b.test", "c.test"])
            assert len(statements) == 1

            statements.clear()
            top_content(session, window)
            assert len(statements) == 3
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record_select)
        engine.dispose()


def test_ranges_keep_requested_window_when_history_is_short(tmp_path):
    log = tmp_path / "access.log"
    newest = 1_700_000_000
    log.write_bytes(make_record(newest - 3_600) + make_record(newest))
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, log)

    with factory() as session:
        windows = build_windows(session)
        assert windows["1d"].end - windows["1d"].start == 86_400
        assert windows["1m"].start < windows["1d"].start
        assert windows["6m"].start < windows["1m"].start
        assert windows["12m"].start < windows["6m"].start
        assert 20 <= len(time_series(session, windows["1d"])["hits"]) <= 25
        assert len(time_series(session, windows["1m"])["hits"]) >= 28
        assert windows["all"].start == newest - 3_600
        assert windows["all"].bucket_seconds == 3_600
    engine.dispose()


def test_ranges_filter_old_events(tmp_path):
    log = tmp_path / "access.log"
    newest = 1_700_000_000
    log.write_bytes(make_record(newest - 40 * 86_400) + make_record(newest))
    engine, factory = create_database(tmp_path / "stats.sqlite3")
    import_log(factory, log)

    with factory() as session:
        windows = build_windows(session)
        assert sum(row["hits"] for row in domain_summary(session, windows["1d"])) == 1
        assert sum(row["hits"] for row in domain_summary(session, windows["1m"])) == 1
        assert sum(row["hits"] for row in domain_summary(session, windows["6m"])) == 2
        assert sum(row["hits"] for row in domain_summary(session, windows["all"])) == 2
    engine.dispose()