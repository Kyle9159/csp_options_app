"""
seed_trade_outcomes.py — One-time seed of trade_outcomes.db from Google Sheet.

Reads all rows from the Trade_History worksheet that have an Exit Date
and inserts them into the `trades` table, skipping any already present
(matched on symbol + strike + expiration + entry_date to avoid duplicates).

Usage:
    python seed_trade_outcomes.py [--dry-run]
"""

import sys
import os
import sqlite3
import re
from datetime import datetime, date
from pathlib import Path

import dotenv
import gspread

dotenv.load_dotenv()

DRY_RUN = "--dry-run" in sys.argv
DB_PATH = Path("cache_files/trade_outcomes.db")
# The Trade_History sheet — hardcoded because .env GOOGLE_SHEET_ID points to
# a different sheet (Live_Trades). Override via TRADE_HISTORY_SHEET_ID env var.
SHEET_ID = os.getenv("TRADE_HISTORY_SHEET_ID", "1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc")

# ── helpers ──────────────────────────────────────────────────────────────────

def safe_float(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default

def safe_int(v, default=0):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return default

def parse_date(v):
    """Return ISO date string (YYYY-MM-DD) or None."""
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def infer_outcome(row, pnl):
    """Map sheet data → trades.outcome enum value."""
    # Prefer explicit columns when present
    exit_reason = str(row.get("Exit Reason", "") or "").lower()
    win_loss = str(row.get("Win/Loss", "") or "").lower()
    notes = str(row.get("Notes", "") or "").lower()
    combined = exit_reason + " " + notes

    if "assign" in combined:
        return "assigned"
    if "roll" in combined:
        return "rolled"
    if "expired" in combined or "worthless" in combined or "expir" in combined:
        return "expired_worthless"
    exit_prem = safe_float(row.get("Exit Premium", 0))
    if exit_prem == 0:
        return "expired_worthless"
    if "loss" in win_loss or pnl < 0:
        return "closed_loss"
    return "closed_profit"

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def ensure_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER,
            symbol TEXT NOT NULL,
            occ_symbol TEXT DEFAULT '',
            strike REAL NOT NULL,
            expiration TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_premium REAL NOT NULL,
            contracts INTEGER DEFAULT 1,
            capital_deployed REAL DEFAULT 0,
            exit_date TEXT,
            exit_premium REAL,
            outcome TEXT DEFAULT 'open',
            pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            holding_days INTEGER DEFAULT 0,
            annualized_return REAL DEFAULT 0,
            schwab_order_id TEXT DEFAULT '',
            schwab_close_order_id TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome);
    """)
    conn.commit()

def already_exists(conn, symbol, strike, expiration, entry_date):
    row = conn.execute(
        "SELECT id FROM trades WHERE symbol=? AND strike=? AND expiration=? AND entry_date=?",
        (symbol, strike, expiration, entry_date)
    ).fetchone()
    return row is not None

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Connecting to Google Sheets...")
    gc = gspread.service_account(filename="google-credentials.json")
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet("Trade_History")
    records = ws.get_all_records()
    print(f"  {len(records)} total rows in Trade_History")

    # Only seed closed trades (have an Exit Date)
    closed = [r for r in records if parse_date(r.get("Exit Date", ""))]
    print(f"  {len(closed)} rows with Exit Date → candidates to seed")

    if not closed:
        print("Nothing to seed.")
        return

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_conn()
    ensure_tables(conn)

    inserted = skipped = errors = 0

    for r in closed:
        symbol = str(r.get("Symbol", "")).strip().upper()
        strike = safe_float(r.get("Strike", 0))
        exp_raw = r.get("Exp Date", "") or r.get("Expiration", "")
        expiration = parse_date(exp_raw) or str(exp_raw).strip()
        entry_date = parse_date(r.get("Entry Date", "")) or date.today().isoformat()
        exit_date = parse_date(r.get("Exit Date", ""))
        entry_prem = safe_float(r.get("Entry Premium", 0))
        exit_prem = safe_float(r.get("Exit Premium", 0))
        contracts = safe_int(r.get("Contracts Qty", 1)) or 1
        capital = strike * 100 * contracts
        pnl = safe_float(r.get("Net Profit $", 0))
        days_held = safe_int(r.get("Days Held", 0))
        notes = str(r.get("Notes", "")).strip()

        if not symbol or strike == 0:
            print(f"  SKIP (missing symbol/strike): {r}")
            skipped += 1
            continue

        pnl_pct = (pnl / capital * 100) if capital > 0 else 0
        annualized = (pnl_pct / days_held * 365) if days_held > 0 else 0
        outcome = infer_outcome(r, pnl)

        if already_exists(conn, symbol, strike, expiration, entry_date):
            print(f"  SKIP (duplicate): {symbol} ${strike}P exp {expiration}")
            skipped += 1
            continue

        try:
            if not DRY_RUN:
                conn.execute("""
                    INSERT INTO trades (
                        symbol, strike, expiration, entry_date, entry_premium,
                        contracts, capital_deployed, exit_date, exit_premium,
                        outcome, pnl, pnl_pct, holding_days, annualized_return,
                        notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol, strike, expiration, entry_date, entry_prem,
                    contracts, capital, exit_date, exit_prem,
                    outcome, round(pnl, 2), round(pnl_pct, 2),
                    days_held, round(annualized, 2), notes
                ))
                conn.commit()
            outcome_label = f"{'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'BE'} ({outcome})"
            print(f"  {'[dry] ' if DRY_RUN else ''}INSERT: {symbol} ${strike}P exp {expiration} | "
                  f"P/L ${pnl:+,.2f} | {outcome_label}")
            inserted += 1
        except Exception as e:
            print(f"  ERROR inserting {symbol}: {e}")
            errors += 1

    conn.close()
    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}Done: {inserted} inserted, {skipped} skipped, {errors} errors")
    if DRY_RUN:
        print("Re-run without --dry-run to commit changes.")

if __name__ == "__main__":
    main()
