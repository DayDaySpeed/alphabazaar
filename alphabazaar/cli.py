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
            f"[dim]x402 budget[/dim]  spendable [bold]${data['balance']:.2f} USDC[/bold]  "
            f"[dim](daily cap ${data['cap']:.0f})[/dim]\n"
        )
    elif kind == "plan":
        console.print(Panel(data["rationale"], title="agent decides what to buy", border_style="yellow"))
        console.print(f"[dim]→ purchasing from:[/dim] [bold]{', '.join(data['picks'])}[/bold]\n")
    elif kind == "buy_start":
        console.print(f"[dim]GET[/dim] {data['url']}  [yellow]→ 402 Payment Required[/yellow]")
        time.sleep(0.4)
    elif kind == "buy_ok":
        pmt = data["payment"]
        console.print(
            f"  [green]✔ paid[/green] [bold]${pmt.amount_usdc:.2f}[/bold] to [bold]{data['seller']}[/bold]  "
            f"[dim]tx {pmt.tx_hash[:14]}…  ({pmt.mode})[/dim]"
        )
        console.print(
            f"  [green]✔ received[/green] {data['signals']} signal(s)   "
            f"[dim]sub-account USDC → ${data['balance']:.2f}[/dim]\n"
        )
        time.sleep(0.3)
    elif kind == "buy_error":
        console.print(f"  [red]✗ {data['seller']}: {data['error']}[/red]\n")
    elif kind == "synthesize":
        console.print(Panel(data["narrative"], title="agent briefing", border_style="cyan"))
    elif kind == "done":
        led = data["ledger"]
        console.print(
            f"\n[dim]run complete[/dim]  spent [bold yellow]${led.spent_today:.2f}[/bold yellow] "
            f"[dim]of ${led.daily_cap_usdc:.0f} cap  ·  {len(led.payments)} payment(s)[/dim]"
        )


def cmd_run(args: argparse.Namespace) -> int:
    result = analyst.run(on_event=_on_event)
    path = write_report(result.report)
    console.print(f"\n[bold green]report →[/bold green] {path}")

    rebs = result.report.rebalance
    if rebs and not args.no_approve:
        a = rebs[0]
        console.print(
            Panel(
                f"Convert [bold]{a.from_qty:.4f} {a.from_asset}[/bold] → "
                f"≈[bold]{a.est_to_qty:,.2f} {a.to_asset}[/bold]\n[dim]{a.rationale}[/dim]",
                title="rebalance proposal — approve?", border_style="yellow",
            )
        )
        try:
            ans = input("execute inside the sub-account? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans == "y":
            fill = analyst.approve_rebalance(result.report, 0)
            console.print(f"[green]✔ {fill.get('status', 'submitted')}[/green]  [dim]{fill}[/dim]")
        else:
            console.print("[dim]skipped.[/dim]")
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
    from . import x402
    from .x402 import Ledger, PaymentError, paid_get

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

    ledger = Ledger(balance_usdc=100.0)
    try:
        body, payment = paid_get(url, "/analysis", ledger)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alphabazaar", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="read sub-account, pay sellers via x402, write report")
    p_run.add_argument("--no-approve", action="store_true", help="skip the interactive rebalance prompt")
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted.[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
