"""`python -m alphabazaar.cli run` — the demo entrypoint."""

from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import analyst
from .config import settings
from .report import write_report

console = Console()


def _fmt_pct(v: float) -> str:
    color = "green" if v >= 0 else "red"
    return f"[{color}]{v:+.2f}%[/{color}]"


def _on_event(kind: str, data: dict) -> None:
    if kind == "connect":
        console.rule("[bold yellow]AlphaBazaar[/bold yellow]  ·  agent-to-agent research market")
        console.print(
            f"[dim]connecting to Binance Agent OS  ·  mode=[/dim][bold]{data['mode']}[/bold]"
            f"[dim]  ·  {data['mcp']}[/dim]"
        )
    elif kind == "portfolio":
        p = data["portfolio"]
        t = Table(title=f"Agentic sub-account · {p.sub_account}", title_style="bold", header_style="dim")
        for col in ("Asset", "Qty", "Price", "Value", "Weight", "24h"):
            t.add_column(col, justify="right" if col != "Asset" else "left")
        for h in p.holdings:
            t.add_row(
                f"[bold]{h.asset}[/bold]", f"{h.qty:.4f}", f"${h.price_usdc:,.2f}",
                f"${h.value_usdc:,.2f}", f"{p.weight(h.asset):.1f}%", _fmt_pct(h.change_24h_pct),
            )
        t.add_row("[dim]USDC[/dim]", "[dim]—[/dim]", "[dim]$1.00[/dim]",
                  f"[dim]${p.cash_usdc:,.2f}[/dim]", f"[dim]{p.weight('USDC'):.1f}%[/dim]", "[dim]—[/dim]")
        console.print(t)
        console.print(f"[dim]total value[/dim] [bold]${p.total_value_usdc:,.2f}[/bold]")
    elif kind == "budget":
        console.print(
            f"[dim]x402 research budget[/dim]  remaining today [bold]${data['remaining']:.2f}[/bold]"
            f"[dim] of ${data['cap']:.0f} cap  ·  payer {data['identity'][:12]}…  ·  {data['network']}[/dim]"
        )
        console.print(
            f"[dim]         (separate from Binance sub-account trading cash "
            f"${data['trading_cash']:.2f} — not the payment source)[/dim]\n"
        )
    elif kind == "discover":
        sellers = data["sellers"]
        console.print(
            f"[dim]discovered {len(sellers)} seller agent(s) in the x402 market:[/dim]"
        )
        scores = data.get("scores") or {}
        for e in sellers:
            s = scores.get(e["id"])
            rep = f"  [dim]rep {s:.2f}[/dim]" if s is not None else ""
            console.print(
                f"  [bold]{e['id']}[/bold] [dim]${e['price_usdc']:.2f}[/dim]  {e['name']}{rep}  "
                f"[dim]{e['endpoint']}[/dim]"
            )
        console.print()
    elif kind == "plan":
        console.print(Panel(data["rationale"], title="agent decides what to buy", border_style="yellow"))
        picks = data["picks"]
        console.print(
            f"[dim]→ purchasing from:[/dim] [bold]{', '.join(picks) if picks else '(nothing)'}[/bold]\n"
        )
    elif kind == "buy_start":
        console.print(f"[dim]GET[/dim] {data['url']}  [yellow]→ 402 Payment Required[/yellow]")
        time.sleep(0.4)
    elif kind == "buy_ok":
        pmt = data["payment"]
        tx = (pmt.tx_hash[:14] + "…") if pmt.tx_hash else "no-tx"
        console.print(
            f"  [green]✔ {pmt.pay_status}[/green] [bold]${pmt.amount_usdc:.2f}[/bold] to [bold]{data['seller']}[/bold]  "
            f"[dim]tx {tx}  ({pmt.mode})[/dim]"
        )
        console.print(
            f"  [green]✔ received[/green] {data['signals']} signal(s)   "
            f"[dim]budget left → ${data['remaining']:.2f}[/dim]\n"
        )
        time.sleep(0.3)
    elif kind == "buy_error":
        console.print(f"  [red]✗ {data['seller']}: {data['error']}[/red]\n")
    elif kind == "validate":
        for v in data["validations"]:
            if not v["ok"]:
                console.print(f"  [red]✗ rebalance rejected by validator:[/red] {'; '.join(v['errors'])}")
    elif kind == "synthesize":
        console.print(Panel(data["narrative"], title="agent briefing", border_style="cyan"))
    elif kind == "done":
        r = data["report"]
        console.print(
            f"\n[dim]run {r.run_id} complete[/dim]  spent [bold yellow]${r.spend_usdc:.2f}[/bold yellow] "
            f"[dim]· budget left ${r.budget_remaining_usdc:.2f}/{r.budget_usdc:.0f} · "
            f"{r.sellers_delivered}/{r.sellers_attempted} sellers · analysis "
            f"{'complete' if r.analysis_complete else 'INCOMPLETE'}[/dim]"
        )
        if r.unresolved_payments:
            console.print(
                f"[yellow]⚠ {len(r.unresolved_payments)} unresolved payment(s); "
                f"reclaim with:[/yellow] [dim]python -m alphabazaar.cli run --resume-run {r.run_id}[/dim]"
            )


def cmd_run(args: argparse.Namespace) -> int:
    result = analyst.run(on_event=_on_event, resume_run_id=args.resume_run)
    path = write_report(result.report)
    console.print(f"\n[bold green]report →[/bold green] {path}")
    console.print(f"[dim]json    →[/dim] {path.with_suffix('.json')}")
    try:
        from . import board

        console.print(f"[dim]board   →[/dim] {board.write()}")
    except Exception as e:  # board is a convenience; never fail the run over it
        console.print(f"[dim]board   → skipped ({type(e).__name__})[/dim]")

    ok_actions = [v.action for v in result.validations if v.ok]
    if not ok_actions or args.no_approve:
        return 0

    # flow: quote -> show quote + expiry -> human approves -> execute once
    try:
        quote, action = analyst.quote_for(result, 0)
    except IndexError:
        return 0
    except Exception as e:  # a live venue quote can fail — don't crash the run
        console.print(f"[red]could not get a quote for the rebalance:[/red] {type(e).__name__}: {e}")
        console.print("[dim]report is written; re-run to retry the rebalance.[/dim]")
        return 1
    q = quote.as_dict()
    console.print(
        Panel(
            f"Convert [bold]{action.from_qty:.6f} {action.from_asset}[/bold] → "
            f"[bold]{q['to']}[/bold]  [dim]({q['rate']:.4f} {action.to_asset}/{action.from_asset})[/dim]\n"
            f"[dim]{action.rationale}[/dim]\n"
            f"quote [bold]{q['quote_id']}[/bold] · "
            f"{'VERIFIED venue quote' if q['verified'] else '[yellow]UNVERIFIED estimate — no live quote available[/yellow]'} · "
            f"expires in {q['expires_in_s']}s",
            title="rebalance proposal — approve this quote?", border_style="yellow",
        )
    )
    if not result.report.trade_execution_allowed:
        console.print(
            "[yellow]note:[/yellow] live execution is blocked this run — "
            f"{'; '.join(result.report.trade_block_reasons)}. "
            "Approval will be recorded but no real order is placed.\n"
        )
    try:
        ans = input("approve & execute inside the sub-account? [y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans != "y":
        console.print("[dim]skipped.[/dim]")
        return 0
    try:
        fill = analyst.approve_and_execute(result, quote, action, human_approved=True)
    except analyst.ExecutionError as e:
        console.print(f"[red]✗ execution refused:[/red] {e}")
        return 1
    console.print(f"[green]✔ {fill.get('status', 'submitted')}[/green]  [dim]{fill}[/dim]")
    return 0


def _mcp_hint(e: Exception) -> int:
    console.print(f"[red]MCP connection failed:[/red] {type(e).__name__}: {e}")
    console.print(
        "[dim]Binance Agent OS currently allows only whitelisted MCP clients "
        "(Claude, Claude Code, Codex, ChatGPT, Cursor, VS Code); a custom client "
        "is rejected with 'unsupported agent'. Use BINANCE_MODE=snapshot — read "
        "the sub-account through a supported client into alphabazaar/snapshot.json "
        "(see README). `live` mode works once Binance opens client registration: "
        "then set BINANCE_OAUTH_CLIENT_METADATA_URL to a public CIMD JSON "
        "(client_id == that URL, token_endpoint_auth_method=none) and re-run mcp-auth.[/dim]"
    )
    return 1


def cmd_mcp_auth(args: argparse.Namespace) -> int:
    from .mcp_client import mcp_auth

    console.print("[dim]starting Binance Agent OS OAuth… a browser tab will open.[/dim]")
    try:
        mcp_auth()
    except Exception as e:
        return _mcp_hint(e)
    return 0


def cmd_mcp_probe(args: argparse.Namespace) -> int:
    from .mcp_client import McpBinanceClient

    try:
        info = McpBinanceClient().probe()
    except Exception as e:
        return _mcp_hint(e)
    console.print(Panel(f"{info['server']} v{info['version']}\n{info['instructions']}", title="server"))
    t = Table(header_style="dim")
    t.add_column("tool")
    t.add_column("description")
    for tool in info["tools"]:
        t.add_row(tool["name"], tool["description"])
    console.print(t)
    console.print("[dim]map these names in alphabazaar/mcp_client.py::_TOOL if they differ.[/dim]")
    return 0


def cmd_x402_selftest(args: argparse.Namespace) -> int:
    """One real x402 payment against a running seller — proves live settlement."""
    import uuid

    from . import x402
    from .ledger import BudgetLedger
    from .x402 import PaymentError, paid_get

    url = args.url or settings.seller_funding_url
    console.print(
        f"[dim]x402 mode=[/dim][bold]{settings.x402_mode}[/bold][dim]  network={settings.x402_network}"
        f"  facilitator={settings.x402_facilitator_url}[/dim]"
    )
    if settings.x402_mode == "live":
        acct = x402._local_account()
        if acct is None:
            console.print("[red]X402_MODE=live needs X402_WALLET_PRIVATE_KEY[/red]")
            return 1
        console.print(f"[dim]payer wallet:[/dim] {acct.address}")

    budget = BudgetLedger(
        payment_identity=x402.payer_address(),
        network=settings.x402_network,
        daily_cap_usdc=settings.x402_daily_cap_usdc,
        db_path=":memory:",  # selftest: don't touch the persistent ledger
    )
    run_id = "selftest-" + uuid.uuid4().hex[:8]
    try:
        body, payment = paid_get(url, "/analysis", budget, run_id=run_id, seller_id="selftest")
    except PaymentError as e:
        console.print(f"[red]payment failed:[/red] {e}")
        return 1
    except Exception as e:
        console.print(f"[red]{type(e).__name__}:[/red] {e}")
        return 1

    console.print(
        f"[green]✔ paid ${payment.amount_usdc:.2f} to {payment.seller}[/green]  "
        f"[dim]({payment.mode})[/dim]"
    )
    console.print(f"[dim]tx:[/dim] {payment.tx_hash}")
    if payment.mode == "live" and settings.x402_network == "base-sepolia" and payment.tx_hash.startswith("0x"):
        console.print(f"[dim]https://sepolia.basescan.org/tx/{payment.tx_hash}[/dim]")
    console.print(f"[dim]signals received: {len(body.get('signals', []))}[/dim]")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    from . import board

    if args.serve:
        try:
            board.serve(port=args.port)
        except KeyboardInterrupt:
            console.print("\n[dim]stopped.[/dim]")
        return 0
    path = board.write()
    console.print(f"[bold green]workbench →[/bold green] {path}")
    ctx = board.collect()
    console.print(
        f"[dim]{ctx['totals']['runs']} run(s) · {ctx['totals']['payments']} payment(s) · "
        f"${ctx['totals']['spend_usdc']:.2f} spent · {ctx['totals']['unresolved']} unresolved[/dim]"
    )
    return 0


def cmd_sellers(args: argparse.Namespace) -> int:
    from . import ratings
    from .ledger import BudgetLedger
    from .registry import load_sellers

    led = BudgetLedger(payment_identity="cli", network=settings.x402_network,
                       daily_cap_usdc=settings.x402_daily_cap_usdc, db_path=settings.ledger_db)
    rows = ratings.ranked(led, load_sellers())
    t = Table(header_style="dim", title="seller reputation (from the payment ledger)")
    for c in ("seller", "score", "calls", "fill", "delivery", "unknown", "degraded", "avg $", "hit-rate"):
        t.add_column(c, justify="left" if c == "seller" else "right")
    for s in rows:
        st = s["stats"]
        hr = st["hit_rate"]
        hr_cell = f"{hr*100:.0f}% ({st['signals_scored']})" if hr is not None else "—"
        t.add_row(
            s["id"], f"{st['score']:.2f}{'' if st['confident'] else ' ?'}", str(st["calls"]),
            f"{st['fill_rate']*100:.0f}%", f"{st['delivery_rate']*100:.0f}%",
            f"{st['unknown_rate']*100:.0f}%", f"{st['degraded_rate']*100:.0f}%",
            f"${st['avg_price_usdc']:.2f}", hr_cell,
        )
    console.print(t)
    console.print("[dim]score shrinks toward 0.60 until ≥3 calls (?)  ·  hit-rate needs `score-signals` on matured signals[/dim]")
    return 0


def cmd_score_signals(args: argparse.Namespace) -> int:
    from . import marketdata, signal_scoring
    from .ledger import BudgetLedger

    led = BudgetLedger(payment_identity="cli", network=settings.x402_network,
                       daily_cap_usdc=settings.x402_daily_cap_usdc, db_path=settings.ledger_db)

    def price_fn(asset: str):
        row = marketdata.get_spot_tickers([asset]).get(asset) or {}
        return row.get("price") or None

    tally = signal_scoring.score_pending(led, price_fn=price_fn)
    total = sum(tally.values())
    if not total:
        console.print("[dim]no matured signals to score (nothing past its valid_until).[/dim]")
        return 0
    console.print(
        f"[green]scored {total} matured signal(s):[/green] "
        f"{tally['hit']} hit · {tally['miss']} miss · {tally['neutral']} neutral · "
        f"{tally['unscorable']} unscorable"
    )
    prov = marketdata.data_provenance()
    if prov["degraded"]:
        console.print("[yellow]note:[/yellow] some prices came from the synthetic fallback — hit/miss is approximate")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alphabazaar", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="read sub-account, pay sellers via x402, write report")
    p_run.add_argument("--no-approve", action="store_true", help="skip the interactive rebalance prompt")
    p_run.add_argument("--resume-run", metavar="RUN_ID", default=None,
                       help="re-use a prior run's ID to reclaim unresolved payments (no re-signing)")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("mcp-auth", help="one-time Binance Agent OS OAuth handshake").set_defaults(
        func=cmd_mcp_auth
    )
    sub.add_parser("mcp-probe", help="print the MCP server's advertised tools").set_defaults(
        func=cmd_mcp_probe
    )
    p_x = sub.add_parser("x402-selftest", help="do one real x402 payment against a running seller")
    p_x.add_argument("--url", help="seller base URL (default: SELLER_FUNDING_URL)")
    p_x.set_defaults(func=cmd_x402_selftest)

    p_b = sub.add_parser("board", help="generate the read-only workbench (reports/board.html)")
    p_b.add_argument("--serve", action="store_true", help="serve reports/ over http instead of just writing")
    p_b.add_argument("--port", type=int, default=8900, help="port for --serve (default 8900)")
    p_b.set_defaults(func=cmd_board)

    sub.add_parser("sellers", help="print seller reputation from the payment ledger").set_defaults(
        func=cmd_sellers
    )
    sub.add_parser(
        "score-signals",
        help="grade matured signals (past their valid_until) against current prices",
    ).set_defaults(func=cmd_score_signals)

    args = parser.parse_args(argv)
    problems = settings.validate()
    if problems:
        console.print("[red]configuration errors:[/red]")
        for p in problems:
            console.print(f"  [red]•[/red] {p}")
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted.[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
