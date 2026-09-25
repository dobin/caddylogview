from __future__ import annotations

import argparse
from pathlib import Path

from .db import create_database
from .ingest import discover_logs, import_log
from .report import build_report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="caddylogview", description="Build compact analytics from Caddy JSON access logs."
    )
    commands = root.add_subparsers(dest="command", required=True)

    def database_option(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--database", default="caddylogview.sqlite3", help="SQLite aggregate/state path"
        )

    importing = commands.add_parser("import", help="Import completed .log.gz rotations")
    importing.add_argument("logs", nargs="+", type=Path)
    importing.add_argument("--strict", action="store_true", help="Stop on the first malformed record")
    database_option(importing)

    building = commands.add_parser("build", help="Generate the static report")
    building.add_argument("--output", default="report", help="Output directory")
    building.add_argument(
        "--site-domain", default="r00ted.ch", help="First-party domain to exclude from referrers"
    )
    database_option(building)

    updating = commands.add_parser("update", help="Import completed rotations and generate the report")
    updating.add_argument("logs", nargs="+", type=Path)
    updating.add_argument("--strict", action="store_true", help="Stop on the first malformed record")
    updating.add_argument("--output", default="report", help="Output directory")
    updating.add_argument(
        "--site-domain", default="r00ted.ch", help="First-party domain to exclude from referrers"
    )
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
                if result.duplicate:
                    print(f"{result.path}: already consumed ({result.read} records)")
                else:
                    print(
                        f"{result.path}: aggregated {result.imported}/{result.read} records, "
                        f"{result.errors} errors"
                    )
            print(f"Imported {total} new records.")
        if arguments.command in {"build", "update"}:
            output = build_report(factory, arguments.output, site_domain=arguments.site_domain)
            print(f"Report written to {output}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()