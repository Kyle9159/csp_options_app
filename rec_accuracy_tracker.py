"""
Recommendation Accuracy Tracker — top-N daily flagging, live price snapshots,
and outcome analysis for CSP scanner recommendations.

Flow:
    1. scanner runs → log_recommendations() (in trade_outcome_tracker.py)
    2. mark_daily_top_n() — keeps the best Top N recs for the day across all
                                                    same-day scans, replacing weaker earlier entries
    3. snapshot_top_ranked_recs() — polls Schwab quotes for each active flagged
                                                                    rec and records premium decay + live Greeks
    4. compute_outcome() — walks snapshots → fills rec_outcomes row
    Analysis query functions return data for /analysis page.
"""

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from contextlib import contextmanager

import dotenv
dotenv.load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = Path("cache_files") / "trade_outcomes.db"

# ── Profit-target thresholds tracked ──────────────────────────────────────────
PROFIT_TARGETS = [40, 50, 60, 70]

# ── Tracking window ───────────────────────────────────────────────────────────
TRACKING_DAYS = 21

try:
    DAILY_TOP_N = max(1, int(os.getenv("REC_ACCURACY_TOP_N", "10")))
except ValueError:
    DAILY_TOP_N = 10


@contextmanager
def get_db():
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


# =============================================================================
# PHASE 1 — SCHEMA MIGRATION
# =============================================================================

def init_db():
    """
    Idempotently migrate the existing trade_outcomes.db:
      • Add new columns to recommendations
      • Create rec_snapshots table
      • Create rec_outcomes table
    Safe to call on every server start.
    """
    _migrate_recommendations_columns()
    _create_snapshot_tables()
    logger.info("rec_accuracy_tracker: DB schema ready")


def _migrate_recommendations_columns():
    """Add new columns to recommendations if they don't exist."""
    new_cols = [
        "scan_time TEXT DEFAULT ''",
        "day_of_week TEXT DEFAULT ''",
        "sector TEXT DEFAULT ''",
        "underlying_price REAL DEFAULT 0",
        "bid REAL DEFAULT 0",
        "ask REAL DEFAULT 0",
        "open_interest INTEGER DEFAULT 0",
        "volume INTEGER DEFAULT 0",
        "bid_ask_spread_pct REAL DEFAULT 0",
        "gamma REAL DEFAULT 0",
        "theta REAL DEFAULT 0",
        "vega REAL DEFAULT 0",
        "iv_rank REAL DEFAULT 0",
        "is_top5_daily INTEGER DEFAULT 0",
        "daily_top_rank INTEGER",
    ]
    with get_db() as conn:
        for col_def in new_cols:
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE recommendations ADD COLUMN {col_def}")
                logger.debug("Added column '%s' to recommendations", col_name)
            except sqlite3.OperationalError:
                pass  # already exists


def _create_snapshot_tables():
    """Create rec_snapshots and rec_outcomes tables."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rec_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_id INTEGER REFERENCES recommendations(id),
                snapshot_date TEXT NOT NULL,
                days_since_rec INTEGER NOT NULL,
                current_mid REAL DEFAULT 0,
                current_bid REAL DEFAULT 0,
                current_ask REAL DEFAULT 0,
                underlying_price REAL DEFAULT 0,
                pct_profit REAL DEFAULT 0,
                current_delta REAL DEFAULT 0,
                current_gamma REAL DEFAULT 0,
                current_theta REAL DEFAULT 0,
                current_vega REAL DEFAULT 0,
                current_iv REAL DEFAULT 0,
                current_dte INTEGER DEFAULT 0,
                is_expired INTEGER DEFAULT 0,
                data_source TEXT DEFAULT 'schwab',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_rec_date
                ON rec_snapshots(recommendation_id, snapshot_date);

            CREATE TABLE IF NOT EXISTS rec_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_id INTEGER UNIQUE REFERENCES recommendations(id),
                tracking_complete INTEGER DEFAULT 0,
                final_pct_profit REAL,
                max_pct_profit REAL,
                min_pct_profit REAL,
                days_to_40pct INTEGER,
                days_to_50pct INTEGER,
                days_to_60pct INTEGER,
                days_to_70pct INTEGER,
                would_have_won INTEGER DEFAULT 0,
                expired_worthless INTEGER DEFAULT 0,
                snapshot_count INTEGER DEFAULT 0,
                computed_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_rec_outcomes_rec
                ON rec_outcomes(recommendation_id);
        """)


# =============================================================================
# PHASE 4 — TOP-N FLAGGING
# =============================================================================

def _daily_rec_key(row) -> str:
    occ_symbol = (row["occ_symbol"] or "").strip()
    if occ_symbol:
        return occ_symbol
    return f"{row['symbol']}|{row['expiration']}|{float(row['strike'] or 0):.3f}"


def _daily_sort_key(row) -> tuple:
    return (
        float(row["grok_trade_score"] or 0),
        float(row["improved_put_score"] or 0),
        float(row["annualized_roi"] or 0),
        row["scan_time"] or "",
        int(row["id"] or 0),
    )


def mark_daily_top_n(scan_date: str = None, top_n: int = DAILY_TOP_N) -> int:
    """
    Keep the best Top N recommendations for scan_date across all scans that day.

    If the same contract is scanned multiple times in one day, only the
    highest-scored version is eligible for the daily Top N. A later stronger
    scan can therefore replace a weaker earlier entry from that day's list.

    Ranking priority:
      1. grok_trade_score
      2. improved_put_score
      3. annualized_roi
      4. later scan_time
      5. later id

    Returns the number of recs flagged (≤ top_n).
    """
    scan_date = scan_date or date.today().isoformat()
    top_n = max(1, int(top_n or DAILY_TOP_N))

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, strike, expiration, occ_symbol, scan_time,
                   grok_trade_score, improved_put_score, annualized_roi
            FROM recommendations
            WHERE scan_date = ?
            """,
            (scan_date,),
        ).fetchall()

        conn.execute(
            "UPDATE recommendations SET is_top5_daily = 0, daily_top_rank = NULL WHERE scan_date = ?",
            (scan_date,),
        )

        best_by_contract = {}
        for row in rows:
            rec_key = _daily_rec_key(row)
            existing = best_by_contract.get(rec_key)
            if existing is None or _daily_sort_key(row) > _daily_sort_key(existing):
                best_by_contract[rec_key] = row

        selected_rows = sorted(
            best_by_contract.values(),
            key=_daily_sort_key,
            reverse=True,
        )[:top_n]

        for rank, row in enumerate(selected_rows, start=1):
            conn.execute(
                "UPDATE recommendations SET is_top5_daily = 1, daily_top_rank = ? WHERE id = ?",
                (rank, row["id"]),
            )

        count = len(selected_rows)

    logger.info("mark_daily_top_n: flagged %d top-%d recs for %s", count, top_n, scan_date)
    return count


def mark_daily_top5(scan_date: str = None) -> int:
    """Backward-compatible wrapper; default daily cutoff is now Top 10."""
    return mark_daily_top_n(scan_date=scan_date, top_n=DAILY_TOP_N)


# =============================================================================
# PHASE 4 — SNAPSHOT ENGINE
# =============================================================================

def snapshot_top_ranked_recs() -> dict:
    """
    For every active flagged daily rec (within TRACKING_DAYS, not expired, not already
    snapshotted today) poll Schwab for the current option quote and Greeks,
    record a rec_snapshots row, then recompute the outcome row.

    Returns summary dict.
    """
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=TRACKING_DAYS)).isoformat()

    with get_db() as conn:
        active_recs = conn.execute(
            """
            SELECT id, symbol, occ_symbol, strike, expiration, premium,
                   scan_date, delta, gamma, theta, vega, iv_rank
            FROM recommendations
            WHERE is_top5_daily = 1
              AND scan_date >= ?
              AND expiration >= ?
                        ORDER BY scan_date DESC, COALESCE(daily_top_rank, overall_rank) ASC, grok_trade_score DESC
            """,
            (cutoff, today),
        ).fetchall()

    snapped = 0
    skipped = 0
    errors = []

    client = _get_schwab_client()

    for rec in active_recs:
        rec_id = rec["id"]
        occ = rec["occ_symbol"]
        entry_premium = rec["premium"]
        scan_date = rec["scan_date"]
        expiration = rec["expiration"]

        # Skip if already snapshotted today
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM rec_snapshots WHERE recommendation_id = ? AND snapshot_date = ?",
                (rec_id, today),
            ).fetchone()
        if existing:
            skipped += 1
            continue

        days_since = (date.today() - date.fromisoformat(scan_date)).days
        exp_date = date.fromisoformat(expiration)
        current_dte = max((exp_date - date.today()).days, 0)

        try:
            quote_data, source = _fetch_option_quote(client, rec, occ)
            if quote_data is None:
                errors.append(f"rec {rec_id} ({rec['symbol']}): no quote data")
                continue

            current_mid = quote_data.get("mid", 0)
            current_bid = quote_data.get("bid", 0)
            current_ask = quote_data.get("ask", 0)
            underlying_px = quote_data.get("underlying_price", 0)
            c_delta = quote_data.get("delta", 0)
            c_gamma = quote_data.get("gamma", 0)
            c_theta = quote_data.get("theta", 0)
            c_vega = quote_data.get("vega", 0)
            c_iv = quote_data.get("iv", 0)
            is_expired = 1 if current_dte == 0 else 0

            # Profit % for a short put: positive = we're winning
            if entry_premium > 0:
                pct_profit = round((entry_premium - current_mid) / entry_premium * 100, 2)
            else:
                pct_profit = 0.0

            with get_db() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rec_snapshots (
                        recommendation_id, snapshot_date, days_since_rec,
                        current_mid, current_bid, current_ask, underlying_price,
                        pct_profit, current_delta, current_gamma, current_theta,
                        current_vega, current_iv, current_dte, is_expired, data_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec_id, today, days_since,
                        current_mid, current_bid, current_ask, underlying_px,
                        pct_profit, c_delta, c_gamma, c_theta,
                        c_vega, c_iv, current_dte, is_expired, source,
                    ),
                )
            snapped += 1
            logger.info(
                "Snapshot: %s rec#%d D+%d pct_profit=%.1f%%",
                rec["symbol"], rec_id, days_since, pct_profit,
            )

            # Immediately recompute outcome
            compute_outcome(rec_id)

        except Exception as e:
            errors.append(f"rec {rec_id} ({rec['symbol']}): {e}")
            logger.warning("Snapshot error rec %d: %s", rec_id, e)

    result = {
        "snapped": snapped,
        "skipped_already_done": skipped,
        "errors": errors[:10],
    }
    logger.info("snapshot_top_ranked_recs complete: %s", result)
    return result


def snapshot_top5_recs() -> dict:
    """Backward-compatible wrapper for older imports."""
    return snapshot_top_ranked_recs()


def _get_schwab_client():
    """Lazily init Schwab client, return None if unavailable."""
    try:
        from schwab_utils import get_client
        c = get_client()
        c.set_enforce_enums(False)
        return c
    except Exception as e:
        logger.warning("Schwab client unavailable for snapshots: %s", e)
        return None


def _fetch_option_quote(client, rec: dict, occ: str) -> tuple:
    """
    Try Schwab get_quotes first, fall back to yfinance.
    Returns (quote_dict, source_str) or (None, None).
    """
    # --- Schwab path ---
    if client and occ:
        try:
            resp = client.get_quotes([occ])
            if resp.status_code == 200:
                data = resp.json()
                opt = data.get(occ, data.get(occ.strip(), {}))
                if opt:
                    q = opt.get("quote", {})
                    r = opt.get("reference", {})
                    bid = float(q.get("bidPrice", 0) or 0)
                    ask = float(q.get("askPrice", 0) or 0)
                    mid = (bid + ask) / 2 if bid or ask else float(q.get("mark", 0) or 0)
                    underlying_px = float(q.get("underlyingPrice", 0) or 0)
                    delta = abs(float(opt.get("Greeks", {}).get("delta", 0) or q.get("delta", 0) or 0))
                    gamma = float(opt.get("Greeks", {}).get("gamma", 0) or q.get("gamma", 0) or 0)
                    theta = float(opt.get("Greeks", {}).get("theta", 0) or q.get("theta", 0) or 0)
                    vega = float(opt.get("Greeks", {}).get("vega", 0) or q.get("vega", 0) or 0)
                    iv = float(opt.get("Greeks", {}).get("volatility", 0) or q.get("volatility", 0) or 0)
                    return {
                        "bid": bid, "ask": ask, "mid": mid,
                        "underlying_price": underlying_px,
                        "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "iv": iv,
                    }, "schwab"
        except Exception as e:
            logger.debug("Schwab quote failed for %s: %s", occ, e)

    # --- yfinance fallback ---
    return _yfinance_fallback(rec)


def _yfinance_fallback(rec: dict) -> tuple:
    """Fetch current option mid from yfinance using stored strike + expiration."""
    try:
        import yfinance as yf
        symbol = rec["symbol"]
        strike = float(rec["strike"])
        expiration = rec["expiration"]  # YYYY-MM-DD

        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiration)
        puts = chain.puts
        underlying_px = float(ticker.history(period="1d")["Close"].iloc[-1]) if not ticker.history(period="1d").empty else 0

        row = puts[abs(puts["strike"] - strike) < 0.01]
        if row.empty:
            row = puts.iloc[(puts["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return None, None

        r = row.iloc[0]
        bid = float(r.get("bid", 0) or 0)
        ask = float(r.get("ask", 0) or 0)
        mid = (bid + ask) / 2 if bid or ask else 0
        iv = float(r.get("impliedVolatility", 0) or 0)
        delta = abs(float(r.get("delta", 0) or 0))
        gamma = float(r.get("gamma", 0) or 0)
        theta = float(r.get("theta", 0) or 0)
        vega = float(r.get("vega", 0) or 0)

        return {
            "bid": bid, "ask": ask, "mid": mid,
            "underlying_price": underlying_px,
            "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "iv": iv,
        }, "yfinance"
    except Exception as e:
        logger.debug("yfinance fallback failed for rec %d: %s", rec.get("id", "?"), e)
        return None, None


# =============================================================================
# PHASE 4 — OUTCOME COMPUTATION
# =============================================================================

def compute_outcome(rec_id: int):
    """
    Walk all snapshots for a rec and (re)compute the rec_outcomes row.
    Called after every new snapshot.
    """
    with get_db() as conn:
        # Load original entry premium
        rec = conn.execute(
            "SELECT premium, expiration, scan_date FROM recommendations WHERE id = ?",
            (rec_id,),
        ).fetchone()
        if not rec:
            return

        snapshots = conn.execute(
            """
            SELECT days_since_rec, pct_profit, is_expired
            FROM rec_snapshots
            WHERE recommendation_id = ?
            ORDER BY days_since_rec ASC
            """,
            (rec_id,),
        ).fetchall()

    if not snapshots:
        return

    profits = [s["pct_profit"] for s in snapshots]
    max_profit = max(profits)
    min_profit = min(profits)
    final_profit = profits[-1]
    snapshot_count = len(snapshots)

    # Days to each target
    days_map = {}
    for target in PROFIT_TARGETS:
        days_map[target] = None
        for s in snapshots:
            if s["pct_profit"] >= target:
                days_map[target] = s["days_since_rec"]
                break

    would_have_won = 1 if days_map[50] is not None else 0
    any_expired = any(s["is_expired"] for s in snapshots)
    last_days = snapshots[-1]["days_since_rec"]
    tracking_complete = 1 if last_days >= TRACKING_DAYS or any_expired else 0

    # Check if expired worthless (final snapshot has mid near 0 and expired)
    expired_worthless = 1 if any_expired and profits[-1] >= 95 else 0

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rec_outcomes (
                recommendation_id, tracking_complete, final_pct_profit,
                max_pct_profit, min_pct_profit,
                days_to_40pct, days_to_50pct, days_to_60pct, days_to_70pct,
                would_have_won, expired_worthless, snapshot_count, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(recommendation_id) DO UPDATE SET
                tracking_complete    = excluded.tracking_complete,
                final_pct_profit     = excluded.final_pct_profit,
                max_pct_profit       = excluded.max_pct_profit,
                min_pct_profit       = excluded.min_pct_profit,
                days_to_40pct        = excluded.days_to_40pct,
                days_to_50pct        = excluded.days_to_50pct,
                days_to_60pct        = excluded.days_to_60pct,
                days_to_70pct        = excluded.days_to_70pct,
                would_have_won       = excluded.would_have_won,
                expired_worthless    = excluded.expired_worthless,
                snapshot_count       = excluded.snapshot_count,
                computed_at          = datetime('now')
            """,
            (
                rec_id, tracking_complete, round(final_profit, 2),
                round(max_profit, 2), round(min_profit, 2),
                days_map[40], days_map[50], days_map[60], days_map[70],
                would_have_won, expired_worthless, snapshot_count,
            ),
        )


# =============================================================================
# PHASE 5 — ANALYSIS QUERY FUNCTIONS
# =============================================================================

def get_summary_stats() -> dict:
    """Overall tracker summary card data."""
    with get_db() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) as total_recs,
                SUM(CASE WHEN o.would_have_won = 1 THEN 1 ELSE 0 END) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars,
                COUNT(CASE WHEN o.tracking_complete = 1 THEN 1 END) as complete_count,
                COUNT(CASE WHEN o.tracking_complete = 0 THEN 1 END) as still_tracking
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            """
        ).fetchone()

        total_premium = conn.execute(
            """
            SELECT ROUND(SUM(r.premium * 100), 2) as total
            FROM recommendations r
            WHERE r.is_top5_daily = 1
            """
        ).fetchone()

        # Hit rates per target
        hit_rates = {}
        for target in PROFIT_TARGETS:
            col = f"days_to_{target}pct"
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN o.{col} IS NOT NULL THEN 1 ELSE 0 END) as hits,
                    ROUND(AVG(o.{col}), 1) as avg_days,
                    (SELECT CAST(o2.{col} AS REAL) FROM rec_outcomes o2
                     JOIN recommendations r2 ON r2.id = o2.recommendation_id
                     WHERE r2.is_top5_daily = 1 AND o2.{col} IS NOT NULL
                     ORDER BY o2.{col}
                     LIMIT 1 OFFSET (
                         SELECT COUNT(*) FROM rec_outcomes o3
                         JOIN recommendations r3 ON r3.id = o3.recommendation_id
                         WHERE r3.is_top5_daily = 1 AND o3.{col} IS NOT NULL
                     ) / 2
                    ) as median_days
                FROM recommendations r
                JOIN rec_outcomes o ON o.recommendation_id = r.id
                WHERE r.is_top5_daily = 1
                """
            ).fetchone()
            total = row["total"] or 0
            hits = row["hits"] or 0
            hit_rates[target] = {
                "hit_rate_pct": round(hits / total * 100, 1) if total > 0 else 0,
                "avg_days": row["avg_days"],
                "median_days": row["median_days"],
                "hits": hits,
                "total": total,
            }

    total = totals["total_recs"] or 0
    wins = totals["wins"] or 0
    return {
        "total_recs_tracked": total,
        "win_rate_pct": round(wins / total * 100, 1) if total > 0 else 0,
        "avg_days_to_50pct": totals["avg_days_to_50"],
        "avg_max_profit_pct": totals["avg_max_profit"],
        "avg_premium_dollars": totals["avg_premium_dollars"] or 0,
        "total_premium_tracked": total_premium["total"] or 0,
        "tracking_complete_count": totals["complete_count"] or 0,
        "still_tracking_count": totals["still_tracking"] or 0,
        "profit_targets": hit_rates,
    }


def get_score_calibration() -> list:
    """
    Win rate + avg days to 50% by daily top rank and grok_trade_score bucket.
    Answers: "Is daily rank #1 actually earning more than rank #10?"
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                COALESCE(r.daily_top_rank, r.overall_rank) as overall_rank,
                CASE
                    WHEN r.grok_trade_score >= 95 THEN '95-100'
                    WHEN r.grok_trade_score >= 90 THEN '90-94'
                    WHEN r.grok_trade_score >= 85 THEN '85-89'
                    WHEN r.grok_trade_score >= 80 THEN '80-84'
                    WHEN r.grok_trade_score >= 70 THEN '70-79'
                    ELSE '<70'
                END as score_bucket,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY COALESCE(r.daily_top_rank, r.overall_rank), score_bucket
            ORDER BY COALESCE(r.daily_top_rank, r.overall_rank) ASC, score_bucket DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_by_symbol() -> list:
    """Win rate, avg premium, avg days to 50% by underlying symbol."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                r.symbol,
                r.sector,
                r.tier,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars,
                ROUND(AVG(r.annualized_roi), 1) as avg_annualized_roi,
                ROUND(AVG(r.iv_rank), 1) as avg_iv_rank,
                ROUND(AVG(r.delta), 3) as avg_delta
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY r.symbol
            ORDER BY rec_count DESC, wins DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_by_sector() -> list:
    """Win rate and averages grouped by GICS sector."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(r.sector, ''), 'Unknown') as sector,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars,
                ROUND(AVG(r.annualized_roi), 1) as avg_annualized_roi
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY sector
            ORDER BY rec_count DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_by_day_of_week() -> list:
    """Rec count and win rate by day the recommendation was generated."""
    order = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(r.day_of_week, ''), 'Unknown') as day_of_week,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars,
                ROUND(AVG(r.grok_trade_score), 1) as avg_grok_score
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY day_of_week
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)

    result.sort(key=lambda x: order.get(x["day_of_week"], 9))
    return result


def get_by_regime() -> list:
    """Win rate and avg days by market regime at time of recommendation."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                r.regime,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY r.regime
            ORDER BY rec_count DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_iv_rank_analysis() -> list:
    """Win rate segmented by IV Rank at time of recommendation."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN r.iv_rank >= 80 THEN '80-100 (Very High)'
                    WHEN r.iv_rank >= 70 THEN '70-79 (High)'
                    WHEN r.iv_rank >= 50 THEN '50-69 (Elevated)'
                    WHEN r.iv_rank >= 30 THEN '30-49 (Normal)'
                    ELSE '<30 (Low)'
                END as iv_rank_bucket,
                CAST(CASE
                    WHEN r.iv_rank >= 80 THEN 4
                    WHEN r.iv_rank >= 70 THEN 3
                    WHEN r.iv_rank >= 50 THEN 2
                    WHEN r.iv_rank >= 30 THEN 1
                    ELSE 0
                END AS INTEGER) as sort_order,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit,
                ROUND(AVG(r.iv_rank), 1) as avg_iv_rank
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY iv_rank_bucket
            ORDER BY sort_order DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_greeks_analysis() -> list:
    """
    Win rate, avg theta at entry, and avg days to profit segmented by delta bucket.
    Shows optimal delta ranges.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN r.delta >= 0.30 THEN '0.30+ (Aggressive)'
                    WHEN r.delta >= 0.25 THEN '0.25-0.30 (Moderate)'
                    WHEN r.delta >= 0.20 THEN '0.20-0.25 (Conservative)'
                    ELSE '<0.20 (Very Conservative)'
                END as delta_bucket,
                CAST(CASE
                    WHEN r.delta >= 0.30 THEN 3
                    WHEN r.delta >= 0.25 THEN 2
                    WHEN r.delta >= 0.20 THEN 1
                    ELSE 0
                END AS INTEGER) as sort_order,
                COUNT(*) as rec_count,
                SUM(o.would_have_won) as wins,
                ROUND(AVG(r.delta), 3) as avg_delta,
                ROUND(AVG(r.gamma), 4) as avg_gamma,
                ROUND(AVG(r.theta), 4) as avg_theta,
                ROUND(AVG(r.vega), 4) as avg_vega,
                ROUND(AVG(r.iv_rank), 1) as avg_iv_rank,
                ROUND(AVG(r.premium * 100), 2) as avg_premium_dollars,
                ROUND(AVG(o.days_to_50pct), 1) as avg_days_to_50,
                ROUND(AVG(o.max_pct_profit), 1) as avg_max_profit
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            GROUP BY delta_bucket
            ORDER BY sort_order DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rc = d["rec_count"] or 0
        d["win_rate_pct"] = round((d["wins"] or 0) / rc * 100, 1) if rc > 0 else 0
        result.append(d)
    return result


def get_profit_timeline() -> dict:
    """
    For each profit target (40/50/60/70%), return distribution stats on
    how many days it takes to reach that target, plus hit rate.
    """
    with get_db() as conn:
        totals_row = conn.execute(
            """
            SELECT COUNT(*) as total
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            """
        ).fetchone()
        total = totals_row["total"] if totals_row else 0

        result = {"total_recs": total, "targets": {}}
        for target in PROFIT_TARGETS:
            col = f"days_to_{target}pct"
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) as total_with_outcome,
                    SUM(CASE WHEN o.{col} IS NOT NULL THEN 1 ELSE 0 END) as hits,
                    ROUND(AVG(o.{col}), 1) as avg_days,
                    MIN(o.{col}) as min_days,
                    MAX(o.{col}) as max_days
                FROM recommendations r
                JOIN rec_outcomes o ON o.recommendation_id = r.id
                WHERE r.is_top5_daily = 1
                  AND o.tracking_complete = 1
                """
            ).fetchone()

            # percentile buckets: ≤7d, 8-14d, 15-21d
            buckets = {}
            for label, lo, hi in [("1-7d", 1, 7), ("8-14d", 8, 14), ("15-21d", 15, 21)]:
                b = conn.execute(
                    f"""
                    SELECT COUNT(*) as c
                    FROM recommendations r
                    JOIN rec_outcomes o ON o.recommendation_id = r.id
                    WHERE r.is_top5_daily = 1
                      AND o.{col} BETWEEN ? AND ?
                    """,
                    (lo, hi),
                ).fetchone()
                buckets[label] = b["c"] if b else 0

            hits = row["hits"] or 0
            tw = row["total_with_outcome"] or 0
            result["targets"][target] = {
                "hit_rate_pct": round(hits / tw * 100, 1) if tw > 0 else 0,
                "hits": hits,
                "total_complete": tw,
                "avg_days": row["avg_days"],
                "min_days": row["min_days"],
                "max_days": row["max_days"],
                "day_buckets": buckets,
            }

    return result


def get_recs_with_outcomes(limit: int = 50, offset: int = 0) -> dict:
    """
    Paginated recommendation detail: joins recommendations + rec_outcomes
    + latest snapshot data.
    """
    with get_db() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            """
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT
                r.id, r.scan_date, r.day_of_week, r.scan_time,
                r.symbol, r.sector, r.tier, r.strike, r.expiration,
                r.dte, r.premium, r.underlying_price,
                r.delta, r.gamma, r.theta, r.vega,
                r.iv, r.iv_rank, r.annualized_roi,
                r.grok_trade_score, r.grok_recommendation,
                COALESCE(r.daily_top_rank, r.overall_rank) as overall_rank, r.regime,
                r.open_interest, r.volume, r.bid_ask_spread_pct,
                r.rebound_score, r.sr_risk_flag,
                o.tracking_complete, o.final_pct_profit, o.max_pct_profit,
                o.min_pct_profit, o.would_have_won, o.expired_worthless,
                o.days_to_40pct, o.days_to_50pct, o.days_to_60pct, o.days_to_70pct,
                o.snapshot_count,
                (SELECT pct_profit FROM rec_snapshots
                 WHERE recommendation_id = r.id
                 ORDER BY snapshot_date DESC LIMIT 1) as latest_pct_profit,
                (SELECT current_delta FROM rec_snapshots
                 WHERE recommendation_id = r.id
                 ORDER BY snapshot_date DESC LIMIT 1) as latest_delta,
                (SELECT underlying_price FROM rec_snapshots
                 WHERE recommendation_id = r.id
                 ORDER BY snapshot_date DESC LIMIT 1) as latest_underlying_price,
                (SELECT snapshot_date FROM rec_snapshots
                 WHERE recommendation_id = r.id
                 ORDER BY snapshot_date DESC LIMIT 1) as last_snapshot_date
            FROM recommendations r
            JOIN rec_outcomes o ON o.recommendation_id = r.id
            WHERE r.is_top5_daily = 1
            ORDER BY r.scan_date DESC, COALESCE(r.daily_top_rank, r.overall_rank) ASC, r.grok_trade_score DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": [dict(r) for r in rows],
    }


def get_snapshot_history(rec_id: int) -> list:
    """Return all snapshots for a single rec (for sparkline/detail view)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date, days_since_rec, current_mid, pct_profit,
                   current_delta, current_gamma, current_theta, current_vega,
                   current_iv, current_dte, underlying_price, data_source
            FROM rec_snapshots
            WHERE recommendation_id = ?
            ORDER BY days_since_rec ASC
            """,
            (rec_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# CONVENIENCE — called from dashboard
# =============================================================================

def run_snapshot_job() -> dict:
    """Entry point for background thread call from Flask."""
    try:
        result = snapshot_top_ranked_recs()
        return {"ok": True, "data": result}
    except Exception as e:
        logger.error("Snapshot job failed: %s", e)
        return {"ok": False, "error": str(e)}


# Auto-migrate on import
try:
    DB_PATH.parent.mkdir(exist_ok=True)
    init_db()
except Exception as _e:
    logger.warning("rec_accuracy_tracker init_db failed: %s", _e)
