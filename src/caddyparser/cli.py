from __future__ import annotations

import argparse
from pathlib import Path

from .db import create_database
from .ingest import discover_logs, import_log
from .report import build_report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="caddyparser", description="Build compact analytics from Caddy JSON access logs."
    )
    commands = root.add_subparsers(dest="command", required=True)

    def database_option(command: argparse.ArgumentParser) -> None:
        command.add_argument("--database", default="caddyparser.sqlite3", help="SQLite cache path")

    importing = commands.add_parser("import", help="Incrementally import access logs")
    importing.add_argument("logs", nargs="+", type=Path)
    importing.add_argument("--strict", action="store_true", help="Stop on the first malformed record")
    database_option(importing)

    building = commands.add_parser("build", help="Generate the static report")
    building.add_argument("--output", default="report", help="Output directory")
    database_option(building)

    updating = commands.add_parser("update", help="Import logs and generate the report")
    updating.add_argument("logs", nargs="+", type=Path)
    updating.add_argument("--strict", action="store_true", help="Stop on the first malformed record")
    updating.add_argument("--output", default="report", help="Output directory")
    database_option(updating)
    return root


def main(argv: list[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    engine, factory = create_database(arguments.database)
    try:
        if arguments.command in {"import", "update"}:
            total = 0
            logs = []
            seen = set()
            for path in arguments.logs:
                for log in discover_logs(path):
                    if log not in seen:
                        logs.append(log)
                        seen.add(log)
            for log in logs:
                result = import_log(factory, log, strict=arguments.strict)
                total += result.imported
                suffix = "; incomplete final line deferred" if result.incomplete else ""
                print(
                    f"{result.path}: imported {result.imported}/{result.read} records, "
                    f"{result.errors} errors{suffix}"
                )
            print(f"Imported {total} new records.")
        if arguments.command in {"build", "update"}:
            output = build_report(factory, arguments.output)
            print(f"Report written to {output}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()