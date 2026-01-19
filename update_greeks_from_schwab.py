"""
update_greeks_from_schwab.py - Automatically Update Greeks in Google Sheets

Fetches live Greeks from Schwab API for all open positions and updates
the Live_Trades worksheet with Delta, Gamma, Theta, Vega values.

Usage:
    python update_greeks_from_schwab.py
"""

import gspread
from schwab_utils import get_client
from schwab.client import Client
from config import logger
from datetime import datetime
from helper_functions import safe_float
import os

GOOGLE_SHEET_ID = "1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc"
WORKSHEET_NAME = "Live_Trades"


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


def ensure_greek_columns(worksheet):
    """
    Ensure Gamma and Vega columns exist in the worksheet.
    Adds them if missing.

    Args:
        worksheet: gspread worksheet object

    Returns:
        dict with column indices for Delta, Gamma, Theta, Vega
    """
    # Get header row
    headers = worksheet.row_values(1)

    greek_cols = {
        'Delta': None,
        'Gamma': None,
        'Theta': None,
        'Vega': None
    }

    # Find existing Greek columns
    for i, header in enumerate(headers, 1):
        if header in greek_cols:
            greek_cols[header] = i

    # Add missing columns
    missing = [col for col, idx in greek_cols.items() if idx is None]

    if missing:
        logger.info(f"Adding missing Greek columns: {missing}")

        # Add columns after the last existing column
        next_col = len(headers) + 1

        for col_name in missing:
            worksheet.update_cell(1, next_col, col_name)
            greek_cols[col_name] = next_col
            logger.info(f"  Added column '{col_name}' at position {next_col}")
            next_col += 1

    return greek_cols


def update_greeks_in_sheet():
    """
    Main function to update Greeks for all positions in Live_Trades worksheet.
    """
    try:
        # Connect to Google Sheets
        logger.info("Connecting to Google Sheets...")
        gc = gspread.service_account(filename='google-credentials.json')
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.worksheet(WORKSHEET_NAME)

        logger.info(f"✅ Connected to worksheet: {WORKSHEET_NAME}")

        # Ensure Greek columns exist
        greek_col_indices = ensure_greek_columns(worksheet)
        logger.info(f"Greek column indices: {greek_col_indices}")

        # Get all records
        records = worksheet.get_all_records()
        logger.info(f"Found {len(records)} positions to update")

        if not records:
            logger.warning("No positions found in worksheet")
            return

        # Update each position
        updated_count = 0
        for i, record in enumerate(records, 2):  # Start at row 2 (row 1 is headers)
            try:
                symbol = str(record.get('Symbol', ''))
                strike = safe_float(record.get('Strike', 0))
                exp_date = str(record.get('Exp Date', ''))
                underlying_price = safe_float(record.get('Underlying Price', 0))

                if not symbol or not strike or not exp_date:
                    logger.warning(f"Row {i}: Missing required data (Symbol: {symbol}, Strike: {strike}, Exp: {exp_date})")
                    continue

                logger.info(f"\nRow {i}: Updating {symbol} ${strike}P exp {exp_date}")

                # Fetch Greeks from Schwab
                greeks = get_option_greeks(symbol, strike, exp_date, underlying_price)

                if greeks['delta'] == 0 and greeks['theta'] == 0:
                    logger.warning(f"Row {i}: No Greeks found for {symbol} ${strike}P")
                    continue

                # Update cells in worksheet
                updates = []

                if greek_col_indices['Delta']:
                    worksheet.update_cell(i, greek_col_indices['Delta'], greeks['delta'])
                    updates.append(f"Delta={greeks['delta']:.4f}")

                if greek_col_indices['Gamma']:
                    worksheet.update_cell(i, greek_col_indices['Gamma'], greeks['gamma'])
                    updates.append(f"Gamma={greeks['gamma']:.4f}")

                if greek_col_indices['Theta']:
                    worksheet.update_cell(i, greek_col_indices['Theta'], greeks['theta'])
                    updates.append(f"Theta={greeks['theta']:.4f}")

                if greek_col_indices['Vega']:
                    worksheet.update_cell(i, greek_col_indices['Vega'], greeks['vega'])
                    updates.append(f"Vega={greeks['vega']:.4f}")

                logger.info(f"✅ Row {i}: Updated {symbol} - {', '.join(updates)}")
                updated_count += 1

            except Exception as e:
                logger.error(f"Error updating row {i}: {e}", exc_info=True)
                continue

        logger.info(f"\n{'='*60}")
        logger.info(f"✅ COMPLETE: Updated Greeks for {updated_count}/{len(records)} positions")
        logger.info(f"{'='*60}")

        return updated_count

    except Exception as e:
        logger.error(f"Failed to update Greeks: {e}", exc_info=True)
        return 0


if __name__ == "__main__":
    print("="*60)
    print("Greeks Update Tool - Schwab API to Google Sheets")
    print("="*60)
    print()

    try:
        updated = update_greeks_in_sheet()

        if updated > 0:
            print()
            print("="*60)
            print(f"✅ SUCCESS: Updated {updated} position(s)")
            print("="*60)
            print()
            print("Next steps:")
            print("1. Refresh your Google Sheet to see the updated Greeks")
            print("2. Restart dashboard server: python dashboard_server.py")
            print("3. Check Analytics tab → Portfolio Greeks")
            print()
        else:
            print()
            print("⚠️ No positions were updated")
            print("Check the logs above for errors")
            print()

    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
