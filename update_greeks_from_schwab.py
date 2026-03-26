"""
update_greeks_from_schwab.py - Update Greeks in SQLite from Schwab API

Fetches live Greeks from Schwab API for all open trades stored in the
SQLite database and writes delta, gamma, theta, vega back to it.

Usage:
    python update_greeks_from_schwab.py
"""

from schwab_utils import get_client
from schwab.client import Client
from config import logger
from datetime import datetime
from helper_functions import safe_float
from trade_outcome_tracker import get_open_trades, get_db


def get_option_greeks(symbol, strike, expiration, underlying_price):
    """
    Fetch Greeks for a specific option from Schwab API.

    Args:
        symbol: Underlying symbol (e.g., 'AAPL')
        strike: Strike price
        expiration: Expiration date string (MM/DD/YYYY or YYYY-MM-DD)
        underlying_price: Current underlying price

    Returns:
        dict with delta, gamma, theta, vega (or zeros if not found)
    """
    try:
        client = get_client()

        # Parse expiration date
        if '/' in expiration:
            exp_dt = datetime.strptime(expiration, '%m/%d/%Y')
        else:
            exp_dt = datetime.strptime(expiration, '%Y-%m-%d')

        logger.info(f"Fetching Greeks for {symbol} ${strike}P expiring {exp_dt.strftime('%Y-%m-%d')}")

        # Fetch option chain
        response = client.get_option_chain(
            symbol=symbol,
            contract_type=Client.Options.ContractType.PUT
        )

        if response.status_code != 200:
            logger.error(f"Failed to fetch option chain for {symbol}: {response.status_code}")
            return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}

        chain = response.json()
        put_exp_map = chain.get('putExpDateMap', {})

        # Find the option by expiration and strike
        exp_key = exp_dt.strftime('%Y-%m-%d')
        target_option = None

        for exp_date_str, strikes in put_exp_map.items():
            # Extract date part (before colon)
            exp_date = exp_date_str.split(':')[0]

            if exp_date == exp_key:
                # Found the expiration, now find the strike
                strike_key = f"{strike:.1f}" if strike != int(strike) else f"{int(strike)}.0"

                if strike_key in strikes:
                    options_list = strikes[strike_key]
                    if options_list and len(options_list) > 0:
                        target_option = options_list[0]
                        break

        if not target_option:
            logger.warning(f"Option not found in chain: {symbol} ${strike}P {exp_key}")

            # Try to log available strikes for debugging
            if put_exp_map:
                first_exp = list(put_exp_map.keys())[0]
                available_strikes = list(put_exp_map[first_exp].keys())[:5]
                logger.info(f"Available strikes for {first_exp}: {available_strikes}")

            return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}

        # Extract Greeks from the option
        delta = safe_float(target_option.get('delta', 0))
        gamma = safe_float(target_option.get('gamma', 0))
        theta = safe_float(target_option.get('theta', 0))
        vega = safe_float(target_option.get('vega', 0))

        logger.info(f"✅ Greeks found for {symbol} ${strike}P:")
        logger.info(f"   Delta: {delta:.4f}, Gamma: {gamma:.4f}, Theta: {theta:.4f}, Vega: {vega:.4f}")

        return {
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'vega': vega
        }

    except Exception as e:
        logger.error(f"Error fetching Greeks for {symbol}: {e}", exc_info=True)
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}


def update_greeks_in_sqlite():
    """
    Update Greeks for all open trades in the SQLite database.

    Reads open trades from trade_outcome_tracker, fetches live Greeks
    from Schwab for each, and writes delta/gamma/theta/vega back to SQLite.

    Returns:
        Number of trades successfully updated.
    """
    open_trades = get_open_trades()
    logger.info(f"Found {len(open_trades)} open trade(s) to update")

    if not open_trades:
        logger.warning("No open trades in SQLite — nothing to update")
        return 0

    updated_count = 0
    for trade in open_trades:
        try:
            symbol = trade.get('symbol', '')
            strike = trade.get('strike', 0)
            expiration = trade.get('expiration', '')
            trade_id = trade.get('id')

            if not symbol or not strike or not expiration:
                logger.warning(f"Trade {trade_id}: missing required data, skipping")
                continue

            logger.info(f"\nTrade {trade_id}: {symbol} ${strike}P exp {expiration}")

            greeks = get_option_greeks(symbol, strike, expiration, 0)

            if greeks['delta'] == 0 and greeks['theta'] == 0:
                logger.warning(f"Trade {trade_id}: no Greeks found for {symbol} ${strike}P")
                continue

            with get_db() as conn:
                conn.execute("""
                    UPDATE trades
                    SET delta = ?, gamma = ?, theta = ?, vega = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (greeks['delta'], greeks['gamma'], greeks['theta'], greeks['vega'], trade_id))

            logger.info(
                f"✅ Trade {trade_id}: {symbol} — "
                f"Delta={greeks['delta']:.4f}, Gamma={greeks['gamma']:.4f}, "
                f"Theta={greeks['theta']:.4f}, Vega={greeks['vega']:.4f}"
            )
            updated_count += 1

        except Exception as e:
            logger.error(f"Error updating trade {trade.get('id', '?')}: {e}", exc_info=True)
            continue

    logger.info(f"\n{'='*60}")
    logger.info(f"✅ COMPLETE: Updated Greeks for {updated_count}/{len(open_trades)} trades")
    logger.info(f"{'='*60}")

    return updated_count


if __name__ == "__main__":
    print("="*60)
    print("Greeks Update Tool — Schwab API → SQLite")
    print("="*60)
    print()

    try:
        updated = update_greeks_in_sqlite()

        if updated > 0:
            print()
            print("="*60)
            print(f"✅ SUCCESS: Updated {updated} trade(s)")
            print("="*60)
            print()
            print("Next steps:")
            print("1. Restart dashboard server: python dashboard_server.py")
            print("2. Check Analytics tab → Portfolio Greeks")
            print()
        else:
            print()
            print("⚠️ No trades were updated")

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        raise
