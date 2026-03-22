# open_trades_monitor_gsheet.py
# Monitors open cash-secured puts from Google Sheet + live quotes + Grok analysis

import sys
import io

# FIX WINDOWS EMOJI ENCODING FIRST
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import os
import asyncio
import json
import logging
import pytz
import gspread
import requests
import pandas as pd
from datetime import datetime
from telegram import Bot as telegram_bot
import yfinance as yf
from schwab import auth
from schwab.client import Client
import re
from dotenv import load_dotenv
from pathlib import Path

from grok_utils import call_grok, MODEL_FAST
from schwab_utils import get_client

from helper_functions import safe_date, safe_date_update, safe_float, safe_int

load_dotenv()

# Suppress pandas warnings
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

# ==================== CONFIG ====================
TELEGRAM_TOKEN = os.getenv('PAPER_TRADE_MONITOR_TELEGRAM_TOKEN')
CHAT_ID = 7972059629
bot = telegram_bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

SCHWAB_API_KEY = os.getenv('SCHWAB_API_KEY')
SCHWAB_APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
OLD_REDIRECT = os.getenv('OLD_REDIRECT', 'https://127.0.0.1')
TOKEN_PATH = 'schwab_token.json'

GROK_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

GOOGLE_SHEET_ID = "1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc"

CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)
QUOTE_CACHE_FILE = CACHE_DIR / 'open_trade_quotes_cache.json'
QUOTE_CACHE_MINUTES = 5
GSHEET_NAME = "Live_Trades"
JSON_KEYFILE = "google-credentials.json"

ET_TZ = pytz.timezone('US/Eastern')

# ==================== SCHWAB CLIENT ====================

get_client = get_client()

# ==================== QUOTE CACHE ====================
def load_quote_cache():
    if os.path.exists(QUOTE_CACHE_FILE):
        try:
            with open(QUOTE_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cache_time = datetime.fromisoformat(data['timestamp'])
            if (datetime.now() - cache_time).total_seconds() < QUOTE_CACHE_MINUTES * 60:
                logging.info(f"Using cached quotes ({len(data['quotes'])} options)")
                return data['quotes']
        except Exception as e:
            logging.warning(f"Quote cache load failed: {e}")
    return None

def save_quote_cache(quotes):
    try:
        cache_data = {
            'timestamp': datetime.now().isoformat(),
            'quotes': quotes
        }
        with open(QUOTE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f)
        logging.info("Saved live quotes to cache")
    except Exception as e:
        logging.warning(f"Quote cache save failed: {e}")


def parse_option_symbol(opt_symbol):
    """Parse user input → Extract details from Thinkorswim format"""
    try:
        # Clean the symbol: remove spaces and leading dot
        clean_symbol = opt_symbol.strip().replace(' ', '').lstrip('.')
        
        # Match the Thinkorswim format: TICKER + YYMMDD + P/C + Strike
        match = re.match(r'^([A-Z]+)(\d{6})([PC])(\d+(?:\.\d+)?)$', clean_symbol.upper())
        if not match:
            print(f"❌ Failed to parse symbol: {opt_symbol}")
            return None, None, None, None, None
        
        ticker = match.group(1)
        date_str = match.group(2)  # YYMMDD
        put_call = match.group(3)
        strike_str = match.group(4)
        
        exp_date = datetime.strptime(date_str, '%y%m%d').date()
        strike = float(strike_str)
        is_put = (put_call == 'P')

        # Return the original Thinkorswim format (with dot) for reference
        # But we'll use the underlying symbol + details for API calls
        occ_symbol = f".{ticker}{date_str}{put_call}{strike_str}"
        
        print(f"✅ Parsed: {opt_symbol} → Ticker: {ticker}, Exp: {exp_date}, Strike: {strike}, Type: {'Put' if is_put else 'Call'}")
        return ticker, exp_date, strike, is_put, occ_symbol
        
    except Exception as e:
        print(f"❌ Parse failed for {opt_symbol}: {e}")
        return None, None, None, None, None


# ==================== GOOGLE SHEET ====================
sheet = None
def get_sheet():
    global sheet
    if sheet is None:
        try:
            root_dir = os.path.dirname(os.path.abspath(__file__))
            creds_path = os.path.join(root_dir, JSON_KEYFILE)
            gc = gspread.service_account(filename=creds_path)
            sh = gc.open_by_key(GOOGLE_SHEET_ID)
            sheet = sh.worksheet(GSHEET_NAME)
            print("✅ Google Sheet connected.")
        except Exception as e:
            print(f"❌ Sheet connection failed: {e}")
            sheet = None
    return sheet

# ==================== TELEGRAM ALERT ====================
async def send_alert(message):
    if not bot:
        print("Telegram disabled")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, disable_web_page_preview=True)
        print("Telegram alert sent.")
    except Exception as e:
        print(f"Telegram error: {e}")

# ==================== CONCURRENT QUOTE FETCHING ====================
async def fetch_option_quote(client, occ_symbol):
    try:
        print(f"🔍 Fetching quote for: '{occ_symbol}'")
        
        # Extract the underlying symbol from the option symbol
        # Remove the dot and parse the underlying ticker
        clean_symbol = occ_symbol.lstrip('.')
        match = re.match(r'^([A-Z]+)\d+[PC][\d.]+$', clean_symbol)
        if not match:
            print(f"❌ Could not extract underlying from {occ_symbol}")
            return None
            
        underlying_symbol = match.group(1)
        print(f"🔧 Underlying symbol: {underlying_symbol}")
        
        # Get the full option chain for the underlying
        resp = client.get_option_chain(
            symbol=underlying_symbol,  # Use underlying, not option symbol
            contract_type=Client.Options.ContractType.ALL
        )
        
        print(f"🔧 Response status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"❌ API error {resp.status_code} for {underlying_symbol}")
            print(f"❌ Response text: {resp.text[:200]}...")
            return None
            
        chain = resp.json()
        print(f"✅ Got chain data for {underlying_symbol}")

        # Parse the option details from the symbol
        match = re.match(r'^[A-Z]+(\d{6})([PC])([\d.]+)$', clean_symbol)
        if not match:
            print(f"❌ Could not parse option details from {clean_symbol}")
            return None

        target_date_str = match.group(1)  # YYMMDD
        target_put_call = match.group(2)  # P or C
        target_strike = float(match.group(3))  # Changed to float to handle decimals
        
        # Convert to Schwab's expected format: YYYY-MM-DD
        target_exp_date = f"20{target_date_str[:2]}-{target_date_str[2:4]}-{target_date_str[4:6]}"
        
        print(f"🔧 Looking for: {target_exp_date}, {target_put_call}, strike: ${target_strike}")
        
        # Search through the chain for our specific option
        chain_map = chain.get('putExpDateMap' if target_put_call == 'P' else 'callExpDateMap', {})
        
        for exp_date_str, strikes in chain_map.items():
            # Schwab's format: "2025-12-26:10" (date:daysToExpiration)
            if exp_date_str.startswith(target_exp_date):
                print(f"✅ Found matching expiration: {exp_date_str}")
                
                for strike_price, options_list in strikes.items():
                    strike_float = float(strike_price)
                    
                    if abs(strike_float - target_strike) < 0.01 and options_list:
                        print(f"✅ Found matching strike: ${strike_float:.2f}")
                        option_data = options_list[0]
                        print(f"✅ Found option: {option_data.get('symbol')}")
                        return option_data
        
        print(f"⚠️  Option not found in chain: {occ_symbol}")
        print(f"⚠️  Available expirations: {list(chain_map.keys())[:3]}...")  # Show first 3
        
        return None
        
    except Exception as e:
        logging.warning(f"Quote fetch failed for {occ_symbol}: {e}")
        return None

async def get_live_quotes_concurrent(client, occ_symbols):
    if not occ_symbols:
        return {}
    
    sem = asyncio.Semaphore(6)  # Max 6 concurrent requests

    async def bounded_fetch(sym):
        async with sem:
            return sym, await fetch_option_quote(client, sym)

    tasks = [bounded_fetch(sym) for sym in occ_symbols]
    results = await asyncio.gather(*tasks)
    return {sym: quote for sym, quote in results if quote is not None}

def get_dynamic_exit_suggestion(row):
    entry = row.get('Entry Premium', 0)
    mark = row.get('_current_mark', entry)
    dte = row.get('_dte', 0)
    theta_daily = row.get('_theta', 0)  # You'll need to add theta from quotes
    progress_pct = row.get('_progress_pct', 0)
    
    if entry == 0:
        return "Hold", "No entry data"
    
    # Base 50% target
    base_target = 50
    
    # Adjust by theta intensity and DTE
    if dte < 15 and abs(theta_daily) > 0.20:
        adjusted_target = 40 + (progress_pct - 40) * 1.5  # Aggressive close
        reason = "High theta burn + low DTE → close early"
    elif dte < 7:
        adjusted_target = 30
        reason = "Very low DTE → lock profits now"
    elif dte > 30 and abs(theta_daily) < 0.15:
        adjusted_target = 65
        reason = "Slow decay + long DTE → let run"
    else:
        adjusted_target = 55
        reason = "Standard theta curve"
    
    current_progress = progress_pct
    suggested_action = "CLOSE NOW" if current_progress >= adjusted_target else "HOLD"
    
    return suggested_action, f"Target: {adjusted_target:.0f}% (Current: {current_progress:.1f}%) — {reason}"

# ==================== ENRICH TRADE WITH LIVE DATA ====================
def enrich_trade_with_live_data(row, quote_data):
    """Add live metrics and Grok analysis to a trade row with fallback to sheet values"""
    opt_symbol_input = row.get('Option Symbol', '').strip()
    entry_premium = safe_float(row.get('Entry Premium'))

    ticker, exp_date, strike, is_put, occ_symbol = parse_option_symbol(opt_symbol_input)
    if not occ_symbol:
        return row

    quote = quote_data.get(occ_symbol, {})

    # === LIVE VALUES FROM SCHWAB ===
    live_bid = quote.get('bid', 0.0)
    live_ask = quote.get('ask', 0.0)
    live_mark = quote.get('mark', (live_bid + live_ask) / 2 if live_bid + live_ask > 0 else quote.get('lastPrice', 0.0))
    live_delta = abs(quote.get('delta', 0.0))
    live_iv = quote.get('volatility', 0.0)

    # === SHEET VALUES (last known) ===
    sheet_bid = safe_float(row.get('Bid', 0))
    sheet_ask = safe_float(row.get('Ask', 0))
    sheet_mark = safe_float(row.get('Current Mark', 0))

    # === FALLBACK LOGIC: Use sheet values if live quotes are zero/unavailable ===
    using_fallback = False
    if live_mark <= 0.01 and sheet_mark > 0:
        # Market closed or quote failed — use last known from sheet
        bid = sheet_bid
        ask = sheet_ask
        current_mark = sheet_mark
        using_fallback = True
        print(f"   ⚠️ {ticker}: Market closed — using sheet values (Mark ${current_mark:.2f})")
    else:
        # Live data good
        bid = live_bid if live_bid > 0 else sheet_bid
        ask = live_ask if live_ask > 0 else sheet_ask
        current_mark = live_mark if live_mark > 0.01 else sheet_mark
        if live_mark <= 0.01:
            print(f"    ⚠️ {ticker}: Mixed fallback (Live: ${live_mark:.2f}, Sheet: ${sheet_mark:.2f})")
    
    action, exit_note = get_dynamic_exit_suggestion(row)

    row['exit_suggestion'] = action
    row['exit_reason'] = exit_note
    row['_using_fallback_quotes'] = using_fallback

    if exp_date:
        row['Exp Date'] = safe_date_update(row, exp_date)

    # Use live greeks if available, otherwise keep sheet/default
    delta = live_delta if live_delta != 0 else safe_float(row.get('Delta', 0))
    iv = live_iv if live_iv != 0 else safe_float(row.get('IV', 0))
    raw_theta = quote.get('theta', 0.0)
    forward_theta_daily = -raw_theta  # Positive benefit for short

    # Underlying price fallback chain
    underlying_price = quote.get('underlyingPrice', 0.00)
    if underlying_price <= 0 and get_client:
        try:
            q = get_client.get_quote(ticker).json()[ticker]['quote']
            underlying_price = q.get('lastPrice') or q.get('closePrice') or 0.0
        except:
            pass
    if underlying_price <= 0:
        try:
            underlying_price = yf.Ticker(ticker).info.get('regularMarketPrice', 0.0)
        except:
            pass

    contracts = safe_int(row.get('Contracts Qty', 1))
    total_credit = entry_premium * 100 * contracts
    
    entry_date = safe_date(row.get('Entry Date'))
    days_open = max((datetime.now().date() - entry_date).days, 1) if entry_date else 1
    dte = quote.get('daysToExpiration', 0)


    profit_captured = max(entry_premium - current_mark, 0)
    pl_dollars = profit_captured * 100 * contracts
    pl_percentage = (profit_captured / entry_premium * 100) if entry_premium > 0 else 0
    progress_pct = min((profit_captured / entry_premium * 100) / 0.5, 100) if entry_premium > 0 else 0

    daily_decay_dollars = profit_captured / days_open if days_open > 0 else 0
    daily_decay_percent = (daily_decay_dollars / entry_premium * 100) if entry_premium > 0 else 0

    # Assignment simulator
    profit_if_expires = entry_premium * 100 * contracts
    cost_basis_if_assigned = strike - entry_premium
    unrealized_if_assigned = (underlying_price - cost_basis_if_assigned) * 100 * contracts

    risk = "Low"
    if underlying_price > strike * 0.95:
        risk = "High"
    elif underlying_price > strike * 0.90:
        risk = "Moderate"

    # Grok prompt (uses current_mark which now includes fallback)
    grok_prompt = (
        f"Short {ticker} ${strike:.2f} {'PUT' if is_put else 'CALL'} exp {exp_date} | "
        f"Entry ${entry_premium:.2f} → ${current_mark:.2f} ({progress_pct:.1f}% to 50%) | "
        f"DTE {dte} {days_open}d open | ${underlying_price:.2f} | "
        f"Delta {delta:.2f} IV {iv:.1f}% Theta {raw_theta:.2f} | P/L ${pl_dollars:,.0f}\n"
        "Advise: Close/Hold/Roll? Risks? Under 80 words."
    )
    grok_analysis = call_grok(
        [{"role": "system", "content": "You are a wheel strategy position manager. Be concise."},
         {"role": "user", "content": grok_prompt}],
        model=MODEL_FAST,
        max_tokens=150,
    ) or "Analysis unavailable"

    # Update row
    row.update({
        'Bid': f"${bid:.2f}",
        'Ask': f"${ask:.2f}",
        'Current Mark': f"${current_mark:.2f}",
        'Current P/L $': f"${pl_dollars:,.2f}",
        'Current P/L %': f"{pl_percentage:.1f}%",
        'Progress to Target': f"{progress_pct:.1f}%",
        'Underlying Price': f"${underlying_price:.2f}",
        'Delta': f"{delta:.2f}",
        'IV': f"{iv:.1f}%",
        'Theta': f"{raw_theta:.2f}",
        'DTE': dte,
        'Days Since Entry': days_open,
        'Daily Theta Decay $': f"${daily_decay_dollars:.3f}",
        'Daily Theta Decay %': f"{daily_decay_percent:.1f}%",
        'Forward Theta Daily $': f"${forward_theta_daily * 100 * contracts:.2f}",

        # Internal/raw
        '_current_mark': current_mark,
        '_pl_dollars': pl_dollars,
        '_pl_percentage': pl_percentage,
        '_progress_pct': progress_pct,
        '_daily_theta_decay_dollars': daily_decay_dollars,
        '_forward_theta_daily': forward_theta_daily * 100 * contracts,
        '_projected_decay': forward_theta_daily * 100 * contracts * dte,
        '_contracts': contracts,
        '_total_credit': total_credit,
        '_underlying_price': underlying_price,
        '_delta': delta,
        '_iv': iv,
        '_dte': dte,
        '_theta': raw_theta,
        '_days_open': days_open,
        '_exp_date': exp_date,
        '_bid': bid,
        '_ask': ask,

        # Assignment
        'profit_if_expires_formatted': f"+${profit_if_expires:,.0f}",
        'cost_basis_if_assigned': cost_basis_if_assigned,
        'current_value_formatted': f"${underlying_price * 100 * contracts:,.0f}",
        'unrealized_formatted': f"${abs(unrealized_if_assigned):,.0f} {'gain' if unrealized_if_assigned > 0 else 'loss'}",
        'assignment_risk': risk,
        'grok_analysis': grok_analysis,
    })

    return row

async def update_sheet_with_live_data(df, ws):
    """Update the Google Sheet with enriched live data"""
    if df.empty or ws is None:
        print("❌ No data to update or worksheet unavailable")
        return

    # Columns we want to update in the sheet
    update_cols = [
        'Bid', 'Ask', 'Current Mark', 'Current P/L $', 'Current P/L %', 'Progress to Target',
        'Underlying Price', 'Delta', 'IV', 'DTE', 'Days Since Entry', 'Theta',
        'Daily Theta Decay $', 'Daily Theta Decay %', 'Forward Theta Daily $',
        'Exp Date'
    ]

    # Get header to find column indices
    header = ws.row_values(1)
    print(f"📋 Sheet header: {header}")
    
    col_indices = {}
    for col_name in update_cols:
        try:
            col_idx = header.index(col_name) + 1  # 1-based for gspread
            col_indices[col_name] = col_idx
            print(f"✅ Found column '{col_name}' at index {col_idx}")
        except ValueError:
            print(f"❌ Column '{col_name}' not found in sheet — skipping")

    if not col_indices:
        print("❌ No matching columns found for update")
        return

    # Build batch update
    batch = []
    for idx, (_, row) in enumerate(df.iterrows()):
        sheet_row = idx + 2  # +1 for header, +1 for 1-based indexing
        print(f"📝 Processing row {sheet_row}: {row.get('Symbol', 'Unknown')}")

        for col_name, col_idx in col_indices.items():
            value = row.get(col_name, '')
            # Convert date/datetime objects to strings for JSON serialization
            if hasattr(value, 'strftime'):
                value = value.strftime('%Y-%m-%d')
            elif hasattr(value, 'isoformat'):
                value = value.isoformat()
            # Handle numpy types
            elif hasattr(value, 'item'):
                value = value.item()
            # Ensure value is JSON serializable
            if value is None or (isinstance(value, float) and (value != value)):  # NaN check
                value = ''
            cell = gspread.utils.rowcol_to_a1(sheet_row, col_idx)
            batch.append({
                'range': cell,
                'values': [[value]]
            })
            print(f"   {cell} ({col_name}): {value}")

    if batch:
        print(f"🔄 Updating {len(batch)} cells in batch...")
        try:
            result = ws.batch_update(batch)
            print(f"✅ Successfully updated {len(df)} rows with live data")
            print(f"📊 Batch update result: {result}")
        except Exception as e:
            print(f"❌ Batch update failed: {e}")
    else:
        print("❌ No batch updates to process")       


# ==================== MAIN FUNCTIONS ====================
def load_trades_from_sheet():
    ws = get_sheet()
    if not ws:
        return pd.DataFrame(), None

    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return pd.DataFrame(), ws

    header = all_values[0]
    data = all_values[1:]
    df = pd.DataFrame(data, columns=header)

    print(f"Loaded {len(df)} trades from sheet")
    return df, ws

async def update_all_trades():
    df, ws = load_trades_from_sheet()
    if df.empty:
        await send_alert("📭 No open trades currently.")
        return []
    
    print("🔍 Parsing option symbols from sheet:")
    occ_symbols = []
    for idx, row in df.iterrows():
        raw_symbol = row.get('Option Symbol', '')
        ticker, exp_date, strike, is_put, occ = parse_option_symbol(raw_symbol)
        if occ:
            print(f"  Row {idx+2}: '{raw_symbol}' → {ticker} ${strike}P → OCC: {occ}")
            occ_symbols.append(occ)
        else:
            print(f"   ❌ Row {idx+2}: Failed to parse '{raw_symbol}'")

    print(f"📊 Total valid OCC symbols: {len(occ_symbols)}")

    # Get OCC symbols for live quotes
    occ_symbols = []
    for _, row in df.iterrows():
        _, _, _, _, occ = parse_option_symbol(row.get('Option Symbol', ''))
        if occ:
            occ_symbols.append(occ)

    # Load cache or fetch live
    cached = load_quote_cache()
    if cached and all(sym in cached for sym in occ_symbols):
        quotes = cached
        print("Using cached quotes")
    else:
        if not get_client:
            print("No Schwab client — skipping live update")
            return df.to_dict('records')
        print(f"Fetching {len(occ_symbols)} live quotes concurrently...")
        quotes = await get_live_quotes_concurrent(get_client, occ_symbols)
        save_quote_cache(quotes)

    # Enrich each row
    enriched = []
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        enriched_row = enrich_trade_with_live_data(row_dict, quotes)
        enriched.append(enriched_row)

    # === WRITE OCC SYMBOL ON CLOSED TRADES ===
    if ws:
        header = ws.row_values(1)
        occ_col_idx = None

        for i, col in enumerate(header):
            if col.strip() == "Option Symbol":
                occ_col_idx = i + 1
                break

        # Add column if missing
        if occ_col_idx is None:
            ws.append_row(["Option Symbol"], table_range="A1")
            occ_col_idx = len(header) + 1
            print("Added 'Option Symbol' column to sheet")

        batch_updates = []
        for idx, (_, row) in enumerate(df.iterrows()):
            sheet_row = idx + 2

            if pd.notna(row.get('Exit Date')) and str(row.get('Exit Date', '')).strip() != '':
                user_input = str(row.get('Option Symbol', '')).strip()
                if not user_input:
                    sym = str(row.get('Symbol', '')).strip()
                    strike = safe_float(row.get('Strike', 0))
                    exp_str = str(row.get('Exp Date', '')).strip()
                    if sym and strike > 0 and exp_str:
                        try:
                            exp_date = datetime.strptime(exp_str, '%m/%d/%Y').strftime('%y%m%d')
                            put_call = 'P' if 'Put' in str(row.get('Strategy', '')) else 'C'
                            user_input = f"{sym}{exp_date}{put_call}{strike}"
                        except:
                            user_input = ""

                _, _, _, _, occ_symbol = parse_option_symbol(user_input)
                if occ_symbol:
                    cell = gspread.utils.rowcol_to_a1(sheet_row, occ_col_idx)
                    batch_updates.append({'range': cell, 'values': [[occ_symbol]]})
                    print(f"Wrote OCC symbol {occ_symbol} for row {sheet_row}")

        if batch_updates:
            try:
                ws.batch_update(batch_updates)
                print(f"Updated {len(batch_updates)} closed trades with OCC symbols")
            except Exception as e:
                print(f"Batch update failed: {e}")

    # === UPDATE SHEET WITH LIVE DATA ===
    if ws:
        try:
            await update_sheet_with_live_data(pd.DataFrame(enriched), ws)
        except Exception as e:
            print(f"Live data update failed: {e}")

    return enriched



# ==================== MONITOR LOOP ====================
async def monitor_loop():
    print("\n" + "="*70)
    print("🚀 STARTING Open Trades Monitor")
    print("="*70)

    while True:
        now_et = datetime.now(ET_TZ)
        market_open = (9 <= now_et.hour < 16) and now_et.weekday() < 5
        sleep_seconds = 3600 if market_open else 14400  # 1h during market, 4h after

        enriched_trades = await update_all_trades()

        if enriched_trades:
            header = f"📊 OPEN CSPs ({len(enriched_trades)} positions) — {now_et.strftime('%b %d %I:%M %p ET')}"
            await send_alert(header)

            for trade in enriched_trades:
                urgency = ""
                if trade['_progress_pct'] >= 80: urgency += "NEAR CLOSE! "
                elif trade['_progress_pct'] >= 50: urgency += "50% HIT! "
                if trade['_dte'] < 7: urgency += "LOW DTE! "
                if trade['_underlying_price'] >= safe_float(trade.get('Strike')): urgency += "ASSIGNMENT RISK "

                msg = (
                    f"{urgency}{trade['Symbol']} ${safe_float(trade.get('Strike')):.2f}P\n"
                    f"Exp: {trade.get('Exp Date', 'N/A')} | DTE: {trade['_dte']} | Open: {trade['_days_open']}d\n"
                    f"Credit: ${safe_float(trade.get('Entry Premium')):.2f} → Mark: ${trade['_current_mark']:.2f}\n"
                    f"Bid/Ask: ${trade.get('Bid','')}/${trade.get('Ask','')}\n"
                    f"Progress: {trade['_progress_pct']:.1f}% | P/L: ${trade['_pl_dollars']:,.0f}\n"
                    f"Price: ${trade['_underlying_price']:.2f} | Δ: {trade['_delta']:.2f} | IV: {trade['_iv']:.0f}%\n\n"
                    f"🎯 Exit: {trade['exit_suggestion']} ({trade['exit_reason']})\n"
                    f"🤖 Grok:\n{trade.get('grok_analysis', 'Analysis unavailable')}"
                )
                await send_alert(msg)
                await asyncio.sleep(3)

        await asyncio.sleep(sleep_seconds)

if __name__ == "__main__":
    asyncio.run(monitor_loop())