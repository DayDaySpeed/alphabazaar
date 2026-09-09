"""Render a Report to a single self-contained HTML file (+ a structured JSON sidecar)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import REPORT_DIR, RUN_DIR
from .models import Report

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def _short_hash(h: str) -> str:
    if not h:
        return "—"
    return h if len(h) <= 18 else f"{h[:10]}…{h[-6:]}"


_env.filters["short_hash"] = _short_hash


def render_html(report: Report) -> str:
    pay_by_req = {p.request_id: p for p in report.payments if p.request_id}
    return _env.get_template("report.html").render(r=report, pay_by_req=pay_by_req)


def write_report(report: Report, out_dir: Path | None = None) -> Path:
    """Write ``<date>__<run_id>.html`` + ``.json`` under reports/runs/, and refresh
    ``reports/<date>.html`` as the latest-of-day pointer. Returns the HTML path.

    A unique run_id in the filename means two runs on the same day never clobber
    each other; the JSON sidecar links payments, signals, validations and the
    execution gate for downstream tooling.
    """
    out_dir = out_dir or REPORT_DIR
    run_dir = (out_dir / "runs") if out_dir != REPORT_DIR else RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    day = (report.generated_at or date.today().isoformat())[:10]
    stem = f"{day}__{report.run_id or 'run'}"
    html_path = run_dir / f"{stem}.html"
    json_path = run_dir / f"{stem}.json"

    html = render_html(report)
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # latest-of-day pointer (kept for the demo / README path)
    (out_dir / f"{day}.html").write_text(html, encoding="utf-8")
    return html_path
