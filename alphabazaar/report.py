"""Render a Report to a single self-contained HTML file."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import REPORT_DIR
from .models import Report

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def _short_hash(h: str) -> str:
    return h if len(h) <= 18 else f"{h[:10]}…{h[-6:]}"


_env.filters["short_hash"] = _short_hash


def render_html(report: Report) -> str:
    return _env.get_template("report.html").render(r=report)


def write_report(report: Report, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date.today().isoformat()}.html"
    path.write_text(render_html(report), encoding="utf-8")
    return path
