"""
Trade Outcome Tracker — SQLite trade outcome logging + Schwab API sync.

Records every scanner recommendation and tracks what actually happened
(closed for profit, assigned, expired worthless, etc.) so we can calibrate
scoring over time.

Schwab API integration: polls order history and account positions to
automatically detect closed/expired/assigned trades and record outcomes.
"""

import sqlite3
import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = Path("cache_files") / "trade_outcomes.db"
DB_PATH.parent.mkdir(exist_ok=True)


@contextmanager
def get_db():
    """Context manager for database connections with WAL mode."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strike REAL NOT NULL,
                expiration TEXT NOT NULL,
                dte INTEGER NOT NULL,
                premium REAL NOT NULL,
                delta REAL DEFAULT 0,
                iv REAL DEFAULT 0,
                annualized_roi REAL DEFAULT 0,
                distance_pct REAL DEFAULT 0,
                tier INTEGER DEFAULT 3,
                regime TEXT DEFAULT '',
                grok_trade_score INTEGER DEFAULT 0,
                grok_recommendation TEXT DEFAULT '',
                improved_put_score REAL DEFAULT 0,
                rebound_score INTEGER DEFAULT 0,
                sr_risk_flag TEXT DEFAULT '',
                overall_rank INTEGER DEFAULT 0,
                occ_symbol TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_id INTEGER REFERENCES recommendations(id),
                symbol TEXT NOT NULL,
                occ_symbol TEXT DEFAULT '',
                strike REAL NOT NULL,
                expiration TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_premium REAL NOT NULL,
                contracts INTEGER DEFAULT 1,
                capital_deployed REAL DEFAULT 0,
                -- Outcome fields (filled when trade closes)
                exit_date TEXT,
                exit_premium REAL,
                outcome TEXT CHECK(outcome IN (
                    'expired_worthless', 'closed_profit', 'closed_loss',
                    'assigned', 'rolled', 'open'
                )) DEFAULT 'open',
                pnl REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                holding_days INTEGER DEFAULT 0,
                annualized_return REAL DEFAULT 0,
                schwab_order_id TEXT,
                schwab_close_order_id TEXT,
                notes TEXT DEFAULT '',
                delta REAL DEFAULT 0,
                gamma REAL DEFAULT 0,
                theta REAL DEFAULT 0,
                vega REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS schwab_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT NOT NULL,
                synced_at TEXT DEFAULT (datetime('now')),
                orders_processed INTEGER DEFAULT 0,
                trades_updated INTEGER DEFAULT 0,
                errors TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_rec_symbol_date
                ON recommendations(symbol, scan_date);
            CREATE INDEX IF NOT EXISTS idx_trades_outcome
                ON trades(outcome);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_occ
                ON trades(occ_symbol);

            CREATE TABLE IF NOT EXISTS regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regime_key TEXT NOT NULL,
                logged_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_regime_log_at
                ON regime_log(logged_at DESC);
        """)
    # Migrate existing databases: add Greek columns to trades if they're missing
    with get_db() as conn:
        for col_def in [
            "delta REAL DEFAULT 0",
            "gamma REAL DEFAULT 0",
            "theta REAL DEFAULT 0",
            "vega REAL DEFAULT 0",
        ]:
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
                logger.debug("Added column '%s' to trades table", col_name)
            except sqlite3.OperationalError:
                pass  # column already exists
    logger.info("Trade outcome DB initialized")


# ==================== REGIME LOGGING ====================

def log_regime_change(regime_key: str) -> None:
    """
    Log the current regime if it differs from the most recent entry.
    Only records a new row when the regime actually changes so the history
    table stays compact and meaningful (one row = one regime period).
    """
    with get_db() as conn:
        last = conn.execute(
            "SELECT regime_key FROM regime_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last and last["regime_key"] == regime_key:
            return  # No change — skip
        conn.execute(
            "INSERT INTO regime_log (regime_key) VALUES (?)", (regime_key,)
        )
    logger.info("Logged regime change: %s", regime_key)


def get_regime_history(limit: int = 60) -> list[dict]:
    """
    Return the most recent regime log entries (newest first).

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of dicts with keys: id, regime_key, logged_at.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, regime_key, logged_at FROM regime_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ==================== RECOMMENDATION LOGGING ====================

def log_recommendations(scanner_tiles, regime_name, scan_date=None,
                        scan_time=None, day_of_week=None):
    """
    Log all scanner recommendations from a scan run.
    Called at end of simple_options_scanner.main().

    Args:
        scanner_tiles: List of tile dicts from the scanner
        regime_name: Current regime string (e.g. 'MILD_BULL')
        scan_date: Override scan date (default: today)
        scan_time: HH:MM string of when scan ran (default: now)
        day_of_week: e.g. 'Monday' (default: derived from scan_date)
    """
    if not scanner_tiles:
        return 0

    scan_date = scan_date or date.today().isoformat()
    if not scan_time:
        scan_time = datetime.now().strftime('%H:%M')
    if not day_of_week:
        day_of_week = datetime.fromisoformat(scan_date).strftime('%A')

    count = 0

    with get_db() as conn:
        for tile in scanner_tiles:
            for opp in tile.get('suggestions', []):
                exp_date = _expiration_from_dte(opp.get('dte', 30))

                conn.execute("""
                    INSERT INTO recommendations (
                        scan_date, scan_time, day_of_week,
                        symbol, sector, strike, expiration, dte, premium,
                        delta, gamma, theta, vega,
                        iv, iv_rank, annualized_roi, distance_pct, tier, regime,
                        grok_trade_score, grok_recommendation, improved_put_score,
                        rebound_score, sr_risk_flag, overall_rank, occ_symbol,
                        underlying_price, bid, ask,
                        open_interest, volume, bid_ask_spread_pct
                    ) VALUES (
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?
                    )
                """, (
                    scan_date, scan_time, day_of_week,
                    opp.get('symbol', ''),
                    opp.get('sector', ''),
                    opp.get('strike', 0),
                    exp_date,
                    opp.get('dte', 0),
                    opp.get('premium', 0),
                    opp.get('delta', 0),
                    opp.get('gamma', 0),
                    opp.get('theta', 0),
                    opp.get('vega', 0),
                    opp.get('iv', 0),
                    opp.get('iv_rank', 50),
                    opp.get('annualized_roi', 0),
                    opp.get('distance', 0),
                    opp.get('tier', 3),
                    regime_name,
                    opp.get('grok_trade_score', 0),
                    opp.get('grok_recommendation', ''),
                    opp.get('improved_score', 0),
                    opp.get('rebound_score', 0),
                    opp.get('sr_risk_flag', ''),
                    opp.get('overall_rank', 0),
                    opp.get('contract', ''),
                    opp.get('current_price', 0),
                    opp.get('bid', 0),
                    opp.get('ask', 0),
                    opp.get('open_interest', 0),
                    opp.get('volume', 0),
                    opp.get('bid_ask_spread_pct', 0),
                ))
                count += 1

    logger.info(f"Logged {count} recommendations for {scan_date}")
    return count


# ==================== TRADE ENTRY ====================

def log_trade_entry(symbol, strike, expiration, entry_premium, contracts=1,
                    occ_symbol='', schwab_order_id='', recommendation_id=None,
                    entry_date=None):
    """
    Record a new trade (CSP sold to open).
    Can be called manually or by the Schwab sync service.
    """
    entry_date = entry_date or date.today().isoformat()
    capital = strike * 100 * contracts

    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO trades (
                recommendation_id, symbol, occ_symbol, strike, expiration,
                entry_date, entry_premium, contracts, capital_deployed,
                outcome, schwab_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """, (
            recommendation_id, symbol, occ_symbol, strike, expiration,
            entry_date, entry_premium, contracts, capital, schwab_order_id
        ))
        trade_id = cursor.lastrowid

    logger.info(f"Logged trade entry: {symbol} ${strike}P x{contracts} @ ${entry_premium} (id={trade_id})")
    return trade_id


# ==================== TRADE EXIT ====================

def log_trade_exit(trade_id, exit_premium, outcome, exit_date=None,
                   schwab_close_order_id='', notes=''):
    """
    Record trade outcome when closed/assigned/expired.

    Args:
        trade_id: ID from trades table
        exit_premium: Price paid to close (0 if expired worthless)
        outcome: One of: expired_worthless, closed_profit, closed_loss, assigned, rolled
    """
    exit_date = exit_date or date.today().isoformat()

    with get_db() as conn:
        trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return

        entry_premium = trade['entry_premium']
        contracts = trade['contracts']

        # PnL: for a short put, profit = entry_premium - exit_premium
        pnl_per_contract = (entry_premium - exit_premium) * 100
        pnl = pnl_per_contract * contracts
        capital = trade['capital_deployed']
        pnl_pct = (pnl / capital * 100) if capital > 0 else 0

        entry_dt = datetime.fromisoformat(trade['entry_date'])
        exit_dt = datetime.fromisoformat(exit_date)
        holding_days = max((exit_dt - entry_dt).days, 1)
        annualized = (pnl_pct / holding_days) * 365

        conn.execute("""
            UPDATE trades SET
                exit_date = ?, exit_premium = ?, outcome = ?,
                pnl = ?, pnl_pct = ?, holding_days = ?,
                annualized_return = ?, schwab_close_order_id = ?,
                notes = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (
            exit_date, exit_premium, outcome, round(pnl, 2),
            round(pnl_pct, 2), holding_days, round(annualized, 2),
            schwab_close_order_id, notes, trade_id
        ))

    logger.info(f"Trade {trade_id} closed: {outcome} | PnL=${pnl:.2f} ({pnl_pct:.1f}%) | {holding_days}d")


# ==================== SCHWAB API SYNC ====================

def sync_from_schwab(account_id=None):
    """
    Poll Schwab API for order history and positions to automatically
    detect and record trade outcomes.

    Flow:
    1. Get all FILLED orders from last 60 days
    2. Match Sell-to-Open orders -> create trade entries (if not already logged)
    3. Match Buy-to-Close orders -> close matching trades
    4. Check for expired options past their expiration date
    5. Check account positions for assignments (stock appeared)

    Returns:
        dict with sync results
    """
    from schwab_utils import get_client
    from schwab.client import Client
    import dotenv
    dotenv.load_dotenv()

    if account_id is None:
        paper_trading = os.getenv('PAPER_TRADING', 'True').lower() == 'true'
        account_id = os.getenv('SCHWAB_PAPER_ACCOUNT_ID' if paper_trading else 'SCHWAB_LIVE_ACCOUNT_ID')
        if not account_id:
            return {"success": False, "error": "No account ID configured"}

    try:
        client = get_client()
        client.set_enforce_enums(False)
    except Exception as e:
        logger.error(f"Schwab client init failed: {e}")
        return {"success": False, "error": str(e)}

    # Get account hash
    try:
        acct_resp = client.get_account_numbers().json()
        account_hash = None
        for acct in acct_resp:
            if acct.get('accountNumber') == account_id:
                account_hash = acct.get('hashValue')
                break
        if not account_hash:
            return {"success": False, "error": f"Account {account_id} not found"}
    except Exception as e:
        return {"success": False, "error": f"Failed to get account hash: {e}"}

    trades_created = 0
    trades_closed = 0
    errors = []

    # --- Step 1: Get filled orders from last 60 days ---
    try:
        now = datetime.now()
        from_date = now - timedelta(days=60)
        orders_resp = client.get_orders_for_account(
            account_hash,
            from_entered_datetime=from_date,
            to_entered_datetime=now,
            status='FILLED'
        )
        orders = orders_resp.json() if orders_resp.status_code == 200 else []
    except Exception as e:
        errors.append(f"Get orders failed: {e}")
        orders = []

    with get_db() as conn:
        for order in orders:
            try:
                order_id = str(order.get('orderId', ''))
                legs = order.get('orderLegCollection', [])
                if not legs:
                    continue

                for leg in legs:
                    instrument = leg.get('instrument', {})
                    if instrument.get('assetType') != 'OPTION':
                        continue
                    if instrument.get('putCall') != 'PUT':
                        continue

                    occ_symbol = instrument.get('symbol', '')
                    instruction = leg.get('instruction', '')
                    quantity = int(leg.get('quantity', 0))

                    parsed = _parse_occ_symbol(occ_symbol)
                    if not parsed:
                        continue

                    fill_price = _extract_fill_price(order)

                    if instruction == 'SELL_TO_OPEN':
                        # Check if we already logged this order
                        existing = conn.execute(
                            "SELECT id FROM trades WHERE schwab_order_id = ?",
                            (order_id,)
                        ).fetchone()
                        if existing:
                            continue

                        # Find matching recommendation
                        rec = conn.execute("""
                            SELECT id FROM recommendations
                            WHERE symbol = ? AND strike = ? AND expiration = ?
                            ORDER BY scan_date DESC LIMIT 1
                        """, (parsed['symbol'], parsed['strike'], parsed['expiration'])).fetchone()

                        fill_date = _extract_fill_date(order)
                        conn.execute("""
                            INSERT INTO trades (
                                recommendation_id, symbol, occ_symbol, strike,
                                expiration, entry_date, entry_premium, contracts,
                                capital_deployed, outcome, schwab_order_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                        """, (
                            rec['id'] if rec else None,
                            parsed['symbol'], occ_symbol, parsed['strike'],
                            parsed['expiration'], fill_date, fill_price,
                            quantity, parsed['strike'] * 100 * quantity, order_id
                        ))
                        trades_created += 1
                        logger.info(f"Synced STO: {parsed['symbol']} ${parsed['strike']}P x{quantity}")

                    elif instruction == 'BUY_TO_CLOSE':
                        # Find the matching open trade
                        open_trade = conn.execute("""
                            SELECT id, entry_premium, contracts, capital_deployed, entry_date
                            FROM trades
                            WHERE occ_symbol = ? AND outcome = 'open'
                            ORDER BY entry_date DESC LIMIT 1
                        """, (occ_symbol,)).fetchone()

                        if open_trade:
                            fill_date = _extract_fill_date(order)
                            pnl_per = (open_trade['entry_premium'] - fill_price) * 100
                            pnl = pnl_per * open_trade['contracts']
                            outcome = 'closed_profit' if pnl >= 0 else 'closed_loss'
                            capital = open_trade['capital_deployed']
                            pnl_pct = (pnl / capital * 100) if capital > 0 else 0

                            entry_dt = datetime.fromisoformat(open_trade['entry_date'])
                            exit_dt = datetime.fromisoformat(fill_date)
                            holding_days = max((exit_dt - entry_dt).days, 1)
                            annualized = (pnl_pct / holding_days) * 365

                            conn.execute("""
                                UPDATE trades SET
                                    exit_date = ?, exit_premium = ?, outcome = ?,
                                    pnl = ?, pnl_pct = ?, holding_days = ?,
                                    annualized_return = ?, schwab_close_order_id = ?,
                                    updated_at = datetime('now')
                                WHERE id = ?
                            """, (
                                fill_date, fill_price, outcome, round(pnl, 2),
                                round(pnl_pct, 2), holding_days, round(annualized, 2),
                                order_id, open_trade['id']
                            ))
                            trades_closed += 1
                            logger.info(f"Synced BTC: {parsed['symbol']} ${parsed['strike']}P -> {outcome} (${pnl:.2f})")

            except Exception as e:
                errors.append(f"Order {order.get('orderId', '?')}: {e}")

        # --- Step 2: Check for expired options ---
        expired_count = _check_expired_trades(conn)
        trades_closed += expired_count

        # --- Step 3: Check for assignments via positions ---
        assigned_count = _check_assignments(conn, client, account_hash)
        trades_closed += assigned_count

    # Log sync
    with get_db() as conn:
        conn.execute("""
            INSERT INTO schwab_sync_log (sync_type, orders_processed, trades_updated, errors)
            VALUES ('auto', ?, ?, ?)
        """, (len(orders), trades_created + trades_closed, '; '.join(errors[:5])))

    result = {
        "success": True,
        "orders_scanned": len(orders),
        "trades_created": trades_created,
        "trades_closed": trades_closed,
        "errors": errors[:5]
    }
    logger.info(f"Schwab sync complete: {result}")
    return result


def _check_expired_trades(conn):
    """Mark trades as expired_worthless if past expiration and still open."""
    today = date.today().isoformat()
    rows = conn.execute("""
        SELECT id, symbol, strike, expiration, entry_premium, contracts, capital_deployed, entry_date
        FROM trades WHERE outcome = 'open' AND expiration < ?
    """, (today,)).fetchall()

    count = 0
    for row in rows:
        pnl = row['entry_premium'] * 100 * row['contracts']
        capital = row['capital_deployed']
        pnl_pct = (pnl / capital * 100) if capital > 0 else 0

        entry_dt = datetime.fromisoformat(row['entry_date'])
        exp_dt = datetime.fromisoformat(row['expiration'])
        holding_days = max((exp_dt - entry_dt).days, 1)
        annualized = (pnl_pct / holding_days) * 365

        conn.execute("""
            UPDATE trades SET
                exit_date = ?, exit_premium = 0, outcome = 'expired_worthless',
                pnl = ?, pnl_pct = ?, holding_days = ?,
                annualized_return = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (row['expiration'], round(pnl, 2), round(pnl_pct, 2),
              holding_days, round(annualized, 2), row['id']))
        count += 1
        logger.info(f"Expired worthless: {row['symbol']} ${row['strike']}P (PnL=${pnl:.2f})")

    return count


def _check_assignments(conn, client, account_hash):
    """
    Check account positions for stock that appeared from assignment.
    If we have an open put trade and the account now holds the underlying,
    mark the trade as assigned.
    """
    try:
        acct_resp = client.get_account(account_hash, fields='positions')
        if acct_resp.status_code != 200:
            return 0

        acct_data = acct_resp.json()
        positions = acct_data.get('securitiesAccount', {}).get('positions', [])

        stock_positions = {}
        for pos in positions:
            instrument = pos.get('instrument', {})
            if instrument.get('assetType') == 'EQUITY':
                symbol = instrument.get('symbol', '')
                qty = pos.get('longQuantity', 0)
                if qty >= 100:
                    stock_positions[symbol] = qty
    except Exception as e:
        logger.warning(f"Position check failed: {e}")
        return 0

    count = 0
    open_trades = conn.execute("""
        SELECT id, symbol, strike, entry_premium, contracts, capital_deployed, entry_date, expiration
        FROM trades WHERE outcome = 'open'
    """).fetchall()

    for trade in open_trades:
        symbol = trade['symbol']
        exp = trade['expiration']

        if symbol in stock_positions and exp <= date.today().isoformat():
            lots_assigned = min(stock_positions[symbol] // 100, trade['contracts'])
            if lots_assigned > 0:
                pnl = trade['entry_premium'] * 100 * trade['contracts']
                capital = trade['capital_deployed']
                pnl_pct = (pnl / capital * 100) if capital > 0 else 0
                entry_dt = datetime.fromisoformat(trade['entry_date'])
                holding_days = max((datetime.now() - entry_dt).days, 1)

                conn.execute("""
                    UPDATE trades SET
                        exit_date = ?, exit_premium = 0, outcome = 'assigned',
                        pnl = ?, pnl_pct = ?, holding_days = ?,
                        notes = 'Assigned - now holding shares', updated_at = datetime('now')
                    WHERE id = ?
                """, (exp, round(pnl, 2), round(pnl_pct, 2), holding_days, trade['id']))
                count += 1
                logger.info(f"Assignment detected: {symbol} {lots_assigned * 100} shares @ ${trade['strike']}")

    return count


# ==================== REPORTING ====================

def get_trade_stats(days=90):
    """Get aggregate trade statistics for the last N days."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    with get_db() as conn:
        closed = conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl >= 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(AVG(pnl), 2) as avg_pnl,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl_pct), 2) as avg_pnl_pct,
                ROUND(AVG(holding_days), 1) as avg_holding_days,
                ROUND(AVG(annualized_return), 2) as avg_annualized
            FROM trades
            WHERE outcome != 'open' AND exit_date >= ?
        """, (cutoff,)).fetchone()

        open_trades = conn.execute("""
            SELECT COUNT(*) as count, ROUND(SUM(capital_deployed), 2) as capital
            FROM trades WHERE outcome = 'open'
        """).fetchone()

        by_outcome = conn.execute("""
            SELECT outcome, COUNT(*) as count, ROUND(SUM(pnl), 2) as total_pnl
            FROM trades WHERE outcome != 'open' AND exit_date >= ?
            GROUP BY outcome
        """, (cutoff,)).fetchall()

    total = closed['total_trades'] or 0
    wins = closed['wins'] or 0

    return {
        'period_days': days,
        'total_closed': total,
        'win_rate': round(wins / total * 100, 1) if total > 0 else 0,
        'wins': wins,
        'losses': closed['losses'] or 0,
        'avg_pnl': closed['avg_pnl'] or 0,
        'total_pnl': closed['total_pnl'] or 0,
        'avg_pnl_pct': closed['avg_pnl_pct'] or 0,
        'avg_holding_days': closed['avg_holding_days'] or 0,
        'avg_annualized': closed['avg_annualized'] or 0,
        'open_count': open_trades['count'] or 0,
        'open_capital': open_trades['capital'] or 0,
        'by_outcome': [dict(r) for r in by_outcome],
    }


def get_score_vs_outcome(min_trades=5):
    """
    Analyze how scanner scores correlate with actual outcomes.
    Foundation for future outcome-calibrated scoring.

    Returns score buckets with win rate and avg PnL.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                CASE
                    WHEN r.grok_trade_score >= 90 THEN '90-100'
                    WHEN r.grok_trade_score >= 80 THEN '80-89'
                    WHEN r.grok_trade_score >= 70 THEN '70-79'
                    WHEN r.grok_trade_score >= 60 THEN '60-69'
                    ELSE '<60'
                END as score_bucket,
                COUNT(*) as trades,
                SUM(CASE WHEN t.pnl >= 0 THEN 1 ELSE 0 END) as wins,
                ROUND(AVG(t.pnl), 2) as avg_pnl,
                ROUND(AVG(t.pnl_pct), 2) as avg_pnl_pct,
                ROUND(AVG(t.annualized_return), 2) as avg_annualized
            FROM trades t
            JOIN recommendations r ON t.recommendation_id = r.id
            WHERE t.outcome != 'open'
            GROUP BY score_bucket
            HAVING trades >= ?
            ORDER BY score_bucket DESC
        """, (min_trades,)).fetchall()

    return [dict(r) for r in rows]


def get_recent_trades(limit=20):
    """Get recent trades for dashboard display."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, r.grok_trade_score, r.grok_recommendation, r.regime
            FROM trades t
            LEFT JOIN recommendations r ON t.recommendation_id = r.id
            ORDER BY t.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(r) for r in rows]


def get_open_trades():
    """Get all currently open trades."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, r.grok_trade_score, r.grok_recommendation, r.regime
            FROM trades t
            LEFT JOIN recommendations r ON t.recommendation_id = r.id
            WHERE t.outcome = 'open'
            ORDER BY t.expiration ASC
        """).fetchall()

    return [dict(r) for r in rows]


# ==================== HELPERS ====================

def _expiration_from_dte(dte):
    """Estimate expiration date from DTE."""
    return (date.today() + timedelta(days=dte)).isoformat()


def _parse_occ_symbol(occ_symbol):
    """
    Parse OCC option symbol into components.
    Format: SYMBOL  YYMMDDP00STRIKE (21 chars, symbol padded to 6)
    Example: AAPL  260319P00150000
    """
    if not occ_symbol or len(occ_symbol) < 15:
        return None

    try:
        symbol = occ_symbol[:6].strip()
        rest = occ_symbol[6:]
        date_str = rest[:6]
        exp_date = datetime.strptime(date_str, '%y%m%d').date().isoformat()
        put_call = rest[6]
        if put_call != 'P':
            return None
        strike = int(rest[7:15]) / 1000.0
        return {'symbol': symbol, 'expiration': exp_date, 'strike': strike}
    except Exception:
        return None


def _extract_fill_price(order):
    """Extract fill price from Schwab order data."""
    activities = order.get('orderActivityCollection', [])
    for activity in activities:
        legs = activity.get('executionLegs', [])
        for leg in legs:
            price = leg.get('price', 0)
            if price > 0:
                return float(price)
    return float(order.get('price', 0))


def _extract_fill_date(order):
    """Extract fill date from Schwab order data."""
    activities = order.get('orderActivityCollection', [])
    for activity in activities:
        legs = activity.get('executionLegs', [])
        for leg in legs:
            time_str = leg.get('time', '')
            if time_str:
                try:
                    return datetime.fromisoformat(time_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                except Exception:
                    pass

    for field in ['closeTime', 'enteredTime']:
        time_str = order.get(field, '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except Exception:
                pass

    return date.today().isoformat()


# Initialize DB on import
init_db()
