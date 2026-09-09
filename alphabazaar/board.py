"""AlphaBazaar workbench — a single read-only page over everything a run leaves behind.

Sources (no new dependency, no live calls):

* ``reports/runs/*.json``   — the structured report sidecars (payments, signals,
  validations, execution gate) written by ``report.write_report``
* ``var/ledger.db``         — the persistent payment ledger (all runs, all days)

It renders one static self-contained HTML file (``reports/board.html``) in the
same inline-CSS style as the run reports — chosen over a live server because the
reports are already static files, the whole tool is an offline CLI, and there is
nothing to keep running. ``cli board --serve`` will still serve ``reports/`` over
stdlib ``http.server`` for convenience.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import ratings
from .config import REPORT_DIR, settings
from .ledger import BudgetLedger, atomic_to_usdc
from .registry import load_sellers

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def _short(s: str, n: int = 12) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


_env.filters["short"] = _short
_env.filters["usdc"] = lambda a: f"{atomic_to_usdc(a):.2f}"


def _open_ledger(db_path: str | None = None) -> BudgetLedger | None:
    path = db_path or settings.ledger_db
    if path in (":memory:", ""):
        return None
    if not Path(path).exists():
        return None
    return BudgetLedger(
        payment_identity="board", network=settings.x402_network,
        daily_cap_usdc=settings.x402_daily_cap_usdc, db_path=path,
    )


def _load_runs(reports_dir: Path) -> list[dict]:
    run_dir = reports_dir / "runs"
    runs: list[dict] = []
    for jf in sorted(run_dir.glob("*.json")):
        try:
            d = json.loads(jf.read_text())
        except Exception:
            continue
        # tolerate sidecars from an older build: fill the fields the view needs
        d.setdefault("run_id", jf.stem.split("__")[-1])
        d.setdefault("generated_at", "")
        for k, dv in (
            ("binance_mode", "?"), ("x402_mode", "?"), ("spend_usdc", 0.0),
            ("budget_usdc", 0.0), ("budget_remaining_usdc", 0.0),
            ("sellers_attempted", 0), ("sellers_delivered", 0),
            ("analysis_complete", True), ("trade_execution_allowed", True),
        ):
            d.setdefault(k, dv)
        d["_json"] = jf.name
        d["_html"] = jf.with_suffix(".html").name if jf.with_suffix(".html").exists() else ""
        d["_signal_count"] = len(d.get("signals", []))
        d["_payment_count"] = len(d.get("payments", []))
        runs.append(d)
    runs.sort(key=lambda r: r.get("generated_at", ""), reverse=True)
    return runs


def collect(reports_dir: Path | None = None, *, ledger: BudgetLedger | None = None) -> dict:
    reports_dir = reports_dir or REPORT_DIR
    led = ledger if ledger is not None else _open_ledger()
    runs = _load_runs(reports_dir)

    payments: list[dict] = []
    unresolved: list[dict] = []
    seller_rows: list[dict] = []
    ledger_present = led is not None
    if led is not None:
        payments = led.all_payments()
        unresolved = led.unresolved()
        try:
            registry = load_sellers()
        except Exception:
            registry = []
        seller_rows = ratings.ranked(led, registry)

    spend_total = sum(
        atomic_to_usdc(p["amount_atomic"]) for p in payments if p["pay_status"] != "failed"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ledger_present": ledger_present,
        "ledger_path": settings.ledger_db,
        "runs": runs,
        "payments": payments,
        "unresolved": unresolved,
        "sellers": seller_rows,
        "totals": {
            "runs": len(runs),
            "payments": len(payments),
            "spend_usdc": round(spend_total, 2),
            "unresolved": len(unresolved),
        },
    }


def render(ctx: dict) -> str:
    return _env.get_template("board.html").render(**ctx)


def write(path: Path | None = None, *, reports_dir: Path | None = None) -> Path:
    reports_dir = reports_dir or REPORT_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = path or (reports_dir / "board.html")
    out.write_text(render(collect(reports_dir)), encoding="utf-8")
    return out


def serve(reports_dir: Path | None = None, *, port: int = 8900) -> None:  # pragma: no cover
    """Serve ``reports/`` (board + all run reports) over stdlib http.server."""
    import functools
    import http.server
    import socketserver

    reports_dir = reports_dir or REPORT_DIR
    write(reports_dir=reports_dir)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(reports_dir))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {reports_dir} at http://127.0.0.1:{port}/board.html  (Ctrl+C to stop)")
        httpd.serve_forever()
