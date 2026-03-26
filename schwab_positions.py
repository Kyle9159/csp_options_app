"""
schwab_positions.py — Fetch live open CSP positions from Schwab account.

Used by both dashboard_server.py and generate_dashboard.py so both can
retrieve open positions without depending on Google Sheets.
"""

import os
import logging
import pandas as _pd
from datetime import date as _date

logger = logging.getLogger(__name__)


def parse_schwab_occ_symbol(occ_symbol: str) -> dict | None:
    """
    Parse a Schwab OCC option symbol into components.
    Format: 'AAPL  260319P00150000' (underlying padded to 6 chars)
    Returns dict with keys: symbol, expiration (ISO), strike (float), put_call.
    Returns None on parse failure.
    """
    try:
        if not occ_symbol or len(occ_symbol) < 15:
            return None
        from datetime import datetime as _dt

        underlying = occ_symbol[:6].strip()
        rest = occ_symbol[6:]
        exp_date = _dt.strptime(rest[:6], "%y%m%d").date().isoformat()
        put_call = rest[6]
        if put_call not in ("P", "C"):
            return None
        strike = int(rest[7:15]) / 1000.0
        return {"symbol": underlying, "expiration": exp_date, "strike": strike, "put_call": put_call}
    except Exception:
        return None


def get_schwab_csp_positions() -> list[dict]:
    """
    Fetch live short PUT positions from the Schwab account via the positions API.

    Returns a list of dicts with the following keys:
        Symbol, Strike, Exp Date, Entry Premium, Contracts Qty, Option Symbol,
        _dte, _current_premium, _current_mark, _bid, _ask, Current Mark,
        _pl_dollars, _progress_pct, _total_credit,
        _daily_theta_decay_dollars, _forward_theta_daily, _projected_decay

    Returns [] on any error so callers can gracefully handle unavailability.
    """
    try:
        import dotenv as _dotenv
        _dotenv.load_dotenv()
        from schwab_utils import get_client

        paper_trading = os.getenv("PAPER_TRADING", "True").lower() == "true"
        account_id = os.getenv(
            "SCHWAB_PAPER_ACCOUNT_ID" if paper_trading else "SCHWAB_LIVE_ACCOUNT_ID"
        )

        client = get_client()
        client.set_enforce_enums(False)

        # Resolve account hash
        acct_numbers = client.get_account_numbers().json()
        account_hash = next(
            (a.get("hashValue") for a in acct_numbers if a.get("accountNumber") == account_id),
            None,
        )
        if not account_hash:
            logger.warning("get_schwab_csp_positions: no account hash found for %s", account_id)
            return []

        # Fetch positions
        pos_resp = client.get_account(account_hash, fields="positions")
        if pos_resp.status_code != 200:
            logger.warning("Schwab positions returned HTTP %s", pos_resp.status_code)
            return []

        positions = pos_resp.json().get("securitiesAccount", {}).get("positions", [])

        rows = []
        for pos in positions:
            instrument = pos.get("instrument", {})
            if instrument.get("assetType") != "OPTION":
                continue
            if instrument.get("putCall") != "PUT":
                continue
            short_qty = int(pos.get("shortQuantity", 0))
            if short_qty <= 0:
                continue

            occ_symbol = instrument.get("symbol", "")
            parsed = parse_schwab_occ_symbol(occ_symbol)
            if not parsed:
                continue

            underlying = instrument.get("underlyingSymbol", "") or parsed["symbol"]
            avg_price = float(pos.get("averagePrice", 0))     # premium received per share
            market_value = float(pos.get("marketValue", 0))   # negative for short

            # Earnings proximity check — warns if earnings land before expiration
            earnings_risk = False
            days_to_earnings: int | None = None
            try:
                import yfinance as _yf
                cal = _yf.Ticker(underlying).calendar
                if cal is not None and 'Earnings Date' in cal:
                    earn_dates = cal['Earnings Date']
                    if isinstance(earn_dates, list) and earn_dates:
                        next_earn = _pd.to_datetime(earn_dates[0])
                        days_to_earnings = (next_earn - _pd.Timestamp.now()).days
                        earnings_risk = days_to_earnings is not None and days_to_earnings < 14
            except Exception:
                pass

            exp_date_str = parsed["expiration"]   # ISO format: YYYY-MM-DD
            strike = parsed["strike"]

            dte = max((_date.fromisoformat(exp_date_str) - _date.today()).days, 0)

            # Current mark derived from market value — avoids an extra API round-trip
            current_mark = abs(market_value) / (short_qty * 100) if short_qty > 0 else 0.0
            total_credit = avg_price * short_qty * 100
            pl_dollars = total_credit - abs(market_value)
            progress_pct = (pl_dollars / total_credit * 100) if total_credit > 0 else 0.0

            rows.append(
                {
                    # Primary trade fields
                    "Symbol": underlying,
                    "Strike": strike,
                    "Exp Date": exp_date_str,
                    "Entry Premium": avg_price,
                    "Contracts Qty": short_qty,
                    "Quantity": short_qty,           # sheet-compat alias
                    "Option Symbol": occ_symbol,
                    # Live pricing — both naming conventions for compat
                    "_dte": dte,
                    "DTE": dte,
                    "_current_premium": round(current_mark, 2),
                    "_current_mark": round(current_mark, 2),   # compat with enrich_trade_with_live_data output
                    "Current Mark": round(current_mark, 2),
                    "Current Premium": round(current_mark, 2),
                    "_bid": 0.0,
                    "_ask": 0.0,
                    "Bid": 0.0,
                    "Ask": 0.0,
                    # P&L
                    "_pl_dollars": round(pl_dollars, 2),
                    "Current P/L": round(pl_dollars, 2),
                    "_progress_pct": round(progress_pct, 1),
                    "_total_credit": round(total_credit, 2),
                    # Theta — not available from positions endpoint
                    "_daily_theta_decay_dollars": 0.0,
                    "_forward_theta_daily": 0.0,
                    "_projected_decay": 0.0,
                    # Greeks — not in positions response; routes fall back to live API
                    "Delta": 0.0,
                    "Gamma": 0.0,
                    "Theta": 0.0,
                    "Vega": 0.0,
                    "IV": 0.0,
                    "Current Price": 0.0,
                    "Underlying Price": 0.0,
                    "Days Since Entry": 0,
                    # Earnings risk enrichment
                    "_earnings_risk": earnings_risk,
                    "_days_to_earnings": days_to_earnings,
                }
            )

        logger.info("Fetched %d open short PUT positions from Schwab", len(rows))
        return rows

    except Exception as e:
        logger.error("get_schwab_csp_positions failed: %s", e, exc_info=True)
        return []


def get_open_positions_as_df():
    """
    Return open CSP positions as a (DataFrame, None) tuple to match the
    historical load_trades_from_sheet() API shape.

    Returns (None, None) when no positions are found or on error.
    """
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas not installed — cannot build positions DataFrame")
        return None, None

    rows = get_schwab_csp_positions()
    if not rows:
        return None, None

    df = pd.DataFrame(rows)
    return df, None
