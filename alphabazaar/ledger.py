"""Persistent, cross-process budget + payment ledger (SQLite).

Why this exists
---------------
The old ``x402.Ledger`` was an in-memory dataclass rebuilt on every
``analyst.run()`` — the x402 daily cap reset on every process restart, and a
payment whose HTTP response was lost left no trace (a naive retry would mint a
*new* EIP-3009 authorization and pay twice).

This module is the durable record. It tracks spend per
``(payment_identity, network, UTC date)`` so the research budget survives
restarts and rolls over correctly at UTC midnight, and it separates three things
the demo previously conflated:

* **research budget**   — the x402 daily cap, enforced here per identity/network/day
* **payment wallet**    — the on-chain USDC wallet that actually signs (``payment_identity``)
* **trading account**   — the Binance sub-account cash (reported, never spent here)

All money is stored as **integer USDC atomic units** (6 dp) — no float drift.
Concurrency is serialised with ``BEGIN IMMEDIATE`` reservations, so two processes
racing the same budget cannot both win.

Idempotency & reclaim
---------------------
Each seller call gets a deterministic ``request_id = sha256(run_id | seller_id)``.

* ``reserve()`` is idempotent on ``request_id``: a second call returns the
  existing row (``resumed=True``) instead of allocating budget again.
* If a payment's outcome is unknown (network dropped after we sent
  ``X-PAYMENT``), the row is parked at ``pay_status='settlement_unknown'`` — it
  keeps consuming budget and is **never** re-paid blindly.
* Re-running with ``--resume-run <run_id>`` reproduces the same ``request_id``s;
  ``paid_get`` then replays the *stored* authorization header (an idempotent GET)
  to reclaim the analysis body without signing anything new.

Scope: reclaim spans processes and restarts **for the same run_id**. Across
different run_ids the ledger still prevents double-spend (budget is charged) and
surfaces unresolved rows, but does not auto-retry them.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

USDC_DECIMALS = 6
_SCALE = 10**USDC_DECIMALS

# pay_status values that count against the daily budget
CONSUMING = ("reserved", "settled", "settlement_unknown")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def usdc_to_atomic(amount) -> int:
    """USDC (float/str/Decimal) -> integer atomic units, half-up at 6dp."""
    return int((Decimal(str(amount)) * _SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def atomic_to_usdc(atomic) -> float:
    return int(atomic) / _SCALE


class BudgetError(RuntimeError):
    """Raised when a reservation would exceed the daily research budget."""


@dataclass
class Reservation:
    request_id: str
    amount_atomic: int
    resumed: bool  # True -> a prior attempt already booked this; do not re-authorize
    pay_status: str
    auth_header: str = ""
    tx_hash: str = ""
    delivery_status: str = "pending"

    @property
    def amount_usdc(self) -> float:
        return atomic_to_usdc(self.amount_atomic)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    request_id        TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    seller_id         TEXT NOT NULL,
    payment_identity  TEXT NOT NULL,
    network           TEXT NOT NULL,
    utc_date          TEXT NOT NULL,
    amount_atomic     INTEGER NOT NULL,
    quote_max_atomic  INTEGER NOT NULL,
    pay_to            TEXT NOT NULL,
    asset             TEXT NOT NULL DEFAULT '',
    auth_nonce        TEXT NOT NULL DEFAULT '',
    auth_header       TEXT NOT NULL DEFAULT '',
    pay_status        TEXT NOT NULL,
    tx_hash           TEXT NOT NULL DEFAULT '',
    settle_note       TEXT NOT NULL DEFAULT '',
    delivery_status   TEXT NOT NULL DEFAULT 'pending',
    signals_count     INTEGER NOT NULL DEFAULT 0,
    data_degraded     INTEGER NOT NULL DEFAULT 0,  -- seller answered on synthetic/fallback data
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payments_budget
    ON payments (payment_identity, network, utc_date, pay_status);
CREATE INDEX IF NOT EXISTS ix_payments_seller ON payments (seller_id);

CREATE TABLE IF NOT EXISTS executions (
    approval_token   TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    account          TEXT NOT NULL,
    action_hash      TEXT NOT NULL,
    quote_id         TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,   -- submitted | filled | failed | unknown
    detail           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id     TEXT PRIMARY KEY,          -- sha256(run_id|seller_id|asset|title)
    run_id        TEXT NOT NULL,
    request_id    TEXT NOT NULL DEFAULT '',  -- -> payments.request_id
    seller_id     TEXT NOT NULL,
    asset         TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- opportunity | risk | info
    direction     TEXT NOT NULL,             -- bullish | bearish | neutral
    ref_price     REAL NOT NULL DEFAULT 0,   -- asset price when the signal was issued
    issued_at     TEXT NOT NULL,
    horizon       TEXT NOT NULL DEFAULT '',
    valid_until   TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL DEFAULT 'pending',  -- pending|hit|miss|neutral|unscorable
    scored_at     TEXT NOT NULL DEFAULT '',
    scored_price  REAL NOT NULL DEFAULT 0,
    move_pct      REAL NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_signal_outcomes_seller ON signal_outcomes (seller_id, outcome);
"""


def _migrate(cx: sqlite3.Connection) -> None:
    """Additive, idempotent column migrations for DBs created by an older build."""
    cols = {r[1] for r in cx.execute("PRAGMA table_info(payments)")}
    if "data_degraded" not in cols:
        cx.execute("ALTER TABLE payments ADD COLUMN data_degraded INTEGER NOT NULL DEFAULT 0")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout FIRST — the journal_mode switch itself takes a lock and would
    # otherwise fail immediately under concurrency ("database is locked").
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # a concurrent writer holds the lock; WAL is likely already set
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# in-process lock: SQLite serialises across processes, this avoids a needless
# BEGIN IMMEDIATE retry storm between threads of one process (the concurrency test).
_PROC_LOCK = threading.Lock()


class BudgetLedger:
    """Durable budget + payment record for one payer identity on one network."""

    def __init__(
        self,
        *,
        payment_identity: str,
        network: str,
        daily_cap_usdc: float,
        db_path: str | Path = ":memory:",
    ):
        self.payment_identity = payment_identity or "unknown"
        self.network = network
        self.daily_cap_atomic = usdc_to_atomic(daily_cap_usdc)
        self.db_path = str(db_path)
        # a shared in-memory DB needs one sticky connection; a file DB opens per call
        self._mem = self.db_path in (":memory:", "")
        if self._mem:
            self._conn = _connect(":memory:")
            self._conn.executescript(_SCHEMA)
            _migrate(self._conn)
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            with _connect(self.db_path) as c:
                have = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('payments','executions','signal_outcomes')"
                ).fetchall()
                if len(have) < 3:  # first open, or an older DB missing a table
                    c.executescript(_SCHEMA)
                _migrate(c)

    # -- connection helper ----------------------------------------------------
    def _cx(self) -> sqlite3.Connection:
        return self._conn if self._mem else _connect(self.db_path)

    # -- budget -------------------------------------------------------------
    def spent_today_atomic(self, *, day: str | None = None) -> int:
        day = day or utc_day()
        cx = self._cx()
        try:
            row = cx.execute(
                f"""SELECT COALESCE(SUM(amount_atomic), 0) AS s FROM payments
                    WHERE payment_identity=? AND network=? AND utc_date=?
                      AND pay_status IN ({','.join('?' * len(CONSUMING))})""",
                (self.payment_identity, self.network, day, *CONSUMING),
            ).fetchone()
            return int(row["s"])
        finally:
            if not self._mem:
                cx.close()

    def remaining_atomic(self, *, day: str | None = None) -> int:
        return self.daily_cap_atomic - self.spent_today_atomic(day=day)

    def remaining_usdc(self) -> float:
        return atomic_to_usdc(self.remaining_atomic())

    # -- reservation ------------------------------------------------------
    @staticmethod
    def request_id(run_id: str, seller_id: str) -> str:
        return hashlib.sha256(f"{run_id}|{seller_id}".encode()).hexdigest()

    def reserve(
        self,
        *,
        request_id: str,
        run_id: str,
        seller_id: str,
        amount_usdc: float,
        quote_max_usdc: float,
        pay_to: str,
        asset: str = "",
    ) -> Reservation:
        """Atomically book ``amount_usdc`` against today's budget, or resume a prior booking.

        Raises ``BudgetError`` if there is no prior row and the amount would
        exceed the daily cap.
        """
        amount_atomic = usdc_to_atomic(amount_usdc)
        quote_max_atomic = usdc_to_atomic(quote_max_usdc)
        now = utc_now_iso()
        day = utc_day()

        with _PROC_LOCK:
            cx = self._cx()
            try:
                cx.execute("BEGIN IMMEDIATE")
                existing = cx.execute(
                    "SELECT * FROM payments WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing is not None and existing["pay_status"] != "failed":
                    # a live reservation for this request already exists — resume it,
                    # never allocate budget twice
                    cx.execute("COMMIT")
                    return Reservation(
                        request_id=request_id,
                        amount_atomic=int(existing["amount_atomic"]),
                        resumed=True,
                        pay_status=existing["pay_status"],
                        auth_header=existing["auth_header"],
                        tx_hash=existing["tx_hash"],
                        delivery_status=existing["delivery_status"],
                    )
                # a prior attempt for this request failed pre-settlement (no money
                # moved, budget released). A fresh reserve must re-check the cap
                # and reactivate the row rather than silently resuming a dead one.
                reactivating = existing is not None

                spent = cx.execute(
                    f"""SELECT COALESCE(SUM(amount_atomic), 0) AS s FROM payments
                        WHERE payment_identity=? AND network=? AND utc_date=?
                          AND pay_status IN ({','.join('?' * len(CONSUMING))})""",
                    (self.payment_identity, self.network, day, *CONSUMING),
                ).fetchone()["s"]
                if int(spent) + amount_atomic > self.daily_cap_atomic:
                    cx.execute("ROLLBACK")
                    raise BudgetError(
                        f"x402 daily cap reached: {atomic_to_usdc(spent):.2f} spent + "
                        f"{amount_usdc:.2f} > {atomic_to_usdc(self.daily_cap_atomic):.2f} "
                        f"USDC ({self.payment_identity[:10]}… / {self.network} / {day})"
                    )

                if reactivating:
                    cx.execute(
                        """UPDATE payments SET
                             run_id=?, amount_atomic=?, quote_max_atomic=?, pay_to=?, asset=?,
                             utc_date=?, pay_status='reserved', delivery_status='pending',
                             auth_nonce='', auth_header='', tx_hash='', settle_note='',
                             data_degraded=0, updated_at=?
                           WHERE request_id=?""",
                        (run_id, amount_atomic, quote_max_atomic, pay_to, asset, day, now, request_id),
                    )
                else:
                    cx.execute(
                        """INSERT INTO payments
                           (request_id, run_id, seller_id, payment_identity, network, utc_date,
                            amount_atomic, quote_max_atomic, pay_to, asset, pay_status,
                            created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?, 'reserved', ?, ?)""",
                        (
                            request_id, run_id, seller_id, self.payment_identity, self.network, day,
                            amount_atomic, quote_max_atomic, pay_to, asset, now, now,
                        ),
                    )
                cx.execute("COMMIT")
            except BudgetError:
                raise
            except Exception:
                try:
                    cx.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                if not self._mem:
                    cx.close()

        return Reservation(
            request_id=request_id, amount_atomic=amount_atomic, resumed=False,
            pay_status="reserved",
        )

    # -- state transitions -------------------------------------------------
    def _update(self, request_id: str, **cols) -> None:
        cols["updated_at"] = utc_now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        cx = self._cx()
        try:
            cx.execute(f"UPDATE payments SET {sets} WHERE request_id=?", (*cols.values(), request_id))
        finally:
            if not self._mem:
                cx.close()

    def attach_authorization(self, request_id: str, *, auth_nonce: str, auth_header: str) -> None:
        self._update(request_id, auth_nonce=auth_nonce, auth_header=auth_header)

    def mark_settled(self, request_id: str, *, tx_hash: str = "", note: str = "") -> None:
        self._update(request_id, pay_status="settled", tx_hash=tx_hash, settle_note=note)

    def mark_settlement_unknown(self, request_id: str, *, note: str) -> None:
        # keeps consuming budget on purpose — we do NOT know it failed
        self._update(request_id, pay_status="settlement_unknown", settle_note=note)

    def mark_failed(self, request_id: str, *, reason: str) -> None:
        # releases budget: a rejected-before-settlement payment never moved money
        self._update(request_id, pay_status="failed", settle_note=reason)

    def mark_delivered(self, request_id: str, *, signals_count: int) -> None:
        self._update(request_id, delivery_status="delivered", signals_count=signals_count)

    def mark_delivery_failed(self, request_id: str, *, reason: str) -> None:
        self._update(request_id, delivery_status="failed", settle_note=reason)

    def mark_provenance(self, request_id: str, *, degraded: bool) -> None:
        """Record whether the seller answered on live or synthetic/fallback data."""
        self._update(request_id, data_degraded=1 if degraded else 0)

    # -- reads -----------------------------------------------------------
    def get(self, request_id: str) -> dict | None:
        cx = self._cx()
        try:
            row = cx.execute("SELECT * FROM payments WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None
        finally:
            if not self._mem:
                cx.close()

    def unresolved(self, *, run_id: str | None = None) -> list[dict]:
        """Rows that took budget but never delivered, or whose settlement is unknown."""
        cx = self._cx()
        try:
            q = (
                "SELECT * FROM payments WHERE "
                "(pay_status='settlement_unknown' OR (pay_status IN ('reserved','settled') "
                "AND delivery_status!='delivered'))"
            )
            args: tuple = ()
            if run_id:
                q += " AND run_id=?"
                args = (run_id,)
            return [dict(r) for r in cx.execute(q, args).fetchall()]
        finally:
            if not self._mem:
                cx.close()

    def payments_for_run(self, run_id: str) -> list[dict]:
        cx = self._cx()
        try:
            return [
                dict(r)
                for r in cx.execute(
                    "SELECT * FROM payments WHERE run_id=? ORDER BY created_at", (run_id,)
                ).fetchall()
            ]
        finally:
            if not self._mem:
                cx.close()

    def all_payments(self, *, since: str | None = None, limit: int | None = None) -> list[dict]:
        """Every payment row, newest first. ``since`` filters on created_at (ISO)."""
        cx = self._cx()
        try:
            q = "SELECT * FROM payments"
            args: list = []
            if since:
                q += " WHERE created_at >= ?"
                args.append(since)
            q += " ORDER BY created_at DESC"
            if limit:
                q += " LIMIT ?"
                args.append(int(limit))
            return [dict(r) for r in cx.execute(q, args).fetchall()]
        finally:
            if not self._mem:
                cx.close()

    # -- executions (trade guard) --------------------------------------
    def record_execution_intent(
        self, *, approval_token: str, run_id: str, account: str, action_hash: str, quote_id: str
    ) -> bool:
        """Returns True if this is the first use of ``approval_token`` (single-use)."""
        now = utc_now_iso()
        with _PROC_LOCK:
            cx = self._cx()
            try:
                cx.execute("BEGIN IMMEDIATE")
                if cx.execute(
                    "SELECT 1 FROM executions WHERE approval_token=?", (approval_token,)
                ).fetchone():
                    cx.execute("ROLLBACK")
                    return False
                cx.execute(
                    """INSERT INTO executions
                       (approval_token, run_id, account, action_hash, quote_id, status,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?, 'submitted', ?, ?)""",
                    (approval_token, run_id, account, action_hash, quote_id, now, now),
                )
                cx.execute("COMMIT")
                return True
            finally:
                if not self._mem:
                    cx.close()

    def update_execution(self, approval_token: str, *, status: str, detail: str = "") -> None:
        cx = self._cx()
        try:
            cx.execute(
                "UPDATE executions SET status=?, detail=?, updated_at=? WHERE approval_token=?",
                (status, detail, utc_now_iso(), approval_token),
            )
        finally:
            if not self._mem:
                cx.close()

    # -- signal outcomes (seller accuracy over time) -------------------
    def record_signal(
        self,
        *,
        signal_id: str,
        run_id: str,
        request_id: str,
        seller_id: str,
        asset: str,
        kind: str,
        direction: str,
        ref_price: float,
        issued_at: str,
        horizon: str = "",
        valid_until: str = "",
    ) -> None:
        """Log a purchased signal for later hit/miss scoring. Idempotent on signal_id."""
        cx = self._cx()
        try:
            cx.execute(
                """INSERT OR IGNORE INTO signal_outcomes
                   (signal_id, run_id, request_id, seller_id, asset, kind, direction,
                    ref_price, issued_at, horizon, valid_until)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, run_id, request_id, seller_id, asset, kind, direction,
                 float(ref_price or 0), issued_at, horizon, valid_until),
            )
        finally:
            if not self._mem:
                cx.close()

    def pending_signals(self, *, due_by: str) -> list[dict]:
        """Recorded signals past their valid_until that haven't been scored yet."""
        cx = self._cx()
        try:
            return [
                dict(r)
                for r in cx.execute(
                    """SELECT * FROM signal_outcomes
                       WHERE outcome='pending' AND valid_until != '' AND valid_until <= ?
                       ORDER BY valid_until""",
                    (due_by,),
                ).fetchall()
            ]
        finally:
            if not self._mem:
                cx.close()

    def score_signal(
        self, signal_id: str, *, outcome: str, scored_price: float, move_pct: float, note: str = ""
    ) -> None:
        self._sig_update(
            signal_id, outcome=outcome, scored_at=utc_now_iso(),
            scored_price=float(scored_price or 0), move_pct=float(move_pct or 0), note=note,
        )

    def _sig_update(self, signal_id: str, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)  # keys are internal literals only
        cx = self._cx()
        try:
            cx.execute(
                f"UPDATE signal_outcomes SET {sets} WHERE signal_id=?",
                (*cols.values(), signal_id),
            )
        finally:
            if not self._mem:
                cx.close()

    def signals_for_seller(self, seller_id: str) -> list[dict]:
        cx = self._cx()
        try:
            return [
                dict(r)
                for r in cx.execute(
                    "SELECT * FROM signal_outcomes WHERE seller_id=? ORDER BY issued_at DESC",
                    (seller_id,),
                ).fetchall()
            ]
        finally:
            if not self._mem:
                cx.close()

    def signal_hit_rates(self) -> dict[str, dict]:
        """seller_id -> {hits, misses, neutral, scored, hit_rate|None}."""
        cx = self._cx()
        try:
            rows = cx.execute(
                """SELECT seller_id, outcome, COUNT(*) AS n FROM signal_outcomes
                   GROUP BY seller_id, outcome"""
            ).fetchall()
        finally:
            if not self._mem:
                cx.close()
        out: dict[str, dict] = {}
        for r in rows:
            d = out.setdefault(r["seller_id"], {"hits": 0, "misses": 0, "neutral": 0, "scored": 0})
            if r["outcome"] == "hit":
                d["hits"] += r["n"]
            elif r["outcome"] == "miss":
                d["misses"] += r["n"]
            elif r["outcome"] == "neutral":
                d["neutral"] += r["n"]
        for d in out.values():
            decisive = d["hits"] + d["misses"]
            d["scored"] = decisive + d["neutral"]
            d["hit_rate"] = round(d["hits"] / decisive, 3) if decisive else None
        return out
