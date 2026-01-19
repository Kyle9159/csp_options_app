import sqlite3
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import json

DB_PATH = Path("data/trading_bot.db")


def initialize_enhanced_database():
    """
    Initialize enhanced database schema with all tables.
    Creates:
    - trades: Original table for trade history
    - opportunities: Scanner results (simple, leaps, 0dte)
    - cache_storage: Unified cache for all data
    - support_resistance: S/R levels by symbol
    - portfolio_snapshots: Historical portfolio tracking
    - greeks_history: Greeks trend analysis
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Ensure data directory exists
    DB_PATH.parent.mkdir(exist_ok=True)

    # Original trades table (if it doesn't exist)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            exit_date TEXT,
            position_type TEXT,
            strike REAL,
            expiration TEXT,
            entry_premium REAL,
            exit_premium REAL,
            contracts INTEGER,
            profit_loss REAL,
            status TEXT DEFAULT 'open',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Opportunities table - Consolidate scanner results
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanner_type TEXT NOT NULL,  -- 'simple', 'leaps', '0dte'
            symbol TEXT NOT NULL,
            strike REAL NOT NULL,
            expiration TEXT NOT NULL,
            dte INTEGER NOT NULL,
            premium REAL,
            delta REAL,
            iv REAL,
            grok_probability REAL,
            trade_score REAL,
            distance_from_price REAL,
            annualized_roi REAL,
            risk_reward_ratio REAL,
            liquidity_score REAL,
            underlying_price REAL,
            recommendation TEXT,
            capital_required REAL,
            raw_data TEXT,  -- JSON blob with all scanner data
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,  -- When this opportunity expires
            ttl_minutes INTEGER DEFAULT 1440  -- 24 hours default
        )
    """)

    # Create indexes for opportunities
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_opportunities_symbol
        ON opportunities(symbol)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_opportunities_scanner_type
        ON opportunities(scanner_type)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_opportunities_expires_at
        ON opportunities(expires_at)
    """)

    # Cache storage table - Replace JSON cache files
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cache_storage (
            cache_key TEXT PRIMARY KEY,
            cache_value TEXT NOT NULL,  -- JSON blob
            data_type TEXT,  -- 'quotes', 'grok', 'scanner', 'support_resistance'
            ttl_minutes INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_cache_expires_at
        ON cache_storage(expires_at)
    """)

    # Support/Resistance levels table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_resistance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe_months INTEGER NOT NULL,  -- 3, 6, 12
            support_level REAL,
            resistance_level REAL,
            current_price REAL,
            distance_to_support REAL,
            distance_to_resistance REAL,
            strength_score REAL,  -- 0-100, based on bounces
            calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, timeframe_months)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sr_symbol
        ON support_resistance(symbol)
    """)

    # Portfolio snapshots table - Historical tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date DATE NOT NULL UNIQUE,
            total_positions INTEGER NOT NULL,
            total_capital_at_risk REAL,
            portfolio_heat_pct REAL,
            portfolio_delta REAL,
            portfolio_theta REAL,
            portfolio_vega REAL,
            win_rate_pct REAL,
            total_pnl REAL,
            health_score REAL,  -- 0-100 composite score
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_date
        ON portfolio_snapshots(snapshot_date DESC)
    """)

    # Greeks history table - Track Greeks over time
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS greeks_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            strike REAL NOT NULL,
            expiration TEXT NOT NULL,
            underlying_price REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            iv REAL,
            dte INTEGER,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_greeks_symbol_exp
        ON greeks_history(symbol, expiration)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_greeks_recorded_at
        ON greeks_history(recorded_at DESC)
    """)

    conn.commit()
    conn.close()
    print("✅ Enhanced database schema initialized successfully")
    return True


def cleanup_expired_cache():
    """Remove expired cache entries"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM cache_storage
            WHERE expires_at < datetime('now')
        """)

        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted > 0:
            print(f"🧹 Cleaned up {deleted} expired cache entries")
        return deleted

    except Exception as e:
        print(f"Error cleaning up cache: {e}")
        return 0


def cleanup_expired_opportunities():
    """Remove expired opportunities"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM opportunities
            WHERE expires_at < datetime('now')
        """)

        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted > 0:
            print(f"🧹 Cleaned up {deleted} expired opportunities")
        return deleted

    except Exception as e:
        print(f"Error cleaning up opportunities: {e}")
        return 0

def get_recent_trades(limit: int = 10) -> List[Dict]:
    """Get recent trades from database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM trades
            ORDER BY entry_date DESC
            LIMIT ?
        """, (limit,))

        columns = [desc[0] for desc in cursor.description]
        trades = [dict(zip(columns, row)) for row in cursor.fetchall()]

        conn.close()
        return trades

    except Exception as e:
        print(f"Error getting recent trades: {e}")
        return []

def get_trade_performance_summary() -> Dict:
    """Get trade performance summary"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get basic stats
        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN profit_loss < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(profit_loss) as total_pnl,
                AVG(CASE WHEN profit_loss > 0 THEN profit_loss END) as avg_win,
                AVG(CASE WHEN profit_loss < 0 THEN profit_loss END) as avg_loss
            FROM trades
            WHERE status = 'closed'
        """)

        result = cursor.fetchone()
        conn.close()

        if result:
            total_trades = result[0] or 0
            winning_trades = result[1] or 0
            losing_trades = result[2] or 0
            total_pnl = result[3] or 0
            avg_win = result[4] or 0
            avg_loss = result[5] or 0

            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
            profit_factor = abs(avg_win * winning_trades / (avg_loss * losing_trades)) if avg_loss != 0 and losing_trades > 0 else 0

            return {
                'total_trades': total_trades,
                'win_rate': round(win_rate, 1),
                'total_pnl': round(total_pnl, 2),
                'profit_factor': round(profit_factor, 2),
                'expectancy': round((avg_win * win_rate/100) + (avg_loss * (100-win_rate)/100), 2)
            }

        return {
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'profit_factor': 0,
            'expectancy': 0
        }

    except Exception as e:
        print(f"Error getting performance summary: {e}")
        return {
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'profit_factor': 0,
            'expectancy': 0
        }