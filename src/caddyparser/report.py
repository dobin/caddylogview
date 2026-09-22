from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .aggregate import RANGE_LABELS, RANGES, build_windows, domain_summary, time_series, top_content
from .models import Event

PACKAGE = Path(__file__).parent


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build_report(factory: sessionmaker[Session], output: str | Path) -> Path:
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        data_dir = temporary / "data"
        with factory() as session:
            windows = build_windows(session)
            data_bounds = session.execute(
                select(func.min(Event.timestamp), func.max(Event.timestamp))
            ).one()
            hosts = list(session.scalars(select(Event.host).distinct().order_by(Event.host)))
            domain_ids = {
                host: "d-" + hashlib.sha256(host.encode()).hexdigest()[:12] for host in hosts
            }
            manifest: dict[str, object] = {
                "domains": [{"id": domain_ids[h], "name": h} for h in hosts],
                "summaries": {},
                "series": {},
            }
            summaries: dict[str, str] = manifest["summaries"]  # type: ignore[assignment]
            series_map: dict[str, dict[str, str]] = manifest["series"]  # type: ignore[assignment]
            for key in RANGES:
                if key not in windows:
                    continue
                window = windows[key]
                summary_file = f"data/{key}.json"
                summaries[key] = summary_file
                _write_json(
                    temporary / summary_file,
                    {
                        "window": {
                            "start": window.start,
                            "end": window.end,
                            "data_start": data_bounds[0],
                            "data_end": data_bounds[1],
                        },
                        "domains": domain_summary(session, window),
                        "top": top_content(session, window),
                    },
                )
                files = {"all": f"data/series/{key}-all.json"}
                _write_json(temporary / files["all"], time_series(session, window))
                for host in hosts:
                    files[domain_ids[host]] = f"data/series/{key}-{domain_ids[host]}.json"
                    _write_json(temporary / files[domain_ids[host]], time_series(session, window, host))
                series_map[key] = files
        _write_json(data_dir / "manifest.json", manifest)
        environment = Environment(loader=FileSystemLoader(PACKAGE / "templates"), autoescape=select_autoescape())
        html = environment.get_template("index.html.j2").render(
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            ranges=RANGE_LABELS,
            default_range="1d",
        )
        (temporary / "index.html").write_text(html, encoding="utf-8")
        shutil.copy2(PACKAGE / "web" / "style.css", temporary / "style.css")
        shutil.copy2(PACKAGE / "web" / "app.js", temporary / "app.js")
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise