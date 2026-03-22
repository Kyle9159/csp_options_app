# generate_dashboard.py — Ultimate Premium Dashboard (Dec 2025)

import os
import asyncio
from datetime import datetime, date, timedelta
import pytz
from jinja2 import Template, Environment, FileSystemLoader
import re
import yfinance as yf
from tqdm import tqdm
import dotenv
import requests
import json
import gspread
import math
from pathlib import Path

dotenv.load_dotenv()

# Schwab imports
from schwab import auth


# Config
API_KEY = os.getenv('SCHWAB_API_KEY')
APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')
PAPER_TRADING = os.getenv('PAPER_TRADING', 'True').lower() == 'true'
PAPER_ACCOUNT_ID = os.getenv('PAPER_ACCOUNT_ID')
LIVE_ACCOUNT_ID = os.getenv('LIVE_ACCOUNT_ID')
ACCOUNT_ID = PAPER_ACCOUNT_ID if PAPER_TRADING else LIVE_ACCOUNT_ID  # <-- Defined here
TOKEN_PATH = 'schwab_token.json'
XAI_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

# Bot imports
from grok_utils import get_grok_opportunity_analysis
from open_trade_monitor import load_trades_from_sheet, enrich_trade_with_live_data, get_live_quotes_concurrent, parse_option_symbol
from grok_utils import call_grok, MODEL_FAST
from covered_call_bot import get_current_positions, find_covered_calls
from dividend_tracker_bot import generate_dividend_report
from simple_options_scanner import main as run_simple_scanner
from zero_dte_spread_scanner import scan_0dte_spreads
from helper_functions import calculate_trade_score, save_cached_scanner, load_cached_scanner, safe_float, safe_int, save_cached_leaps, load_cached_leaps, load_sr_cache
from schwab_utils import get_client

# Enhanced analytics imports
from core.trade_scorer import TradeScorer
from smart_alerts import calculate_bollinger_bands

import leaps_scanner
from leaps_scanner import main as run_leaps_scanner

ET_TZ = pytz.timezone('US/Eastern')
DASHBOARD_FILE = 'trading_dashboard.html'
EODHD_API_KEY = os.getenv('EODHD_API_KEY')
GOOGLE_SHEET_ID = "1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc"

WHEEL_CAPITAL = float(os.getenv('WHEEL_CAPITAL', 25000))  # Your total wheel cash
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', 5))
MAX_PER_TRADE_PCT = float(os.getenv('MAX_PER_TRADE_PCT', 0.20))  # 20% max per trade

TOP_WHEEL_TRADERS = [
    "tastytrade", "optionalpha", "projectoption", "thetagang", "wheeltrader", 
    "cspmaster", "thetatrader", "steadywheel"  # add real handles you follow
]

# Data
open_trades = []
covered_calls = []
simple_scanner_opps = []
zero_dte_opps = []
dividend_tiles = []
portfolio_summary = {}
calendar_events = []
grok_symbols = []
grok_sentiment = "NEUTRAL"
grok_summary = "Loading..."

leaps_opps = []
captured_leaps = []

captured_div = []
captured_scanner = []
captured_0dte = []


import dividend_tracker_bot
import simple_options_scanner

def capture_div(message):
    captured_div.append(message)

def capture_scanner(message):
    captured_scanner.append(message)

def capture_leaps(message):
    captured_leaps.append(message)

async def async_capture_leaps(message):
    capture_leaps(message)

def capture_0dte(message):
    captured_0dte.append(message)

# Create async wrappers that the bots can await
async def async_capture_div(message):
    capture_div(message)

async def async_capture_scanner(message):
    capture_scanner(message)

original_div_send = dividend_tracker_bot.send_alert
dividend_tracker_bot.send_alert = async_capture_div

original_scanner_send = simple_options_scanner.send_alert
simple_options_scanner.send_alert = async_capture_scanner

original_leaps_send = leaps_scanner.send_alert
leaps_scanner.send_alert = async_capture_leaps

    
# Schwab client — lazy loaded so importing this module doesn't trigger auth flow
c = None
client = None
def _get_client():
    global client
    if client is None:
        client = get_client()
    return client

# === GROK CACHING UTILITIES ===
CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)


TRADE_ANALYSIS_CACHE = CACHE_DIR / 'grok_trade_analysis_cache.json'
CACHE_HOURS = 168  # Refresh once per day (allows slight buffer)

SCANNER_CACHE_FILE = CACHE_DIR / 'simple_scanner_cache.json'
SCANNER_CACHE_HOURS = 2  # Refresh every 4 hours during active trading day
LEAPS_CACHE_FILE = CACHE_DIR / 'leaps_cache.json'
LEAPS_CACHE_HOURS = 24  # Refresh every 24 hours during active trading day
OPP_ANALYSIS_CACHE = CACHE_DIR / 'opp_analysis_cache.json'
OPP_CACHE_HOURS = 24  # Refresh daily



# Create custom filter for safe formatting
def safe_format(value, format_spec):
    """Safely format a value, converting to float if needed"""
    try:
        if isinstance(value, str):
            value = float(value)
        elif value is None:
            value = 0.0
        return format_spec % value
    except (ValueError, TypeError):
        return "N/A"

# Create environment with custom filter and auto-escaping for security
env = Environment(
    loader=FileSystemLoader('.'),
    autoescape=True  # Enable auto-escaping to prevent XSS attacks
)
env.filters['safe_float'] = safe_float
env.filters['safe_format'] = safe_format

def get_company_name(symbol):
    """Get company name for a symbol using yfinance"""
    try:
        ticker = yf.Ticker(symbol)
        return ticker.info.get('longName') or ticker.info.get('shortName') or symbol
    except Exception as e:
        print(f"Warning: Failed to get company name for {symbol}: {e}")
        return symbol
    
def calculate_conservative_cc(symbol, shares=100, safety_level='safe', dte_pref='21-60'):
    """Calculate best conservative covered call for given symbol and preferences"""
    shares = int(shares)
    contracts = shares // 100
    if contracts == 0:
        return {"error": "Need at least 100 shares for 1 contract"}

    try:
        client = get_client()
        resp = asyncio.run(asyncio.to_thread(client.get_option_chain, symbol))
        chain = resp.json()
        if 'callExpDateMap' not in chain or not chain['callExpDateMap']:
            return {"error": "No call options found for symbol"}

        price = yf.Ticker(symbol).info.get('regularMarketPrice') or chain['underlying']['last']
        if price <= 0:
            return {"error": "Invalid stock price"}
        capital_for_100 = round(price * 100, 2)

        # Safety level mapping
        safety_map = {
            'very_safe': {'delta_max': 0.20, 'dte_min': 30, 'dte_max': 60},
            'safe': {'delta_max': 0.25, 'dte_min': 21, 'dte_max': 60},
            'balanced': {'delta_max': 0.30, 'dte_min': 21, 'dte_max': 45}
        }
        params = safety_map.get(safety_level, safety_map['safe'])

        # DTE preference
        dte_ranges = {
            '21-60': (21, 60),
            '30-45': (30, 45),
            '45-60': (45, 60)
        }
        dte_min, dte_max = dte_ranges.get(dte_pref, (21, 60))

        candidates = []
        for exp, strikes in chain['callExpDateMap'].items():
            try:
                exp_date_str = exp.split(':')[0]
                exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d")
                dte = (exp_date.date() - datetime.now().date()).days
                if not (dte_min <= dte <= dte_max):
                    continue
            except (ValueError, IndexError) as e:
                print(f"Warning: Failed to parse expiration date '{exp}': {e}")
                continue

            for strike_str, contracts_list in strikes.items():
                if not contracts_list:
                    continue
                opt = contracts_list[0]

                bid = opt.get('bid', 0)
                if bid < 0.30:  # Minimum decent premium
                    continue

                delta = abs(opt.get('delta', 0) or 0)
                if delta > params['delta_max'] or delta < 0.05:
                    continue

                oi = opt.get('openInterest', 0) or 0
                if oi < 50:
                    continue

                strike = float(strike_str)
                distance_pct = ((strike - price) / price) * 100
                prob_keep = (1 - delta) * 100
                total_income = bid * 100 * contracts
                annualized = (bid / price) * 100 * (365 / dte) if dte > 0 else 0

                candidates.append({
                    'strike': strike,
                    'dte': dte,
                    'bid': bid,
                    'delta': delta,
                    'distance_pct': distance_pct,
                    'prob_keep': prob_keep,
                    'total_income': total_income,
                    'annualized': annualized,
                    'symbol': opt.get('symbol', '')
                })

        if not candidates:
            return {"error": "No suitable conservative calls found with current filters"}

        # Sort by prob_keep descending (safest first)
        candidates.sort(key=lambda x: x['prob_keep'], reverse=True)
        best = candidates[0]

        # Safety badge
        if best['distance_pct'] > 12:
            badge = "Very Deep OTM 🟢🟢"
        elif best['distance_pct'] > 8:
            badge = "Deep OTM 🟢"
        elif best['distance_pct'] > 4:
            badge = "Safe OTM 🟡"
        else:
            badge = "Near ATM 🟠"

        return {
            "symbol": symbol.upper(),
            "current_price": round(price, 2),
            "capital_for_100": capital_for_100,
            "shares": shares,
            "contracts": contracts,
            "best_strike": best['strike'],
            "best_dte": best['dte'],
            "premium": round(best['bid'], 2),
            "total_income": round(best['total_income'], 0),
            "annualized": round(best['annualized'], 1),
            "prob_keep": round(best['prob_keep'], 0),
            "delta": round(best['delta'], 2),
            "distance_pct": round(best['distance_pct'], 1),
            "badge": badge,
            "option_symbol": best['symbol'],
            "message": f"Strong conservative call: {round(best['prob_keep'], 0)}% chance to keep shares with ${round(best['total_income'], 0)} income."
        }

    except Exception as e:
        return {"error": f"Failed to calculate: {str(e)}"}

def parse_dividend_tile(symbol, lines, roc=False):
    """Extract structured data from dividend bot lines."""
    tile = {'symbol': symbol, 'roc_warning': roc, 'qty': 0, 'avg_price': 0.0, 
            'cost_basis': 0.0, 'market_value': 0.0, 'unrealized_pl': 0.0, 
            'unrealized_pl_pct': 0.0, 'total_div': 0.0, 'ytd_div': 0.0, 'yoc': 0.0}
    
    for line in lines:
        line = line.strip().replace('<b>', '').replace('</b>', '')
        if 'shares' in line:
            tile['qty'] = safe_int(re.search(r'(\d+)', line).group(1))
        elif 'Avg Cost:' in line:
            tile['avg_price'] = safe_float(re.search(r'\$([\d.]+)', line).group(1))
        elif 'Cost Basis:' in line:
            tile['cost_basis'] = safe_float(re.search(r'\$([\d,]+)', line).group(1).replace(',', ''))
        elif 'Value:' in line:
            tile['market_value'] = safe_float(re.search(r'\$([\d,]+)', line).group(1).replace(',', ''))
        elif 'Unrealized P/L:' in line:
            pl_match = re.search(r'\$([\d,]+)', line)
            pct_match = re.search(r'\(([\d.-]+)%\)', line)
            if pl_match: tile['unrealized_pl'] = safe_float(pl_match.group(1).replace(',', ''))
            if pct_match: tile['unrealized_pl_pct'] = safe_float(pct_match.group(1))
        elif 'Lifetime:' in line:
            tile['total_div'] = safe_float(re.search(r'\$([\d,]+)', line).group(1).replace(',', ''))
        elif 'YTD:' in line:
            tile['ytd_div'] = safe_float(re.search(r'\$([\d,]+)', line).group(1).replace(',', ''))
        elif 'Yield on Cost:' in line:
            tile['yoc'] = safe_float(re.search(r'([\d.]+)%', line).group(1))
    
    return tile

def get_dynamic_exit_suggestion(trade):
    """
    Dynamic exit based on theta rate, DTE, and progress.
    """
    entry = safe_float(trade.get('Entry Premium', 0))
    mark = safe_float(trade.get('_current_mark', entry))
    dte = safe_int(trade.get('_dte', 0))
    theta_daily = safe_float(trade.get('_theta', 0))  # Will be 0 if not available
    progress_pct = safe_float(trade.get('_progress_pct', 0))

    if entry <= 0:
        return "HOLD", "No entry data"

    # Base 50% target
    base_target = 50

    if dte < 15 and abs(theta_daily) > 0.20:
        adjusted_target = 45
        reason = f"High theta decay (${-theta_daily:.3f}/day) + low DTE ({dte}) → close early at ~{adjusted_target}% profit"
    elif dte < 7:
        adjusted_target = 35
        reason = f"Very low DTE ({dte}) → lock profits now"
    elif dte > 30 and abs(theta_daily) < 0.15:
        adjusted_target = 65
        reason = f"Slow decay (${-theta_daily:.3f}/day) + long DTE → hold to {adjusted_target}%"
    else:
        adjusted_target = 55
        reason = f"Standard theta curve → target {adjusted_target}% profit"

    current_progress = progress_pct
    action = "CLOSE NOW" if current_progress >= adjusted_target else "HOLD"

    return action, f"{action}: Target {adjusted_target}% (Current: {current_progress:.1f}%) — {reason}"
    
async def run_all_bots():
    with tqdm(total=6, desc="Building Dashboard", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]") as pbar:
        global open_trades, covered_calls, simple_scanner_opps
        global dividend_tiles, portfolio_summary, calendar_events, grok_symbols, leaps_opps, captured_leaps
        global grok_sentiment, grok_summary, trade_history, original_main, scanner_mod, allocation_data, total_wheel_capital, zero_dte_opps

        get_client()

        # === 0DTE SCANNER ===
        pbar.set_description("0DTE Spreads...")

        # Load from cache first
        cache_path = Path("cache_files/0dte_spreads_cache.json")
        if cache_path.exists():
            try:
                with open(cache_path) as f:
                    data = json.load(f)
                zero_dte_opps = data.get('opportunities', [])[:20]
                print(f"Loaded {len(zero_dte_opps)} cached 0DTE opportunities")
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Failed to load 0DTE cache: {e}")
                zero_dte_opps = []
        else:
            zero_dte_opps = []

        if not zero_dte_opps:
            # Run fresh scan (only during market hours ideally)
            try:
                zero_dte_opps = await scan_0dte_spreads()
                print(f"Fresh 0DTE scan: {len(zero_dte_opps)} opportunities")
            except Exception as e:
                print(f"0DTE scanner failed: {e}")
                zero_dte_opps = []

        # Limit for display
        zero_dte_opps = zero_dte_opps[:15]

        pbar.update(1)

        # === LEAPS SCANNER ===
        pbar.set_description("LEAPS Scanner...")

        # Load from dedicated LEAPS cache file
        cached_leaps = load_cached_leaps(LEAPS_CACHE_FILE="leaps_cache.json")
        if cached_leaps is not None:
            leaps_opps = cached_leaps
            print(f"Loaded {len(leaps_opps)} cached LEAPS opportunities")
        else:
            import leaps_scanner as leaps_mod

            try:
                await leaps_mod.main()
                # The new script saves to leaps_cache.json and returns nothing useful,
                # so we load from cache right after run
                leaps_opps = load_cached_leaps(LEAPS_CACHE_FILE="leaps_cache.json") or []
                print(f"Fresh LEAPS scan: {len(leaps_opps)} opportunities generated")
            except Exception as e:
                print(f"LEAPS scanner failed: {e}")
                import traceback
                traceback.print_exc()
                leaps_opps = []

        pbar.update(1)

        # === OPEN TRADES ===
        pbar.set_description("Open Trades...")
        trades_records, ws = load_trades_from_sheet()
        if not trades_records.empty:
           # Get OCC symbols for live quotes
           occ_symbols = []
           for row_dict in trades_records.to_dict('records'):  # trades_records is list of dicts
                   option_symbol = row_dict.get('Option Symbol', '')
                   _, _, _, _, occ = parse_option_symbol(option_symbol)
                   if occ:
                       occ_symbols.append(occ)
           
           schwab_client = get_client()
           if schwab_client and occ_symbols:
               print("Schwab client available — fetching quotes...")
               quotes = await get_live_quotes_concurrent(schwab_client, occ_symbols)
               print(f"Received {len(quotes)} quotes back")
               successful = [k for k, v in quotes.items() if v.get('bid', 0) > 0 or v.get('mark', 0) > 0]
               print(f"{len(successful)} quotes have non-zero bid/mark")
           else:
               print("No Schwab client or no OCC symbols — using empty quotes")
               quotes = {}
           
           enriched_trades = []
           for row_dict in trades_records.to_dict('records'):
               enriched = enrich_trade_with_live_data(row_dict.copy(), quotes)
               enriched_trades.append(enriched)
           
           global open_trades
           open_trades = enriched_trades[:10]  # Limit to top 10 for dashboard
           print(f"Current Open Positions: {len(open_trades)}")
        else:
           open_trades = []
         
        for trade in open_trades:
            symbol = trade.get('Symbol', 'N/A')
            strike = safe_float(trade.get('Strike', 0))
            entry_premium = safe_float(trade.get('Entry Premium', 0))
            contracts = safe_int(trade.get('Contracts Qty', 1))

            # === Live & Fallback Quote Logic ===
            live_mark = safe_float(trade.get('_current_mark', 0))
            live_bid = safe_float(trade.get('_bid', 0))
            live_ask = safe_float(trade.get('_ask', 0))

            sheet_mark = safe_float(trade.get('Current Mark', 0))
            sheet_bid = safe_float(trade.get('Bid', 0))
            sheet_ask = safe_float(trade.get('Ask', 0))

            using_fallback = False

            if live_mark <= 0.01 or (live_bid <= 0 and live_ask <= 0):
                if sheet_mark > 0:
                    current_mark = sheet_mark
                    bid = sheet_bid
                    ask = sheet_ask
                    using_fallback = True
                    print(f"   ⚠️ {symbol}: Using sheet fallback values")
                else:
                    current_mark = live_mark
                    bid = live_bid
                    ask = live_ask
                    print(f"   ⚠️ {symbol}: No valid data — showing $0.00")
            else:
                current_mark = live_mark
                bid = live_bid
                ask = live_ask

            # Store for template
            trade['_current_mark'] = current_mark
            trade['Current Mark'] = f"${current_mark:.2f}"
            trade['Bid'] = f"${bid:.2f}"
            trade['Ask'] = f"${ask:.2f}"
            trade['_bid'] = bid
            trade['_ask'] = ask
            trade['_using_fallback_quotes'] = using_fallback

            # === Core Metrics ===
            # Try multiple keys for underlying price with robust fallback
            underlying_price = safe_float(
                trade.get('_underlying_price',
                trade.get('Underlying Price',
                trade.get('underlying_price',
                trade.get('Underlying', 0))))
            )
            # Ensure it's not 0 - if so, try to calculate from other data
            if underlying_price <= 0 and strike > 0:
                # If price is 0, use strike as fallback (not ideal but better than 0)
                underlying_price = strike
                print(f"   ⚠️ {symbol}: No underlying price found, using strike as fallback: ${underlying_price}")

            trade['_underlying_price'] = underlying_price
            trade['Underlying Price'] = underlying_price

            delta = safe_float(trade.get('Delta', trade.get('delta', trade.get('_delta', 0))))
            iv = safe_float(trade.get('IV', trade.get('iv', trade.get('_iv', 0))))
            dte = trade['_dte'] if '_dte' in trade else safe_int(trade.get('DTE', trade.get('dte', 0)))
            days_open = trade['_days_open'] if '_days_open' in trade else safe_int(trade.get('Days Since Entry', 0))

            # === P/L & Progress ===
            pl_dollars = (entry_premium - current_mark) * 100 * contracts
            trade['_pl_dollars'] = pl_dollars
            trade['Current P/L $'] = f"${pl_dollars:+,.0f}"

            progress_pct = 0.0
            if entry_premium > 0:
                progress_pct = ((entry_premium - current_mark) / entry_premium) * 100
                progress_pct = min(max(progress_pct, 0), 100)
            trade['_progress_pct'] = progress_pct
            trade['Progress to Target'] = f"{progress_pct:.1f}%"
            trade['display_delta'] = f"{abs(delta):.2f}"

            # === Grok Real-Time Profit Probability (Critical for Score) ===
            prob, oneliner = get_grok_opportunity_analysis(
                symbol=symbol,
                price=underlying_price,
                strike=strike,
                dte=dte,
                premium=current_mark,
                delta=abs(delta),
                iv=iv,
                rsi=50,
                vol_surge=1.0,
                in_uptrend=(underlying_price > strike)
            )
            trade['grok_profit_prob'] = prob
            trade['grok_one_liner'] = oneliner

            calculate_trade_score(trade)

            # === ENHANCED TRADE SCORING WITH BADGES ===
            try:
                scorer = TradeScorer()
                # Use score_trade method with the trade dict
                score_result = scorer.score_trade(trade)

                # Store enhanced score data
                trade['trade_score_data'] = score_result
                trade['grok_trade_score'] = score_result.get('overall_score', 0)

                # Generate score badge with color coding
                total_score = score_result.get('overall_score', 0)
                if total_score >= 90:
                    badge = '🔥'  # Fire - Excellent
                    badge_color = '#34d399'
                elif total_score >= 80:
                    badge = '🚀'  # Rocket - Great
                    badge_color = '#60a5fa'
                elif total_score >= 70:
                    badge = '✅'  # Check - Good
                    badge_color = '#4ade80'
                elif total_score >= 60:
                    badge = '⚡'  # Lightning - Fair
                    badge_color = '#fbbf24'
                elif total_score >= 50:
                    badge = '⚠️'  # Warning - Caution
                    badge_color = '#f59e0b'
                else:
                    badge = '🚨'  # Alert - Poor
                    badge_color = '#ef4444'

                trade['score_badge'] = badge
                trade['score_badge_color'] = badge_color
                trade['score_letter_grade'] = score_result.get('grade', 'C')

            except Exception as e:
                print(f"Error scoring trade {symbol}: {e}")
                trade['grok_trade_score'] = trade.get('grok_trade_score', 0)
                trade['score_badge'] = '⚠️'
                trade['score_badge_color'] = '#94a3b8'
                trade['score_letter_grade'] = 'N/A'

            # === DYNAMIC THETA-BASED EXIT SUGGESTION ===
            action, reason = get_dynamic_exit_suggestion(trade)
            trade['exit_suggestion'] = action
            trade['exit_reason'] = reason
            trade['exit_target_pct'] = reason.split("Target ")[1].split("%")[0] + "%" if "Target" in reason else "50%"

            # === Assignment Simulator ===
            profit_if_expires = round(entry_premium * 100 * contracts, 0)
            cost_basis_if_assigned = round(strike - entry_premium, 2)
            unrealized_if_assigned = round((underlying_price - cost_basis_if_assigned) * 100 * contracts, 0)

            risk = "Low"
            if underlying_price > strike * 0.95:
                risk = "High"
            elif underlying_price > strike * 0.90:
                risk = "Moderate"

            trade.update({
                'profit_if_expires_formatted': f"+${profit_if_expires:,.0f}",
                'cost_basis_if_assigned': cost_basis_if_assigned,
                'current_value_formatted': f"${underlying_price * 100 * contracts:,.0f}",
                'unrealized_formatted': f"${abs(unrealized_if_assigned):,.0f} {'gain' if unrealized_if_assigned > 0 else 'loss'}",
                'assignment_risk': risk,
            })

            # === Full Grok Analysis (Optional — Only if Missing) ===
            if not trade.get('grok_analysis'):
                prompt = (
                    f"Short {symbol} ${strike:.2f}P exp {trade.get('Exp Date', 'N/A')} | "
                    f"Entry ${entry_premium:.2f} → Mark ${current_mark:.2f} ({progress_pct:.1f}%) | "
                    f"DTE {dte} | ${underlying_price:.2f} | Delta {abs(delta):.2f} IV {iv:.1f}% | "
                    f"P/L ${pl_dollars:+.0f}\n"
                    "Advise: Close/Hold/Roll? Key risks? Under 80 words."
                )
                trade['grok_analysis'] = call_grok(
                    [{"role": "system", "content": "You are a wheel strategy position manager. Be concise."},
                     {"role": "user", "content": prompt}],
                    model=MODEL_FAST,
                    max_tokens=150,
                )

        pbar.update(1)

        pbar.set_description("Capital Allocation...")
        
        # === CAPITAL ALLOCATION OPTIMIZER ===
        total_wheel_capital = float(os.getenv('WHEEL_CAPITAL', 25000))  # Your total wheel cash
        max_per_trade_pct = 0.20  # 20% max per trade
        max_positions = 10

        if open_trades:
            # Calculate current capital at risk
            capital_at_risk = 0
            sector_exposure = {}
            for trade in open_trades:
                symbol = trade.get('Symbol', 'UNKNOWN')
                strike = safe_float(trade.get('Strike', 0))
                contracts = safe_int(trade.get('Contracts Qty', 1))
                capital = strike * 100 * contracts
                capital_at_risk += capital

                # Simple sector mapping (expand as needed)
                sector_map = {
                    'Tech': ['AAPL', 'MSFT', 'NVDA', 'AMD', 'META', 'AMZN', 'GOOGL', 'NFLX',
                            'ADBE', 'CRM', 'ORCL', 'QCOM', 'LRCX', 'MU', 'ASML', 'KLAC', 'MRVL',
                            'SNPS', 'CDNS', 'PANW', 'SHOP', 'AVGO', 'TSM', 'INTC', 'TXN', 'BABA',
                            'CRWD', 'PLTR', 'ROKU', 'SNAP', 'ZS', 'DASH', 'NET', 'DDOG', 'MDB', 'ZM',
                            'TQQQ', 'SOXL', 'TDAQ', 'HIMS'],  
                    'Energy': ['XOM', 'CVX', 'OXY', 'BP', 'HAL', 'SLB', 'APA', 'XLE', 'FCX', 'CLF', 'SMR'],
                    'Consumer Staples': ['KO', 'PG', 'WMT', 'COST', 'PEP', 'MCD', 'SBUX', 'CMG'],
                    'Healthcare': ['JNJ', 'MRK', 'BMY', 'ABBV', 'PFE', 'LLY', 'UNH'],
                    'Financials': ['JPM', 'BAC', 'GS', 'MS', 'V', 'MA', 'AXP', 'PYPL', 'BLK', 'C', 'USB', 'WFC', 'PNC', 'TFC',
                                'CFG', 'FITB', 'KEY', 'RF', 'ZION'],
                    'Industrials': ['CAT', 'DE', 'HD', 'LOW', 'UPS', 'FDX', 'GE', 'LMT', 'RTX'],
                    'Utilities': ['DUK', 'SO'],
                    'Real Estate': ['O'],
                    'Telecom': ['VZ', 'T'],
                    'Materials': ['NEM', 'VALE', 'FCX'],
                    'Uranium/Nuclear': ['CCJ', 'LEU', 'CEG', 'BWXT', 'NLR'],
                    'Crypto/Blockchain': ['COIN', 'MARA', 'RIOT', 'MSTR', 'IBIT', 'IREN', 'CCCX'],  # Bitcoin miners + spot ETFs
                    'Travel/Hospitality': ['EXPE', 'NCLH', 'CCL', 'MAR', 'RCL', 'DAL', 'LUV', 'UAL', 'ABNB', 'UBER'],
                    'Gaming/Entertainment': ['DKNG', 'PINS', 'ROKU', 'DIS'],
                    'Renewables': ['ENPH', 'FSLR'],
                    'Biotech': ['BNTX'],
                    'Retail/Consumer': ['NKE', 'TGT'],
                    'Leveraged ETFs': ['TQQQ', 'SOXL'],
                    'Broad Market': ['SPY', 'QQQ', 'IWM', 'DIA'],
                    'Precious Metals': ['GLD'],  # Gold ETF
                }

                # Then in the loop:
                sector = 'Other'
                for sec, symbols in sector_map.items():
                    if symbol in symbols:
                        sector = sec
                        break

                sector_exposure[sector] = sector_exposure.get(sector, 0) + capital

            pct_allocated = (capital_at_risk / total_wheel_capital) * 100 if total_wheel_capital else 0
            remaining_capital = total_wheel_capital - capital_at_risk
            room_for_trades = int(remaining_capital / (total_wheel_capital * max_per_trade_pct))

            allocation_data = {
                'total_capital': total_wheel_capital,
                'capital_at_risk': capital_at_risk,
                'pct_allocated': round(pct_allocated, 1),
                'positions_open': len(open_trades),
                'max_positions': max_positions,
                'remaining_capital': remaining_capital,
                'room_for_trades': room_for_trades,
                'max_per_trade': total_wheel_capital * max_per_trade_pct,
                'sector_exposure': sector_exposure
            }
        else:
            allocation_data = {
                'total_capital': total_wheel_capital,
                'capital_at_risk': 0,
                'pct_allocated': 0,
                'positions_open': 0,
                'max_positions': max_positions,
                'remaining_capital': total_wheel_capital,
                'room_for_trades': int(1 / max_per_trade_pct),
                'max_per_trade': total_wheel_capital * max_per_trade_pct,
                'sector_exposure': {}
            }

        print(f"Capital Optimizer: {allocation_data['pct_allocated']}% allocated, room for {allocation_data['room_for_trades']} more trades")
        pbar.update(1)

        # === COVERED CALLS ===
        pbar.set_description("Covered Calls...")
        positions = get_current_positions()
        if positions:
            tasks = [find_covered_calls(sym, pos) for sym, pos in positions.items()]
            results = await asyncio.gather(*tasks)
            covered_calls = []
            for sym, calls in zip(positions.keys(), results):
                if calls:
                    covered_calls.append({
                        "symbol": sym, 
                        "shares": positions[sym]['quantity'], 
                        "calls": calls[:3]
                    })
        else:
            covered_calls = []
        pbar.update(1)

        # === SIMPLE SCANNER ===
        pbar.set_description("Simple Scanner...")

        cached_scanner_opps = load_cached_scanner()
        if cached_scanner_opps is not None:
            simple_scanner_opps = cached_scanner_opps
            print(f"Loaded {len(simple_scanner_opps)} cached scanner tiles")
        else:
            import simple_options_scanner as scanner_mod

            original_send_alert = getattr(scanner_mod, 'send_alert', None)
            scanner_mod.captured_opportunities = []

            success = False
            try:
                await scanner_mod.main()
                success = True
            except Exception as e:
                print(f"Simple scanner run failed: {e}")
                import traceback
                traceback.print_exc()

            # Always define both variables
            simple_scanner_opps = scanner_mod.captured_opportunities if success else []

            print(f"Fresh scan: {len(simple_scanner_opps)} scanner tiles generated")

            if simple_scanner_opps:
                save_cached_scanner(simple_scanner_opps)
                print(f"Cached {len(simple_scanner_opps)} scanner tiles")
            else:
                print("No scanner opportunities — skipping cache")

        # Calculate Bollinger Bands for each scanner opportunity symbol
        print("Calculating Bollinger Bands for scanner symbols...")
        bb_cache = {}
        for tile in simple_scanner_opps:
            if isinstance(tile, dict) and 'suggestions' in tile:
                symbol = tile.get('symbol')
                if symbol and symbol not in bb_cache:
                    try:
                        ticker = yf.Ticker(symbol)
                        hist = ticker.history(period='3mo')
                        if not hist.empty:
                            bb_data = calculate_bollinger_bands(hist)
                            if bb_data:
                                bb_cache[symbol] = bb_data
                    except Exception as e:
                        print(f"BB calc failed for {symbol}: {e}")

        # Add BB data to tiles
        for tile in simple_scanner_opps:
            if isinstance(tile, dict):
                symbol = tile.get('symbol')
                if symbol in bb_cache:
                    tile['bollinger_bands'] = bb_cache[symbol]

        # Enrich opportunities with S/R levels from dedicated cache (32-day TTL)
        # This ensures S/R levels persist even when scanner cache is regenerated
        print("Enriching scanner opportunities with S/R levels...")
        sr_cache = load_sr_cache()
        sr_enriched_count = 0
        for tile in simple_scanner_opps:
            if isinstance(tile, dict) and 'suggestions' in tile:
                symbol = tile.get('symbol', '').upper()
                for opp in tile['suggestions']:
                    # Only add S/R if missing or empty
                    if not opp.get('support_resistance'):
                        if symbol in sr_cache:
                            sr_entry = sr_cache[symbol]
                            sr_levels = sr_entry.get('levels', {})
                            if sr_levels:
                                opp['support_resistance'] = sr_levels
                                sr_enriched_count += 1
        if sr_enriched_count > 0:
            print(f"Enriched {sr_enriched_count} opportunities with cached S/R levels")

        pbar.update(1)

         # Trade History...
        pbar.set_description("Trade History...")
        trade_history = []
        try:
            if not os.path.exists('google-credentials.json'):
                print("⚠️  google-credentials.json not found. Trade History will be empty.")
                trade_history = []
            else:
                gc = gspread.service_account(filename='google-credentials.json')
                sh = gc.open_by_key("1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc")
                history_ws = sh.worksheet("Trade_History")
                records = history_ws.get_all_records()
                print(f"Found {len(records)} total records in Trade_History sheet")
                trade_history = [r for r in records if r.get('Exit Date')]  # Only closed
                print(f"Found {len(trade_history)} closed trades (with Exit Date)")
                # Optional: enrich with strike formatting
                for t in trade_history:
                    strike = safe_float(t.get('Strike', 0))
                    t['strike_display'] = f"${strike:.2f}P" if strike > 0 else "N/A"
        except Exception as e:
            print(f"❌ Trade history load failed: {e}")
            import traceback
            traceback.print_exc()
            trade_history = []
        pbar.update(1)

        #=======Dividend Tracker=======
        pbar.set_description("Dividend Tracker...")
        dividend_tiles = await generate_dividend_report()
        if not dividend_tiles:
            dividend_tiles = []
            print("Dividend tracker returned no data")
        print(f"Loaded {len(dividend_tiles)} dividend positions")
        pbar.update(1)

        ###====Position Symbols Helper=====
        position_symbols = []
        for pos in positions:
            if isinstance(pos, dict):
                instr = pos.get('instrument', {})
                if isinstance(instr, dict):
                    sym = instr.get('symbol')
                    if sym:
                        position_symbols.append(sym)

        # Calendar Events...
        pbar.set_description("Calendar Events...")
        calendar_events = []

        client = get_client()
        try:
            # Use last 90 days to avoid issues with date ranges
            end = datetime.now()
            start = end - timedelta(days=90)

            # Schwab API might need specific date format - try different approaches
            try:
                tx_resp = client.get_transactions(ACCOUNT_ID, start_date=start, end_date=end)
            except TypeError:
                # If that fails, try without keyword arguments
                tx_resp = client.get_transactions(ACCOUNT_ID, start, end)

            if tx_resp.status_code != 200:
                print(f"Schwab transactions fetch failed with status {tx_resp.status_code}")
                print(f"  Response: {tx_resp.text if hasattr(tx_resp, 'text') else 'No response text'}")
                transactions = []
            else:
                raw = tx_resp.json()
                transactions = raw if isinstance(raw, list) else raw.get('transactions', [])
                print(f"Found {len(transactions)} transactions")

            # Keyword mapping from description to your ETF symbol
            desc_to_symbol = {
                'HOOD WEEKLYPAYETF': 'HOOW',
                'PLTR WEEKLYPAYETF': 'PLTW',
                'TSLA WEEKLYPAYETF': 'TSLW',
                'NVDA WEEKLYPAYETF': 'NVDW',
                'META WEEKLYPAYETF': 'METW',
                'AMZN WEEKLYPAYETF': 'AMZW',
                'MSTR WEEKLYPAYETF': 'MSTW',
                'NICHOLAS CRYPTO INCOME ETF': 'BLOX',
                'ISHARES 0-3 MONTH': 'SGOV',
                'YIELDMAX ULTRA': 'YMAX',
            }

            div_by_date = {}
            for tx in transactions:
                tx_type = tx.get('type', '') or tx.get('transactionType', '')
                if 'DIVIDEND' in tx_type.upper() or 'CASH_DIVIDEND' in tx_type.upper():
                    amount = abs(tx.get('netAmount', 0))
                    if amount == 0:
                        continue
                    tx_date_str = tx.get('transactionDate', '') or tx.get('settlementDate', '')
                    if not tx_date_str:
                        continue
                    try:
                        tx_date = datetime.strptime(tx_date_str.split('T')[0], '%Y-%m-%d')
                        date_key = tx_date.strftime('%b %d')
                    except (ValueError, IndexError) as e:
                        print(f"Warning: Failed to parse transaction date '{tx_date_str}': {e}")
                        continue

                    desc = tx.get('description', '').upper()
                    sym = 'Cash'
                    for keyword, etf in desc_to_symbol.items():
                        if keyword in desc:
                            sym = etf
                            break

                    if date_key not in div_by_date:
                        div_by_date[date_key] = {"total": 0, "items": []}
                    div_by_date[date_key]["total"] += amount
                    div_by_date[date_key]["items"].append({"symbol": sym, "amount": amount})

            # Create events
            for date_key in sorted(div_by_date.keys(), key=lambda x: datetime.strptime(x + f" {datetime.now().year}", '%b %d %Y'), reverse=True):
                event = div_by_date[date_key]
                calendar_events.append({
                    "date": date_key,
                    "total": event["total"],
                    "items": event["items"]
                })

        except Exception as e:
            print(f"Schwab calendar fetch error: {e}")

        calendar_events = calendar_events[:30]
        pbar.update(1)

        pbar.set_description("Grok Analysis...")       
        # GROK Symbols from open trades and covered calls
        grok_symbols = sorted(position_symbols)
        pbar.update(1)
    
    # Simple Scanner
    for opp in simple_scanner_opps:
        if not isinstance(opp, dict):
            print(f"Skipping non-dict opportunity: {opp}")
            continue

        prob, oneliner = get_grok_opportunity_analysis(
        symbol=opp.get('symbol', 'UNKNOWN'),
        price=opp.get('current_price') or opp.get('underlying_price') or opp.get('price', 100.0),
        strike=opp.get('strike') or opp.get('strike_price') or opp.get('strikePrice', 100.0),
        dte=opp.get('dte') or opp.get('days_to_expiration', 30),
        premium=opp.get('premium', 1.0),
        delta=opp.get('delta', 0.3),
        iv=opp.get('iv') or opp.get('volatility', 30),
        rsi=opp.get('rsi', 50),
        vol_surge=opp.get('vol_surge', 1.0),
        in_uptrend=opp.get('in_uptrend', True)
    )
        opp['grok_profit_prob'] = prob
        opp['grok_one_liner'] = oneliner
        calculate_trade_score(opp)

def generate_html():
    template_str = r"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Kyle's Dashboard - {{ now }}</title>
            <meta http-equiv="refresh" content="1800">
            <style>
                body {
                    font-family: 'Segoe UI', system-ui, sans-serif;
                    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                    color: #e2e8f0;
                    margin: 0;
                    padding: 20px;
                    min-height: 100vh;
                }
                .container { max-width: 1600px; margin: auto; }
                h1 {
                    text-align: center;
                    color: #60a5fa;
                    font-size: 2.5rem;
                    margin-bottom: 10px;
                    text-shadow: 0 0 20px rgba(96, 165, 250, 0.5);
                }
                .subtitle { text-align: center; color: #94a3b8; font-size: 1.1rem; margin-bottom: 30px; }
                .accordion {
                    margin-bottom: 15px;
                    border: 1px solid #334155;
                    border-radius: 12px;
                    overflow: hidden;
                }
                .accordion-header {
                    background: linear-gradient(135deg, #1e293b, #0f172a);
                    padding: 18px 24px;
                    cursor: pointer;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    transition: background 0.3s;
                }
                .accordion-header:hover {
                    background: #334155;
                }
                .accordion-content {
                    max-height: 0;
                    overflow: hidden;
                    transition: max-height 0.4s ease;
                }
                .accordion-content.open {
                    max-height: 3500px;
                    padding: 20px;
                }
                .action-buttons {
                    text-align: center;
                    margin: 30px 0;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 12px;
                    justify-content: center;
                }
                .action-btn {
                    padding: 14px 28px;
                    border: none;
                    border-radius: 12px;
                    font-weight: bold;
                    cursor: pointer;
                    font-size: 1rem;
                    transition: all 0.3s;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
                }

                /* Quick Action Buttons */
                .action-buttons-csp {
                    margin-top: auto;
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 12px;
                    padding-top: 12px;
                    border-top: 1px dashed #334155;
                }
                .action-btn-close {
                    background: #34d399;
                    color: #064e3b;
                }
                .action-btn-roll {
                    background: #f59e0b;
                    color: white;
                }
                .action-btn:hover {
                    transform: translateY(-4px);
                    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
                }

                .button-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 24px;
                margin: 30px 0;
                }
                .script-buttons-container {
                    position: absolute;
                    top: 15px;
                    right: 20px;
                    display: flex;
                    gap: 8px;
                    flex-wrap: nowrap;
                    justify-content: flex-end;
                    align-items: center;
                    z-index: 100;
                }
                .script-button {
                    position: relative;
                    padding: 4px 10px;
                    height: 28px;
                    font-size: 0.7rem;
                    font-weight: 600;
                    color: white;
                    background: linear-gradient(135deg, #1e293b, #0f172a);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 6px;
                    cursor: pointer;
                    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3),
                                inset 0 1px 0 rgba(255, 255, 255, 0.1);
                    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                    overflow: hidden;
                    white-space: nowrap;
                    text-align: center;
                    backdrop-filter: blur(10px);
                }
                .script-button::before {
                    content: '';
                    position: absolute;
                    top: 0;
                    left: -100%;
                    width: 100%;
                    height: 100%;
                    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
                    transition: left 0.5s;
                }
                .script-button:hover::before {
                    left: 100%;
                }
                .script-button:hover {
                    transform: translateY(-3px) scale(1.02);
                    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4),
                                0 0 20px rgba(96, 165, 250, 0.3),
                                inset 0 1px 0 rgba(255, 255, 255, 0.2);
                    border-color: rgba(96, 165, 250, 0.5);
                }
                .script-button:active {
                    transform: translateY(-1px) scale(0.98);
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
                }
                #btn-scanner {
                    background: linear-gradient(135deg, #f59e0b 0%, #ea580c 100%);
                    border-color: rgba(251, 146, 60, 0.3);
                }
                #btn-scanner:hover {
                    box-shadow: 0 8px 30px rgba(234, 88, 12, 0.4),
                                0 0 25px rgba(251, 146, 60, 0.3);
                }
                #btn-leaps {
                    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                    border-color: rgba(16, 185, 129, 0.3);
                }
                #btn-leaps:hover {
                    box-shadow: 0 8px 30px rgba(5, 150, 105, 0.4),
                                0 0 25px rgba(16, 185, 129, 0.3);
                }
                #btn-covered {
                    background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
                    border-color: rgba(167, 139, 250, 0.3);
                }
                #btn-covered:hover {
                    box-shadow: 0 8px 30px rgba(124, 58, 237, 0.4),
                                0 0 25px rgba(167, 139, 250, 0.3);
                }
                #btn-dividends {
                    background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                    border-color: rgba(248, 113, 113, 0.3);
                }
                #btn-dividends:hover {
                    box-shadow: 0 8px 30px rgba(220, 38, 38, 0.4),
                                0 0 25px rgba(248, 113, 113, 0.3);
                }
                .script-button:not(#btn-scanner):not(#btn-leaps):not(#btn-covered):not(#btn-dividends) {
                    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
                    border-color: rgba(96, 165, 250, 0.3);
                }
                .script-button:not(#btn-scanner):not(#btn-leaps):not(#btn-covered):not(#btn-dividends):hover {
                    box-shadow: 0 8px 30px rgba(37, 99, 235, 0.4),
                                0 0 25px rgba(96, 165, 250, 0.3);
                }
                .action-btn:hover { transform: translateY(-4px); box-shadow: 0 8px 25px rgba(0,0,0,0.5); }
                .status { text-align: center; font-weight: bold; font-size: 1.2rem; min-height: 30px; }
                .tabs {
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    justify-content: center;
                    margin: 40px 0 20px 0;
                }
                .tab-btn {
                    padding: 12px 28px;
                    background: #1e293b;
                    border: none;
                    color: #e2e8f0;
                    border-radius: 50px;
                    cursor: pointer;
                    font-weight: bold;
                    transition: all 0.3s;
                    box-shadow: 0 4px 10px rgba(0,0,0,0.2);
                }
                .tab-btn.active, .tab-btn:hover {
                    background: #3b82f6;
                    transform: translateY(-2px);
                    box-shadow: 0 8px 20px rgba(59, 130, 246, 0.4);
                }
                .tab-content { display: none; padding: 20px 0; }
                .tab-content.active { display: block; animation: fadeIn 0.5s; }
                @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

                .grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); /* Slightly wider min-width */
                    gap: 32px;                      /* Consistent row/column gap */
                    width: 100%;
                    padding: 20px 0;
                    box-sizing: border-box;
                }

                #open-csp-grid {
                    display: grid !important;
                    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)) !important;
                    gap: 32px !important;
                    width: 100%;
                }

                .accordion-item {
                    margin-bottom: 16px;
                    border-radius: 16px;
                    overflow: hidden;
                    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                }

                .accordion-header {
                    padding: 20px 28px;
                    background: linear-gradient(135deg, #1e293b, #0f172a);
                    cursor: pointer;
                    border: 2px solid #334155;
                    border-radius: 16px;
                    user-select: none;
                    transition: all 0.3s;
                }

                .accordion-header:hover {
                    background: linear-gradient(135deg, #263344, #1e293b);
                    border-color: #475569;
                }

                .accordion-content {
                    max-height: 0;
                    overflow: hidden;
                    transition: max-height 0.5s ease;
                    background: #0f172a;
                }

                /* Prevent any child overflow from breaking layout */
                .grid > * {
                    overflow: hidden;
                }
                .tile {
                    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                    border-radius: 16px;
                    border: 1px solid #334155;
                    box-shadow: 0 8px 25px rgba(0,0,0,0.4);
                    transition: all 0.3s ease;
                    height: 200px;
                    min-height: 150px;
                    padding: 20px;
                    display: flex;
                    flex-direction: column;
                    justify-content: flex-start;
                }
                .tile_cc {
                    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                    border-radius: 16px;
                    border: 1px solid #334155;
                    box-shadow: 0 8px 25px rgba(0,0,0,0.4);
                    transition: all 0.3s ease;
                    height: 250px;
                    min-height: 225px;
                    padding: 20px;
                    display: flex;
                    flex-direction: column;
                    justify-content: flex-start;
                }
                .tile_csp {
                    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                    padding: 24px;
                    border-radius: 20px;
                    border: 1px solid #334155;
                    box-shadow: 0 12px 35px rgba(0, 0, 0, 0.6);
                    transition: all 0.4s ease;
                    height: 1500px;                  /* Slightly taller — ensures Grok visible */
                    display: flex;
                    flex-direction: column;
                    gap: 12px;                      /* Tight but balanced spacing */
                    overflow: hidden;               /* Clean edges */
                    width: 100%;
                    box-sizing: border-box;
                }

                /* Reduce specific large gaps */
                .tile_csp > p[style*="margin:16px 0"] {
                    margin: 8px 0 !important;       /* Entry Premium margin */
                }

                .tile_csp > div[style*="grid-template-columns: 1fr 1fr"] {
                    gap: 12px;
                    margin: 8px 0;
                }

                /* Tighten specific sections */
                .tile_csp h3 {
                    margin: 0 0 6px 0;
                    font-size: 1.5rem;
                    line-height: 1.2;
                }

                .tile_csp p {
                    margin: 4px 0;
                    font-size: 0.98rem;
                    line-height: 1.4;
                }

                /* Ensure no extra margin from child elements */
                .tile_csp > * {
                    margin-top: 0;
                    margin-bottom: 0;
                }

                /* Hover Lift */
                .tile_csp:hover {
                    transform: translateY(-8px);
                    z-index: 10;
                    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8);
                }

                /* 100% Progress Winners */
                .tile_csp[data-progress="100"] {
                    border: 2px solid #34d399;
                    box-shadow: 0 0 35px rgba(52, 211, 153, 0.5);
                }

                .tile_csp[data-progress="100"] h3::after {
                    content: " 🎉 100% TARGET HIT";
                    color: #34d399;
                    font-size: 1rem;
                    font-weight: bold;
                    margin-left: 8px;
                }

                /* Market closed / fallback quote notice */
                .quote-note {
                    background: rgba(251, 146, 60, 0.15);
                    border: 1px dashed #fb923c;
                    padding: 12px;
                    border-radius: 12px;
                    color: #fed7aa;
                    text-align: center;
                    font-size: 0.95rem;
                    font-weight: 600;
                    margin: 10px 0;
                }

                /* Make theta row pop more */
                .theta-row {
                    background: rgba(52, 211, 153, 0.15);
                    padding: 12px;
                    border-radius: 14px;
                    border-left: 5px solid #34d399;
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 10px;
                    font-size: 1rem;
                    margin: 6px 0;
                }

                /* Assignment Simulator — make it stand out and not get cut off */
                .assignment-sim {
                    background: linear-gradient(135deg, #1e293b, #16313f);
                    border: 1px solid #0ea5e9;
                    border-radius: 14px;
                    padding: 14px;
                    font-size: 0.94rem;
                    line-height: 1.45;
                    margin: 6px 0;
                    margin-bottom: 12px;
                }

                /* Grok box — better contrast and expand on hover */
                .grok-insight {
                    background: linear-gradient(135deg, rgba(22, 101, 52, 0.95), rgba(6, 78, 59, 1));
                    border: 1px solid #10b981;
                    border-top: 2px solid #10b981;
                    border-radius: 16px;
                    padding: 16px;
                    margin-top: 8px;
                    min-height: 180px;             
                    display: flex;
                    flex-direction: column;
                }

                .grok-insight .grok-probability {
                    font-weight: bold;
                    color: #9ff2d6;
                    font-size: 1.125rem;
                    margin-bottom: 10px;
                }

                .grok-insight .grok-oneliner {
                    font-size: 0.94rem;
                    line-height: 1.5;
                    color: #e0f7ef;
                }

                .refresh-btn {
                    background: linear-gradient(135deg, #065f46, #047857);
                    color: #d1fae5;
                    border: 1px solid #10b981;
                    border-radius: 8px;
                    padding: 8px 16px;
                    font-size: 0.95rem;
                    cursor: pointer;
                    transition: all 0.3s ease;
                    margin-top: 12px;
                    width: 100%;
                }

                .refresh-btn:hover {
                    background: linear-gradient(135deg, #047857, #065f46);
                    transform: translateY(-2px);
                    box-shadow: 0 4px 12px rgba(16,185,129,0.3);
                }

                .refresh-btn:disabled {
                    opacity: 0.6;
                    cursor: not-allowed;
                    transform: none;
                }

                .grok-oneliner strong {
                    color: #9ff2d6;
                    font-size: 1.05rem;
                }

                .grok-oneliner br + strong {
                    color: #fb923c;                 /* Orange for "Risks" */
                }

                .grok-insight:hover {
                    min-height: 160px;
                    border-color: #34d399;
                    box-shadow: 0 8px 30px rgba(52, 211, 153, 0.5);
                }
               /* Grok AI Insights Response */
                #grok-response {
                    background: linear-gradient(135deg, rgba(22, 101, 52, 0.95), rgba(6, 78, 59, 1));
                    border: 2px solid #10b981;
                    border-radius: 20px;
                    padding: 28px;
                    margin: 30px 0;
                    min-height: 200px;
                    max-height: 700px;
                    overflow-y: auto;
                    color: #e0f7ef;
                    font-size: 1.05rem;
                    line-height: 1.8;
                    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
                    white-space: pre-wrap;
                    word-wrap: break-word;
                }
                #grok-response strong {
                    color: #9ff2d6;
                    font-size: 1.1rem;
                }
                #grok-response:hover {
                    max-height: 900px;
                    border-color: #34d399;
                    box-shadow: 0 16px 50px rgba(52, 211, 153, 0.4);
                }
                /* Custom scrollbar */
                #grok-response::-webkit-scrollbar {
                    width: 8px;
                }
                #grok-response::-webkit-scrollbar-track {
                    background: rgba(0,0,0,0.3);
                    border-radius: 10px;
                }
                #grok-response::-webkit-scrollbar-thumb {
                    background: #34d399;
                    border-radius: 10px;
                }
                /* Grok Trade Performance Analysis - Premium Styling */
                #grok-trade-analysis {
                    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                    border: 1px solid #334155;
                    border-radius: 20px;
                    padding: 32px;
                    margin: 30px 0;
                    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
                    color: #e0f7ef;
                    font-size: 1.05rem;
                    line-height: 1.7;
                    max-width: 1200px;
                    margin-left: auto;
                    margin-right: auto;
                }

                #grok-trade-analysis h3 {
                    color: #34d399;
                    font-size: 1.6rem;
                    margin: 28px 0 16px 0;
                    border-bottom: 2px solid #10b981;
                    padding-bottom: 8px;
                }

                #grok-trade-analysis h4 {
                    color: #60a5fa;
                    font-size: 1.3rem;
                    margin: 20px 0 10px 0;
                }

                #grok-trade-analysis p {
                    margin: 16px 0;
                    line-height: 1.7;
                }

                #grok-trade-analysis strong {
                    color: #9ff2d6;
                    font-weight: bold;
                }

                #grok-trade-analysis table {
                    width: 100%;
                    border-collapse: collapse;
                    margin: 20px 0;
                    background: rgba(6, 78, 59, 0.6);
                    border-radius: 12px;
                    overflow: hidden;
                    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4);
                }

                #grok-trade-analysis th {
                    background: linear-gradient(135deg, #065f46, #064e3b);
                    color: #34d399;
                    padding: 14px;
                    text-align: center;
                    font-weight: bold;
                    font-size: 1.05rem;
                    border-bottom: 2px solid #10b981;
                }
                #grok-trade-analysis td {
                    padding: 12px 14px;
                    text-align: center;
                    border-bottom: 1px solid #334155;
                    color: #e0f7ef;
                }

                #grok-trade-analysis tr:hover td {
                    background: rgba(52, 211, 153, 0.15);
                }

                #grok-trade-analysis tr:last-child td {
                    border-bottom: none;
                }

                #grok-trade-analysis tr:nth-child(even) {
                    background: rgba(0, 0, 0, 0.2);
                }

                /* Highlight best categories */
                #grok-trade-analysis .highlight-row {
                    background: rgba(52, 211, 153, 0.2) !important;
                    border-left: 4px solid #34d399;
                }

                #grok-trade-analysis .insight-highlight {
                    background: rgba(52, 211, 153, 0.15);
                    padding: 16px;
                    border-left: 5px solid #34d399;
                    border-radius: 10px;
                    margin: 20px 0;
                    font-style: italic;
                }
                #grok-trade-analysis tr.highlight td {
                    background: rgba(52, 211, 153, 0.25) !important;
                    border-left: 4px solid #34d399;
                }

                .tile_scanner {
                    background: linear-gradient(145deg, #1e293b, #111827);
                    padding: 24px;
                    border-radius: 20px;
                    border: 1px solid rgba(96, 165, 250, 0.2);  /* Softer, more subtle blue glow */
                    box-shadow: 
                        0 10px 30px rgba(0, 0, 0, 0.5),
                        0 0 20px rgba(96, 165, 250, 0.08);     /* Reduced intensity for elegance */
                    transition: all 0.3s ease;
                    min-height: 540px;                            /* Slightly taller for better breathing room */
                    display: flex;
                    flex-direction: column;
                    gap: 18px;
                    position: relative;
                    overflow: hidden;
                }

                /* Subtle inner glow on hover */
                .tile_scanner:hover {
                    transform: translateY(-10px);
                    box-shadow: 
                        0 20px 50px rgba(0, 0, 0, 0.6),
                        0 0 30px rgba(96, 165, 250, 0.15);
                    border-color: rgba(96, 165, 250, 0.4);
                }

                /* Optional: Add a faint top accent bar */
                .tile_scanner::before {
                    content: '';
                    position: absolute;
                    top: 0;
                    left: 0;
                    right: 0;
                    height: 4px;
                    background: linear-gradient(90deg, #60a5fa, #3b82f6);
                    border-radius: 20px 20px 0 0;
                }

                .tile_scanner h3 {
                    color: #60a5fa;
                    font-size: 1.5rem;
                    margin: 0;
                    font-weight: 600;
                    display: flex;
                    align-items: center;
                    gap: 12px;
                }

                .tile_scanner h3 span {
                    font-size: 2rem;
                    filter: drop-shadow(0 0 6px currentColor);
                }

                .tile_scanner p {
                    margin: 8px 0;
                    line-height: 1.6;
                    color: #cbd5e1;
                }

                .tile_scanner .exit-box {
                    background: linear-gradient(135deg, #065f46, #064e3b);
                    padding: 16px;
                    border-radius: 14px;
                    border: 1px solid #10b981;
                    text-align: center;
                    font-weight: bold;
                    font-size: 1.1rem;
                    margin-top: auto;
                    color: #d1fae5;
                }

                .leaps-tile {
                    background: linear-gradient(135deg, #1e3a8a, #1e40af);
                    border-left: 5px solid #60a5fa;
                }

                .leaps-tile:hover {
                    transform: translateY(-5px);
                    box-shadow: 0 12px 35px rgba(30, 58, 138, 0.8);
                }

                tile_allocation {
                    height: 400px;
                    padding: 24px;
                    border-radius: 12px;
                    grid-column: 1 / -1;
                }
                .tile_summary {
                    height: 150px;
                    padding: 24px;
                    grid-column: 1 / -1;
                }

                .tile_summary h3 {
                    margin-top: 0;
                    margin-bottom: 16px;
                }
                .tile:hover {
                    transform: translateY(-8px);
                    box-shadow: 0 20px 40px rgba(0,0,0,0.6);
                    border-color: #60a5fa;
                }

                .trade-card {
                    background: linear-gradient(135deg, #1e293b, #0f172a);
                    border: 2px solid #334155;
                    border-radius: 16px;
                    padding: 20px;
                    transition: all 0.3s ease;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.4);
                }
                .trade-card:hover {
                    transform: translateY(-6px);
                    box-shadow: 0 15px 35px rgba(0,0,0,0.6);
                    border-color: #60a5fa;
                }
                .trade-card .card-header {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 16px;
                    padding-bottom: 12px;
                    border-bottom: 1px solid #334155;
                }
                .trade-card .card-header h3 {
                    margin: 0;
                    color: #60a5fa;
                    font-size: 1.3rem;
                }
                .trade-card .card-body {
                    margin-bottom: 16px;
                }
                .trade-card .detail-row {
                    margin: 8px 0;
                    color: #cbd5e1;
                    font-size: 0.95rem;
                }
                .trade-card .detail-row strong {
                    color: #e2e8f0;
                }
                .trade-card .detail-row.highlight {
                    font-size: 1.05rem;
                    margin: 12px 0;
                }
                .trade-card .profit {
                    color: #34d399;
                    font-weight: bold;
                }
                .trade-card .loss {
                    color: #fb923c;
                    font-weight: bold;
                }
                .trade-card .target {
                    color: #fbbf24;
                    font-weight: bold;
                }
                .trade-card .prob {
                    color: #60a5fa;
                    font-weight: bold;
                }
                .trade-card .grok-section {
                    background: linear-gradient(135deg, rgba(22, 101, 52, 0.3), rgba(6, 78, 59, 0.4));
                    border: 1px solid #10b981;
                    border-radius: 10px;
                    padding: 12px;
                    margin-top: 12px;
                }
                .trade-card .grok-section strong {
                    color: #34d399;
                }

                .zero-dte-card .short-leg { font-weight: bold; color: #fb923c; }  /* Orange-red for short legs */
                .zero-dte-card .long-leg { color: #94a3b8; }                    /* Gray for long legs */
                .spread-row { font-family: monospace; letter-spacing: 0.5px; }

                .score-badge { display: flex; align-items: center; gap: 8px; font-size: 1.1rem; }
                .score-badge .badge { font-size: 1.4rem; }

                .risk-badge { margin-left: 8px; padding: 2px 8px; border-radius: 12px; font-size: 0.85rem; }
                .risk-badge.safe { background: #065f46; color: #34d399; }
                .risk-badge.caution { background: #713f12; color: #fcd34d; }
                .risk-badge.danger { background: #7f1d1d; color: #fca5a5; }

                .grok-text { font-style: italic; margin: 8px 0 0; line-height: 1.5; color: #e0f7ef; }

                .detail-row.highlight { margin: 12px 0; font-size: 1.1rem; }

                /* Grok Box - Fixed area with internal scroll */
                .grok-insight {
                    background: linear-gradient(135deg, rgba(22, 101, 52, 0.8), rgba(6, 78, 59, 0.9));
                    border: 1px solid #10b981;
                    border-radius: 14px;
                    padding: 16px;
                    min-height: 140px;
                    max-height: 200px;
                    overflow-y: auto;
                    display: flex;
                    flex-direction: column;
                    transition: all 0.4s ease;
                    cursor: pointer;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
                }
                .grok-insight:hover {
                    max-height: 300px;
                    border-color: #34d399;
                    box-shadow: 0 8px 25px rgba(52, 211, 153, 0.5);
                    transform: translateY(-2px);
                }
                .grok-probability {
                    font-weight: bold;
                    color: #9ff2d6;
                    font-size: 1.1rem;
                    margin-bottom: 8px;
                    text-shadow: 0 1px 3px rgba(0,0,0,0.5);
                }
                .grok-oneliner {
                    font-size: 0.94rem;
                    line-height: 1.5;
                    color: #e0f7ef;
                    flex-grow: 1;
                    overflow: hidden;
                }
                .grok-insight:hover .grok-oneliner {
                    overflow-y: auto;
                }
                /* Scrollbar styling for webkit */
                .grok-insight:hover::-webkit-scrollbar {
                    width: 6px;
                }
                .grok-insight:hover::-webkit-scrollbar-track {
                    background: rgba(0,0,0,0.2);
                    border-radius: 10px;
                }
                .grok-insight:hover::-webkit-scrollbar-thumb {
                    background: #34d399;
                    border-radius: 10px;
                }
                .roc-warning { border-left-color: #fb923c; }
                .roc { color: #fb923c; font-weight: bold; }
                .kv-grid {
                    display: grid;
                    grid-template-columns: max-content 1fr;
                    gap: 10px 20px;
                    margin: 15px 0;
                    font-size: 0.95rem;
                }
                .kv-grid strong { color: #60a5fa; }
                .highlight { color: #34d399 !important; font-weight: bold; font-size: 1.1rem; }
                .badge {
                    padding: 4px 12px;
                    border-radius: 20px;
                    font-size: 0.8rem;
                    font-weight: 800;
                    margin-left: 8px;
                }
                .badge-safe { background: #064e3b; color: #34d399; border: 1px solid #059669; }
                .badge-aggressive { background: #7c2d12; color: #fb923c; border: 1px solid #fb923c; }

                .exit-box {
                    background: linear-gradient(135deg, #065f46, #064e3b);
                    color: #a7f3d0;
                    padding: 14px;
                    border-radius: 12px;
                    margin: 18px 0 12px 0;
                    font-weight: 600;
                    font-size: 1.02rem;
                    border: 1px solid #10b981;
                    text-align: center;
                    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
                }
                .tip-box {
                    background: rgba(96, 165, 250, 0.1);
                    color: #60a5fa;
                    padding: 12px;
                    border-radius: 10px;
                    font-style: italic;
                    border-left: 4px solid #60a5fa;
                }
                .empty {
                    grid-column: 1 / -1;
                    text-align: center;
                    color: #94a3b8;
                    padding: 60px;
                    font-style: italic;
                    font-size: 1.2rem;
                }
                .progress-container {
                    margin-top: 4px;
                    height: 18px;
                    background: rgba(0, 0, 0, 0.4);
                    border-radius: 9px;
                    overflow: hidden;
                    box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.6);
                    opacity: 0;
                    transition: opacity 0.4s ease;
                }
                .progress-container.visible {
                    opacity: 1;
                }
                .progress-bar {
                    width: 100%;
                    height: 20px;
                    background: #333;
                    border-radius: 10px;
                    overflow: hidden;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.3);
                }
                .progress-fill {
                    height: 100%;
                    width: 0%;
                    background: linear-gradient(90deg, #10b981, #34d399, #6ee7b7);
                    border-radius: 19px;
                    position: relative;
                    transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
                    box-shadow: 0 0 15px rgba(52, 211, 153, 0.6);
                    }
                .progress-text {
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    font-weight: bold;
                    font-size: 16px;
                    color: white;
                    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.7);
                }
                .progress-glow {
                    position: absolute;
                    top: 0;
                    left: -100%;
                    width: 50%;
                    height: 100%;
                    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
                    animation: shimmer 3s infinite;
                }
                .progress-container span {
                    display: block;
                    text-align: center;
                    margin-top: 6px;
                    font-weight: bold;
                    color: #34d399;
                }
                                /* === DIVIDEND TILES - PREMIUM STYLING === */
                .tile_dividend {
                    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                    padding: 28px;
                    border-radius: 18px;
                    border: 1px solid #334155;
                    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
                    transition: all 0.4s ease;
                    height: 420px;                  /* Consistent height */
                    display: flex;
                    flex-direction: column;
                    justify-content: space-between;
                    position: relative;
                    overflow: hidden;
                }

                .tile_dividend:hover {
                    transform: translateY(-10px);
                    box-shadow: 0 20px 45px rgba(0, 0, 0, 0.7);
                    border-color: #60a5fa;
                }

                /* High ROC Warning Banner */
                .roc_banner {
                    background: linear-gradient(135deg, #7c2d12, #9a3412);
                    color: #fed7aa;
                    padding: 10px 16px;
                    border-radius: 12px 12px 0 0;
                    font-weight: bold;
                    font-size: 1.05rem;
                    text-align: center;
                    margin: -28px -28px 20px -28px;
                    border-bottom: 2px solid #fb923c;
                    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
                }

                /* Symbol Header */
                .tile_dividend h3 {
                    margin: 0 0 16px 0;
                    color: #60a5fa;
                    font-size: 1.6rem;
                    text-align: center;
                }

                /* Info Rows */
                .div_info {
                    margin: 12px 0;
                    font-size: 1.02rem;
                    line-height: 1.6;
                }
                .div_info strong {
                    color: #a7f3d0;
                }

                /* Historical Income Highlight */
                .div_income {
                    background: rgba(52, 211, 153, 0.15);
                    padding: 14px;
                    border-radius: 12px;
                    border-left: 4px solid #34d399;
                    margin: 16px 0;
                    text-align: center;
                }
                .div_income strong {
                    color: #34d399;
                    font-size: 1.15rem;
                }

                /* Yield on Cost */
                .div_yoc {
                    text-align: center;
                    font-size: 1.3rem;
                    font-weight: bold;
                    color: #34d399;
                    margin-top: auto;
                    padding-top: 16px;
                    border-top: 1px dashed #334155;
                }

                /* Portfolio Totals Tile - Make it STAND OUT */
                .tile_portfolio_totals {
                    background: linear-gradient(135deg, #064e3b, #065f46) !important;
                    border: 2px solid #34d399 !important;
                    grid-column: 1 / -1;
                    height: auto;
                    min-height: 300px;
                    box-shadow: 0 15px 40px rgba(52, 211, 153, 0.3);
                }
                .tile_portfolio_totals:hover {
                    box-shadow: 0 25px 60px rgba(52, 211, 153, 0.5);
                    transform: translateY(-12px);
                }
                .tile_portfolio_totals h3 {
                    color: #34d399;
                    font-size: 1.8rem;
                    text-align: center;
                    margin-bottom: 20px;
                }

                #market-pulse::-webkit-scrollbar {
                    width: 8px;
                }
                #market-pulse::-webkit-scrollbar-track {
                    background: rgba(0,0,0,0.3);
                    border-radius: 10px;
                }
                #market-pulse::-webkit-scrollbar-thumb {
                    background: #60a5fa;
                    border-radius: 10px;
                }
                #market-pulse::-webkit-scrollbar-thumb:hover {
                    background: #3b82f6;
                }

                .tile_mirror {
                    background: linear-gradient(135deg, #0f172a, #1e293b);
                    border: 2px solid #334155;
                    border-radius: 16px;
                    padding: 20px;
                    box-shadow: 0 6px 20px rgba(0,0,0,0.5);
                    transition: all 0.3s ease;
                    height: 220px;                    /* Fixed shorter height */
                    display: flex;
                    flex-direction: column;
                    justify-content: space-between;
                    overflow: hidden;
                }

                .tile_mirror:hover {
                    transform: translateY(-6px);
                    box-shadow: 0 12px 30px rgba(0,0,0,0.7);
                    border-color: #60a5fa;
                }

                /* Collapsible Sections */
                .collapsible-section .collapsible-header {
                    cursor: pointer;
                    padding: 16px;
                    background: rgba(15, 23, 42, 0.6);
                    border-radius: 12px;
                    margin-bottom: 8px;
                    user-select: none;
                }
                .collapsible-section.collapsed .collapsible-content {
                    display: none;
                }
                .collapsible-section .collapsible-content {
                    padding: 20px;
                    background: rgba(30, 41, 59, 0.4);
                    border-radius: 12px;
                }

                /* 3-Column Grid for Top 10 */
                .grid-3 {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                    gap: 16px;
                    margin-top: 16px;
                }
                .top10-tile {
                    padding: 16px;
                    background: rgba(15, 23, 42, 0.8);
                    border-radius: 12px;
                    border-left: 5px solid #34d399;
                    font-size: 0.95rem;
                    line-height: 1.6;
                }
                .top10-tile.pre-grok {
                    border-left-color: #f59e0b;
                }

                /* Enhanced Sorting Dropdown - Dark Mode Friendly */
                .csp-sort {
                    padding: 14px 48px 14px 18px;
                    background: linear-gradient(135deg, #065f46, #064e3b) !important;
                    color: #d1fae5 !important;
                    border: 2px solid #10b981 !important;
                    border-radius: 16px !important;
                    font-size: 1.1rem;
                    font-weight: 600;
                    cursor: pointer;
                    outline: none;
                    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4);
                    appearance: none;
                    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23a7f3d0' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
                    background-repeat: no-repeat;
                    background-position: right 18px center;
                    background-size: 16px;
                }

                /* Force dark dropdown menu on WebKit browsers (Chrome, Edge, Safari) */
                .csp-sort::-webkit-calendar-picker-indicator { display: none; }
                .csp-sort option {
                    background: #0f172a;        /* Dark background */
                    color: #d1fae5;              /* Light green text */
                    padding: 10px;
                }

                /* Firefox - limited control, but this helps */
                .csp-sort:-moz-focusring {
                    color: transparent;
                    text-shadow: 0 0 0 #d1fae5;
                }

                /* Hover/Focus states */
                .csp-sort:hover {
                    background: linear-gradient(135deg, #10b981, #059669);
                    border-color: #34d399;
                    box-shadow: 0 10px 30px rgba(52, 211, 153, 0.3);
                }

                .csp-sort:focus {
                    border-color: #34d399;
                    box-shadow: 0 0 0 4px rgba(52, 211, 153, 0.3);
                    background: linear-gradient(135deg, #10b981, #059669);
                }
                /* Optional: Style the label too */
                label[for="csp-sort"] {
                    color: #d1fae5;
                    font-weight: 700;
                    font-size: 1.15rem;
                    text-shadow: 0 1px 3px rgba(0,0,0,0.5);
                }
                @keyframes shimmer {
                    0% { left: -100%; }
                    100% { left: 100%; }
                }
                @media (max-width: 768px) {
                    .grid { grid-template-columns: 1fr; }
                    .action-buttons { flex-direction: column; align-items: center; }
                    .action-btn { width: 80%; }
                }
                div[style*="max-height"][style*="overflow-y"]::-webkit-scrollbar {
                    width: 8px;
                }
                div[style*="max-height"][style*="overflow-y"]::-webkit-scrollbar-track {
                    background: rgba(0,0,0,0.3);
                    border-radius: 10px;
                }
                div[style*="max-height"][style*="overflow-y"]::-webkit-scrollbar-thumb {
                    background: #34d399;
                    border-radius: 10px;
                }
                div[style*="max-height"][style*="overflow-y"]::-webkit-scrollbar-thumb:hover {
                    background: #10b981;
                }
            </style>
        </head>
        <body>
            <div style="position:fixed; top:12px; left:12px; z-index:9999;">
            <button onclick="refreshDashboard()" 
                    style="padding:12px 20px; background:#dc2626; color:white; border:none; border-radius:12px; cursor:pointer; font-weight:bold; box-shadow:0 4px 15px rgba(0,0,0,0.4);">
                🔄 Refresh Dashboard
            </button>
            </div>
            <div class="container" style="margin-top:60px;">  <!-- Push content down a bit -->
                <h1>🚀 Kyle's Interactive Trading Dashboard</h1>
                <!-- Grok Market Pulse Tile -->
                <div class="tile" style="grid-column: 1 / -1; background:linear-gradient(135deg,#1e293b,#0f172a); border:2px solid #60a5fa; margin-bottom:40px; max-height:500px; overflow:hidden; display:flex; flex-direction:column;">
                    <div style="display:flex; justify-content:space-between; align-items:center; padding:20px 24px 0 24px;">
                        <h3 style="color:#60a5fa; margin:0;">📡 Grok Market Pulse — {{ now }}</h3>
                        <button onclick="loadMarketPulse()" style="padding:8px 16px; background:#3b82f6; color:white; border:none; border-radius:8px; cursor:pointer; font-weight:bold;">
                            Refresh Pulse
                        </button>
                    </div>
                    <div id="market-pulse" style="flex:1; padding:20px; overflow-y:auto; color:#e0f7ef; line-height:1.8; font-size:1.05rem; margin-top:8px;">
                        Loading market pulse...
                    </div>
                </div>

                <script>
                async function loadMarketPulse() {
                    const div = document.getElementById('market-pulse');
                    div.innerHTML = "🔍 Updating market pulse...";
                    try {
                        const resp = await fetch('http://127.0.0.1:5000/grok/market_pulse');
                        if (!resp.ok) {
                            throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
                        }
                        const data = await resp.json();
                        if (data.error) {
                            div.innerHTML = `<span style="color:#fb923c;">⚠️ ${data.error}</span>`;
                        } else {
                            div.innerHTML = data.pulse.replace(/\n/g, '<br>');
                        }
                    } catch (e) {
                        div.innerHTML = `<span style="color:#fb923c;">⚠️ Market Pulse unavailable</span><br><span style="font-size:0.9rem; color:#94a3b8;">Make sure dashboard server is running: <code style="background:#1e293b; padding:2px 6px; border-radius:4px;">python dashboard_server.py</code></span><br><span style="font-size:0.85rem; color:#64748b;">Error: ${e.message}</span>`;
                    }
                }
                loadMarketPulse();
                </script>

            <!-- Script Running Buttons (Top-Right Corner) -->
            <div class="script-buttons-container">
                <button class="script-button" style="background: linear-gradient(135deg, #6366f1, #4f46e5);"
                        onclick="location.reload()">
                    🔄 Refresh Data
                </button>
                <button class="script-button" id="btn-scanner" onclick="runScript('scanner')">
                    🔍 Run CSP Scanner
                    <div class="progress-container" id="progress-scanner">
                        <div class="progress-fill" id="fill-scanner">
                            <div class="progress-glow"></div>
                            <div class="progress-text" id="text-scanner">0%</div>
                        </div>
                    </div>
                </button>
                <button class="script-button" id="btn-leaps" onclick="runScript('leaps')">
                    🕰️ Run LEAPS Scanner
                    <div class="progress-container" id="progress-leaps">
                        <div class="progress-fill" id="fill-leaps">
                            <div class="progress-glow"></div>
                            <div class="progress-text" id="text-leaps">0%</div>
                        </div>
                    </div>
                </button>
                <button class="script-button" onclick="runScript('open_trades_refresh')">
                    🔄 Refresh Open CSPs
                    <div class="progress-container" id="progress-open_trades_refresh">
                        <div class="progress-fill" id="fill-open_trades_refresh">
                            <div class="progress-glow"></div>
                            <div class="progress-text" id="text-open_trades_refresh">0%</div>
                        </div>
                    </div>
                </button>
                <button class="script-button" id="btn-covered" onclick="runScript('covered_calls')">
                    💸 Run Covered Calls
                    <div class="progress-container" id="progress-covered_calls">
                        <div class="progress-fill" id="fill-covered_calls">
                            <div class="progress-glow"></div>
                            <div class="progress-text" id="text-covered_calls">0%</div>
                        </div>
                    </div>
                </button>
                <button class="script-button" id="btn-dividends" onclick="runScript('dividends')">
                    📊 Run Dividend Report
                    <div class="progress-container" id="progress-dividends">
                        <div class="progress-fill" id="fill-dividends">
                            <div class="progress-glow"></div>
                            <div class="progress-text" id="text-dividends">0%</div>
                        </div>
                    </div>
                </button>
            </div>
                </div>
                <div id="status"></div>
                    <div class="tabs">
                        <button class="tab-btn active" data-tab="scanner" onclick="openTab('scanner')">CSP Opportunities</button>
                        <button class="tab-btn" data-tab="zero-dte" onclick="openTab('zero-dte')">🔥 0DTE Spreads</button>
                        <button class="tab-btn" data-tab="leaps" onclick="openTab('leaps')">LEAPS Opportunities</button>
                        <button class="tab-btn" data-tab="open" onclick="openTab('open')">Open CSPs</button>
                        <button class="tab-btn" data-tab="analytics" onclick="openTab('analytics')">📈 Analytics</button>
                        <button class="tab-btn" data-tab="risk-dashboard" onclick="openTab('risk-dashboard')">⚠️ Risk Dashboard</button>
                        <button class="tab-btn" data-tab="cc" onclick="openTab('cc')">Covered Calls</button>
                        <button class="tab-btn" data-tab="dividends" onclick="openTab('dividends')">Dividends</button>
                        <button class="tab-btn" data-tab="calendar" onclick="openTab('calendar')">Calendar</button>
                        <button class="tab-btn" data-tab="grok" onclick="openTab('grok')">Grok Insights</button>
                        <button class="tab-btn" onclick="window.open('/heatmap', '_blank')">🔥 Heatmap</button>
                    </div>

                <!-- CSP Scanner -->
                <div id="scanner" class="tab-content">
                    <h2 style="color:#60a5fa;">🎯 CSP Scanner — Top Opportunities</h2>

                    <!-- Collapsible: Top 10 Grok Ranked -->
                    <div class="collapsible-section" style="margin-bottom: 30px;">
                        <div class="collapsible-header" onclick="this.parentElement.classList.toggle('collapsed')">
                            <h3 style="color:#34d399; margin:0; display:inline;">🌟 Top 10 Global Ranked Suggestions (Grok Scored)</h3>
                            <span style="float:right; font-size:1.5rem;">▼</span>
                        </div>
                        <div class="collapsible-content">
                            {% if simple_scanner_opps and simple_scanner_opps|length > 0 %}
                                {% set all_suggestions = [] %}
                                {% for tile in simple_scanner_opps %}
                                    {% if tile.suggestions is defined and tile.suggestions|length > 0 %}
                                        {% set _ = all_suggestions.extend(tile.suggestions) %}
                                    {% endif %}
                                {% endfor %}
                                
                                {% if all_suggestions|length > 0 %}
                                    {% set sorted_grok = all_suggestions|sort(attribute='grok_trade_score', reverse=true) %}
                                    <div class="grid-3">
                                        {% for opp in sorted_grok[:10] %}
                                            {% if opp.strike is defined %}
                                                <div class="top10-tile">
                                                    <strong>#{{ loop.index }} • {{ opp.symbol }}</strong><br>
                                                    ${{ opp.strike|safe_format("%.0f") }}P • {{ opp.dte }} DTE<br>
                                                    <span style="color:#34d399;">Score: {{ opp.grok_trade_score|default(0) }}/100</span><br>
                                                    Premium: ${{ opp.premium|default(0)|safe_format("%.2f") }} • 
                                                    Ann: {{ opp.annualized_roi|default(0)|safe_format("%.1f") }}%<br>
                                                    Delta: {{ opp.delta|default(0)|safe_format("%.2f") }} • 
                                                    Dist: {{ opp.distance|default(0)|safe_format("%.1f") }}%<br>
                                                    <strong>Capital: ${{ opp.capital|default(0)|safe_format("%.0f") }}</strong>
                                                </div>
                                            {% endif %}
                                        {% endfor %}
                                    </div>
                                {% else %}
                                    <p style="color:#94a3b8; padding:20px 0;">No valid suggestions found in data</p>
                                {% endif %}
                            {% else %}
                                <p style="color:#94a3b8; padding:20px 0;">No suggestions available yet — run the scanner for fresh data</p>
                            {% endif %}
                        </div>
                    </div>

                    <!-- Main Grid -->
                    {% if simple_scanner_opps and simple_scanner_opps|length > 0 %}
                        <div class="grid">
                            {% for tile in simple_scanner_opps %}
                                {% if tile.suggestions is defined and tile.suggestions|length > 0 %}
                                    <div class="tile_scanner" style="position: relative;">
                                        <!-- Bollinger Band Indicator -->
                                        {% if tile.bollinger_bands %}
                                            {% set bb = tile.bollinger_bands %}
                                            {% set position = bb.position %}
                                            {% if position == 'below_lower' %}
                                                {% set bb_color = '#34d399' %}
                                                {% set bb_text = 'OVERSOLD' %}
                                            {% elif position == 'above_upper' %}
                                                {% set bb_color = '#f87171' %}
                                                {% set bb_text = 'OVERBOUGHT' %}
                                            {% else %}
                                                {% set bb_color = '#94a3b8' %}
                                                {% set bb_text = 'NEUTRAL' %}
                                            {% endif %}
                                            <div style="position: absolute; top: 16px; right: 16px; background: {{ bb_color }}; color: #0f172a; padding: 6px 12px; border-radius: 6px; font-weight: bold; font-size: 0.75rem; box-shadow: 0 2px 8px rgba(0,0,0,0.3);">
                                                {{ bb_text }}<br>
                                                <span style="font-size: 0.65rem;">%B: {{ bb.percent_b|safe_format("%.2f") }}</span>
                                            </div>
                                        {% endif %}

                                        <h3>
                                            {{ tile.symbol|default('Unknown') }}
                                            <span style="margin-left:12px; font-size:1.3rem;">{{ tile.best_badge|default('⚠️') }}</span>
                                            <span style="color:#94a3b8; font-size:1rem;">Best Score: {{ tile.best_score|default(0) }}/100</span>
                                        </h3>
                                        {% if tile.company_name %}
                                        <div style="color:#94a3b8; font-size:0.9rem; margin-top:-8px; margin-bottom:8px; font-style:italic;">
                                            {{ tile.company_name }}
                                        </div>
                                        {% endif %}

                                        <div style="color:#94a3b8; font-size:1.1rem; margin-bottom:16px;">
                                            Underlying: <strong style="color:#e2e8f0;">${{ tile.suggestions[0].current_price|safe_format("%.2f") }}</strong>
                                            <span style="margin-left:12px; color:#cbd5e1;">
                                                RSI: {{ tile.suggestions[0].rsi|safe_format("%.1f") }}
                                            </span>
                                        </div>

                                        <!-- Rebound Signals -->
                                        {% set rebound_signals = tile.suggestions[0].get('rebound_signals', {}) if tile.suggestions and tile.suggestions|length > 0 else {} %}
                                        {% if rebound_signals and rebound_signals.get('total_score') %}
                                            {% set score = rebound_signals.get('total_score', 0) %}
                                            {% set max_score = rebound_signals.get('max_score', 15) %}
                                            {% set score_pct = (score / max_score * 100)|int %}

                                            {% if score >= 10 %}
                                                {% set color = '#34d399' %}
                                                {% set bg = 'rgba(16, 185, 129, 0.2)' %}
                                                {% set icon = '🔥' %}
                                            {% elif score >= 7 %}
                                                {% set color = '#fbbf24' %}
                                                {% set bg = 'rgba(251, 191, 36, 0.2)' %}
                                                {% set icon = '⚡' %}
                                            {% else %}
                                                {% set color = '#94a3b8' %}
                                                {% set bg = 'rgba(148, 163, 184, 0.1)' %}
                                                {% set icon = '📊' %}
                                            {% endif %}

                                            <div style="margin: 12px 0; padding: 12px; background: {{ bg }}; border-radius: 10px; border-left: 4px solid {{ color }};">
                                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                                    <strong style="color: {{ color }}; font-size: 1.05rem;">{{ icon }} Rebound Signals</strong>
                                                    <span style="color: {{ color }}; font-weight: bold; font-size: 1.1rem;">{{ score }}/{{ max_score }}</span>
                                                </div>

                                                <div style="font-size: 0.9rem; color: #cbd5e1; line-height: 1.6;">
                                                    {% if rebound_signals.get('drawdown_pct') %}
                                                        <div>📉 Drawdown: <strong>{{ rebound_signals.drawdown_pct }}%</strong> from 20d high</div>
                                                    {% endif %}
                                                    {% if rebound_signals.get('consecutive_red_days') %}
                                                        <div>🔻 Consecutive red days: <strong>{{ rebound_signals.consecutive_red_days }}</strong></div>
                                                    {% endif %}
                                                    {% if rebound_signals.get('bb_position_pct') %}
                                                        <div>📊 BB Position: <strong>{{ rebound_signals.bb_position_pct }}%</strong> (lower=oversold)</div>
                                                    {% endif %}
                                                    {% if rebound_signals.get('volume_ratio') %}
                                                        <div>📈 Volume: <strong>{{ rebound_signals.volume_ratio }}x</strong> avg</div>
                                                    {% endif %}
                                                </div>

                                                {% if rebound_signals.get('verdict') %}
                                                    <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.1); font-style: italic; color: {{ color }}; font-size: 0.9rem;">
                                                        {{ rebound_signals.verdict }}
                                                    </div>
                                                {% endif %}
                                            </div>
                                        {% endif %}

                                        <!-- Support/Resistance: Once per symbol -->
                                        {% set sr = tile.suggestions[0].get('support_resistance', {}) if tile.suggestions and tile.suggestions|length > 0 else {} %}
                                        {% if sr and (sr.get('1') or sr.get('3') or sr.get('6') or sr.get('12')) %}
                                            <div class="support-resistance" style="margin: 12px 0; padding: 12px; background: rgba(16, 78, 59, 0.6); border-radius: 10px; font-size: 0.95rem;">
                                                <strong>📊 Support / Resistance Levels</strong>

                                                {% set three_m = sr.get('3', {}) %}
                                                {% set three_m_support_val = three_m.get('support')|safe_float %}
                                                {% set best_strike_val = tile.suggestions[0].strike|safe_float %}
                                                {% if three_m_support_val and best_strike_val %}
                                                    {% if three_m_support_val < best_strike_val * 0.97 %}
                                                        <div style="margin: 8px 0; padding: 8px; background: rgba(52, 211, 153, 0.4); border-radius: 8px; font-weight: bold; color: #34d399;">
                                                            🟢 Best strike safely below 3m support (${{ best_strike_val|safe_format("%.0f") }} vs ${{ three_m_support_val|safe_format("%.2f") }})
                                                        </div>
                                                    {% elif three_m_support_val < best_strike_val * 1.03 %}
                                                        <div style="margin: 8px 0; padding: 8px; background: rgba(251, 191, 36, 0.4); border-radius: 8px; font-weight: bold; color: #fbbf24;">
                                                            🟡 Best strike near 3m support (${{ best_strike_val|safe_format("%.0f") }} vs ${{ three_m_support_val|safe_format("%.2f") }})
                                                        </div>
                                                    {% else %}
                                                        <div style="margin: 8px 0; padding: 8px; background: rgba(239, 68, 68, 0.4); border-radius: 8px; font-weight: bold; color: #f87171;">
                                                            🔴 Best strike at/above 3m support — higher risk
                                                        </div>
                                                    {% endif %}
                                                {% endif %}

                                                <ul style="margin: 10px 0 0 0; padding-left: 20px; line-height: 1.5; color: #e2e8f0;">
                                                    {% for months in ['1', '3', '6', '12'] %}
                                                        {% set levels = sr.get(months, {}) %}
                                                        {% set support_val = levels.get('support') %}
                                                        {% set resistance_val = levels.get('resistance') %}
                                                        {% if support_val or resistance_val %}
                                                            <li>
                                                                <strong>{{ months }}m:</strong>
                                                                {% if support_val %}
                                                                    Supp ${{ support_val|safe_format("%.2f") }}
                                                                {% else %}
                                                                    Supp N/A
                                                                {% endif %} /
                                                                {% if resistance_val %}
                                                                    Res ${{ resistance_val|safe_format("%.2f") }}
                                                                {% else %}
                                                                    Res N/A
                                                                {% endif %}
                                                            </li>
                                                        {% endif %}
                                                    {% endfor %}
                                                </ul>
                                            </div>
                                        {% else %}
                                            <em style="color:#94a3b8; margin: 12px 0; display: block;">
                                                Support/Resistance levels unavailable
                                            </em>
                                        {% endif %}

                                        <!-- Quality Signals -->
                                        {% set quality = tile.suggestions[0].get('quality_signals', {}) if tile.suggestions and tile.suggestions|length > 0 else {} %}
                                        {% if quality and quality.keys()|length > 1 %}
                                            {% set warnings = quality.get('warnings', []) %}
                                            {% set has_warnings = warnings|length > 0 %}

                                            <div style="margin: 12px 0; padding: 12px; background: {% if has_warnings %}rgba(239, 68, 68, 0.2){% else %}rgba(16, 185, 129, 0.2){% endif %}; border-radius: 10px; border-left: 4px solid {% if has_warnings %}#ef4444{% else %}#10b981{% endif %};">
                                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                                    <strong style="color: {% if has_warnings %}#ef4444{% else %}#10b981{% endif %}; font-size: 1.05rem;">
                                                        {% if has_warnings %}⚠️ Quality Warnings{% else %}✅ Quality Checks{% endif %}
                                                    </strong>
                                                </div>

                                                <div style="font-size: 0.9rem; color: #cbd5e1; line-height: 1.6;">
                                                    {% if warnings|length > 0 %}
                                                        {% for warning in warnings %}
                                                            <div style="color: #fbbf24; margin: 4px 0;">⚠️ {{ warning }}</div>
                                                        {% endfor %}
                                                    {% endif %}

                                                    {% if quality.get('days_to_earnings') %}
                                                        <div>📅 Earnings: {{ quality.days_to_earnings }} days away</div>
                                                    {% endif %}

                                                    {% if quality.get('iv_premium_pct') is not none %}
                                                        <div>💎 IV Premium: {{ quality.iv_premium_pct }}% (IV: {{ quality.iv }}% vs HV: {{ quality.hv_20 }}%)</div>
                                                    {% endif %}

                                                    {% if quality.get('distance_from_52w_low_pct') %}
                                                        <div>📍 52w Low Distance: {{ quality.distance_from_52w_low_pct }}% above</div>
                                                    {% endif %}

                                                    {% if quality.get('macd_status') %}
                                                        <div>📉 MACD: {{ quality.macd_status }}</div>
                                                    {% endif %}

                                                    {% if quality.get('relative_strength') is not none %}
                                                        <div>📊 vs SPY: {{ quality.relative_strength }}% (Stock: {{ quality.stock_5d_return }}%, SPY: {{ quality.spy_5d_return }}%)</div>
                                                    {% endif %}

                                                    {% if quality.get('ma_signal') %}
                                                        <div>📈 20d MA: {{ quality.ma_signal }} ({{ quality.ma_20_slope }}%)</div>
                                                    {% endif %}
                                                </div>
                                            </div>
                                        {% endif %}

                                        <!-- Individual Opportunities -->
                                        <div style="margin-top: 16px;">
                                            {% for opp in tile.suggestions %}
                                                {% if opp.strike is defined %}
                                                    <div class="csp-tile"
                                                        data-premium="{{ opp.premium|default(0) }}"
                                                        data-capital="{{ opp.capital|default(0) }}"
                                                        data-symbol="{{ tile.symbol }}"
                                                        data-strike="{{ opp.strike }}"
                                                        data-expiration="{{ opp.expiration_date }}"
                                                        data-current-price="{{ opp.current_price|default(0) }}"
                                                        data-delta="{{ opp.delta|default(0) }}"
                                                        data-iv="{{ opp.iv|default(0) }}"
                                                        data-oi="{{ opp.open_interest|default(0) }}"
                                                        data-volume="{{ opp.volume|default(0) }}"
                                                        style="background:rgba(15,23,42,0.9); padding:18px; border-radius:14px; margin-bottom:14px; border-left:5px solid #34d399; box-shadow:0 6px 16px rgba(0,0,0,0.4);">

                                                        <!-- Rank & Header -->
                                                        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
                                                            <div style="display:flex; align-items:center; gap:12px;">
                                                                <input type="checkbox" class="compare-checkbox"
                                                                    data-opp='{{ opp|tojson }}'
                                                                    style="width:18px; height:18px; cursor:pointer;">
                                                                <div>
                                                                    <strong style="font-size:1.3rem; color:#60a5fa;">#{{ opp.overall_rank|default('?') }}</strong>
                                                                    <span style="color:#94a3b8; margin-left:8px; font-size:0.95rem;">
                                                                        (Pre-Grok #{{ opp.global_rank|default('?') }})
                                                                    </span>
                                                                    <div style="font-size:0.75rem; color:#64748b; margin-top:4px;">
                                                                        <span class="live-update-status">📡 Live</span>
                                                                    </div>
                                                                </div>
                                                            </div>
                                                            <div style="text-align:right;">
                                                                <div style="font-size:2.4rem; filter:drop-shadow(0 0 8px currentColor);">
                                                                    {{ opp.score_badge|default('') }}
                                                                </div>
                                                                <div style="color:#94a3b8; font-size:0.9rem;">Score</div>
                                                                <div style="font-size:1.2rem; font-weight:bold; color:#e2e8f0;">
                                                                    {{ opp.grok_trade_score|default(0) }}/100
                                                                </div>
                                                            </div>
                                                        </div>

                                                        <!-- Symbol & Company Name -->
                                                        <div style="font-size:1.1rem; color:#94a3b8; margin-bottom:8px; font-style:italic;">
                                                            {{ tile.company_name|default(tile.symbol) }}
                                                        </div>

                                                        <!-- Strike Line -->
                                                        <div style="font-size:1.4rem; font-weight:bold; color:#e2e8f0; margin-bottom:12px;">
                                                            ${{ opp.strike|safe_format("%.0f") }} PUT • {{ opp.dte }} DTE
                                                        </div>

                                                        <!-- Key Metrics -->
                                                        <div style="line-height:1.7; color:#cbd5e1; margin-bottom:16px;">
                                                            Premium: <strong style="color:#34d399;" class="live-premium">${{ opp.premium|safe_format("%.2f") }}</strong>
                                                            <span class="live-premium-change" style="margin-left:4px; font-size:0.85rem; font-weight:bold;"></span> •
                                                            Ann ROI: <strong>{{ opp.annualized_roi|safe_format("%.1f") }}%</strong><br>
                                                            Delta: <span class="live-delta">{{ opp.delta|safe_format("%.2f") }}</span> •
                                                            Dist: <span class="live-distance">{{ opp.distance|safe_format("%.1f") }}%</span> OTM •
                                                            IV: <span class="live-iv">{{ opp.iv|safe_format("%.0f") }}</span>
                                                            <br>
                                                            <span style="font-size:0.85rem; color:#94a3b8;">
                                                                OI: <span class="live-oi">{{ opp.open_interest|default(0)|safe_format("%.0f") }}</span> •
                                                                Vol: <span class="live-volume">{{ opp.volume|default(0)|safe_format("%.0f") }}</span> •
                                                                Spread: <span class="live-spread">N/A</span>
                                                            </span>
                                                        </div>

                                                        <!-- Contracts Calculator — Per Opportunity -->
                                                        <div style="padding:16px; background:rgba(30,41,59,0.9); border-radius:12px; border:1px solid #334155;">
                                                            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
                                                                <label style="color:#e2e8f0; font-weight:600; font-size:1.1rem;">Contracts:</label>
                                                                <select class="contract-selector" style="padding:8px 12px; background:#1e293b; color:white; border-radius:8px; border:none; font-size:1rem;">
                                                                    {% for n in range(1, 11) %}
                                                                        <option value="{{ n }}" {% if loop.index == 1 %}selected{% endif %}>{{ n }}</option>
                                                                    {% endfor %}
                                                                </select>
                                                            </div>

                                                            <div style="line-height:1.8; color:#e2e8f0; font-size:1.05rem;">
                                                                {% set cap_val = opp.capital|default(0)|safe_float %}
                                                                {% set prem_val = opp.premium|default(0)|safe_float %}
                                                                <div><strong>Capital Required:</strong> <span class="dynamic-capital">${{ cap_val|safe_format("%.0f") }}</span></div>
                                                                <div><strong>Total Premium Received:</strong> <span class="dynamic-premium">${{ (prem_val * 100)|safe_format("%.2f") }}</span></div>
                                                                <div><strong>Est. Profit at 50% Target:</strong> <span class="dynamic-profit">${{ (prem_val * 100 * 0.5)|round(0)|safe_format("%.0f") }}</span></div>
                                                            </div>
                                                        </div>

                                                        <!-- Grok Recommendation -->
                                                        <div style="margin-top:16px; padding:16px; background:rgba(16,78,59,0.8); border-radius:12px; border-left:4px solid #34d399; word-wrap:break-word; white-space:normal; overflow:visible;">
                                                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                                                                <strong style="color:#34d399; font-size:1.1rem;">🤖 {{ opp.grok_recommendation|default('N/A') }}</strong>
                                                                <span style="background:rgba(52,211,153,0.2); padding:4px 12px; border-radius:6px; color:#6ee7b7; font-size:0.9rem; font-weight:600;">
                                                                    Profit Prob: {{ opp.grok_profit_prob|default('N/A') }}
                                                                </span>
                                                            </div>
                                                            <div style="color:#d1fae5; line-height:1.7; margin-bottom:12px; font-size:0.95rem;">
                                                                {{ opp.grok_reason|default('No analysis available') }}
                                                            </div>

                                                            <!-- Technical Details Grid -->
                                                            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; padding-top:12px; border-top:1px solid rgba(52,211,153,0.3); font-size:0.85rem;">
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>📊 Current Price:</strong><br>
                                                                    ${{ opp.current_price|safe_format("%.2f") }}
                                                                </div>
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>🎯 S/R Risk:</strong><br>
                                                                    {{ opp.sr_risk_flag|default('Unknown') }}
                                                                </div>
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>📈 RSI:</strong><br>
                                                                    {{ opp.rsi|safe_format("%.1f") }}
                                                                </div>
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>💹 IV:</strong><br>
                                                                    {{ opp.iv|safe_format("%.0f") }}%
                                                                </div>
                                                                {% if opp.get('open_interest') %}
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>💧 Open Interest:</strong><br>
                                                                    {{ opp.open_interest|safe_format("%d") }}
                                                                </div>
                                                                {% endif %}
                                                                {% if opp.get('volume') is not none %}
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>📊 Volume:</strong><br>
                                                                    {{ opp.volume|safe_format("%d") }}
                                                                </div>
                                                                {% endif %}
                                                                {% if opp.get('bid_ask_spread_pct') %}
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>💱 Bid-Ask Spread:</strong><br>
                                                                    {{ opp.bid_ask_spread_pct|safe_format("%.1f") }}%
                                                                </div>
                                                                {% endif %}
                                                                {% if opp.get('bid') and opp.get('ask') %}
                                                                <div style="color:#a7f3d0;">
                                                                    <strong>📋 Bid/Ask:</strong><br>
                                                                    ${{ opp.bid|safe_format("%.2f") }} / ${{ opp.ask|safe_format("%.2f") }}
                                                                </div>
                                                                {% endif %}
                                                            </div>

                                                            <!-- Support/Resistance Levels -->
                                                            {% if opp.support_resistance and '3' in opp.support_resistance %}
                                                            {% set sr_3m = opp.support_resistance['3'] %}
                                                            <div style="margin-top:12px; padding-top:12px; border-top:1px solid rgba(52,211,153,0.3);">
                                                                <div style="color:#6ee7b7; font-weight:600; margin-bottom:6px; font-size:0.85rem;">📍 Key Levels (3-Month):</div>
                                                                <div style="display:flex; gap:16px; font-size:0.85rem; color:#a7f3d0;">
                                                                    {% if sr_3m.support is defined %}
                                                                    <div>Support: <strong style="color:#34d399;">${{ sr_3m.support|safe_format("%.2f") }}</strong></div>
                                                                    {% endif %}
                                                                    {% if sr_3m.resistance is defined %}
                                                                    <div>Resistance: <strong style="color:#fb923c;">${{ sr_3m.resistance|safe_format("%.2f") }}</strong></div>
                                                                    {% endif %}
                                                                </div>
                                                            </div>
                                                            {% endif %}
                                                        </div>

                                                        <!-- Live Pricing Info -->
                                                        {% set prem_val = opp.premium|safe_float %}
                                                        {% set bid_val = opp.bid|safe_float if opp.bid else prem_val * 0.98 %}
                                                        {% set ask_val = opp.ask|safe_float if opp.ask else prem_val * 1.02 %}

                                                        <div style="margin-top:16px; padding:12px; background:rgba(30,41,59,0.8); border-radius:10px; border:1px solid #334155;">
                                                            <div style="display:flex; justify-content:space-between; color:#e2e8f0; font-size:0.95rem; margin-bottom:8px;">
                                                                <span>Bid: <strong style="color:#34d399;">${{ bid_val|safe_format("%.2f") }}</strong></span>
                                                                <span>Mark: <strong style="color:#60a5fa;">${{ prem_val|safe_format("%.2f") }}</strong></span>
                                                                <span>Ask: <strong style="color:#fb923c;">${{ ask_val|safe_format("%.2f") }}</strong></span>
                                                            </div>
                                                        </div>

                                                        <!-- Sell To Open Button -->
                                                        <button class="action-btn sell-to-open-btn"
                                                            style="margin-top:12px; width:100%; padding:14px; background:linear-gradient(135deg, #10b981, #059669); color:white; border:none; border-radius:12px; font-weight:bold; font-size:1.1rem; cursor:pointer; transition:all 0.3s; box-shadow:0 4px 12px rgba(16,185,129,0.4);"
                                                            data-bid="{{ bid_val }}"
                                                            data-mark="{{ prem_val }}"
                                                            data-ask="{{ ask_val }}"
                                                            onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 6px 16px rgba(16,185,129,0.6)';"
                                                            onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 4px 12px rgba(16,185,129,0.4)';"
                                                            onclick="sellToOpen('{{ tile.symbol }}', {{ opp.strike }}, '{{ opp.exp_date }}', 1, {{ bid_val }}, this)">
                                                            📈 Sell To Open @ ${{ bid_val|safe_format("%.2f") }}
                                                        </button>
                                                    </div>
                                                {% endif %}
                                            {% endfor %}
                                        </div>
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No opportunities found — run the simple scanner for fresh results!</div>
                    {% endif %}

                    <!-- Grok Compare Button -->
                    <div id="compare-controls" style="position: fixed; bottom: 20px; right: 20px; z-index: 1000; display: none;">
                        <button id="grok-compare-btn"
                            style="background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); color: white; padding: 14px 24px; border: none; border-radius: 12px; font-weight: bold; font-size: 1rem; cursor: pointer; box-shadow: 0 8px 24px rgba(99, 102, 241, 0.4); transition: all 0.3s;">
                            🤖 Compare Selected (<span id="selected-count">0</span>)
                        </button>
                    </div>

                    <!-- Grok Compare Modal -->
                    <div id="compare-modal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 2000; align-items: center; justify-content: center;">
                        <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); padding: 30px; border-radius: 16px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.5);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                                <h2 style="color: #e2e8f0; margin: 0;">🤖 Grok Comparison Analysis</h2>
                                <button id="close-modal" style="background: #475569; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 1.2rem;">✕</button>
                            </div>
                            <div id="comparison-result" style="color: #e2e8f0; line-height: 1.8; white-space: pre-wrap;"></div>
                        </div>
                    </div>

                </div>  <!-- ← Closes id="scanner" -->

                <script>
                // Contract selector calculator
                document.querySelectorAll('.contract-selector').forEach(selector => {
                    selector.addEventListener('change', function() {
                        const tile = this.closest('.csp-tile');
                        if (!tile) return;
                        const contracts = parseInt(this.value) || 1;

                        const premiumPer = parseFloat(tile.dataset.premium || 0);
                        const capitalPer = parseFloat(tile.dataset.capital || 0);

                        const totalPremium = premiumPer * contracts * 100;
                        const totalCapital = capitalPer * contracts;
                        const profit50 = totalPremium * 0.5;

                        tile.querySelector('.dynamic-premium').textContent = '$' + totalPremium.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                        tile.querySelector('.dynamic-capital').textContent = '$' + totalCapital.toLocaleString('en-US', {minimumFractionDigits: 0});
                        tile.querySelector('.dynamic-profit').textContent = '$' + profit50.toLocaleString('en-US', {minimumFractionDigits: 0});
                    });
                });

                // Live data polling for opportunities
                const LIVE_UPDATE_INTERVAL = 45000; // 45 seconds
                let liveUpdateIntervals = {};

                function startLiveUpdates() {
                    const opportunities = document.querySelectorAll('.csp-tile[data-symbol][data-strike][data-expiration]');
                    console.log(`Starting live updates for ${opportunities.length} opportunities`);

                    opportunities.forEach(tile => {
                        const symbol = tile.dataset.symbol;
                        const strike = tile.dataset.strike;
                        const expiration = tile.dataset.expiration;

                        if (!symbol || !strike || !expiration) return;

                        // Initial update
                        updateOpportunityData(tile);

                        // Set up periodic updates
                        const key = `${symbol}-${strike}-${expiration}`;
                        if (!liveUpdateIntervals[key]) {
                            liveUpdateIntervals[key] = setInterval(() => {
                                updateOpportunityData(tile);
                            }, LIVE_UPDATE_INTERVAL);
                        }
                    });
                }

                async function updateOpportunityData(tile) {
                    const symbol = tile.dataset.symbol;
                    const strike = tile.dataset.strike;
                    const expiration = tile.dataset.expiration;
                    const statusEl = tile.querySelector('.live-update-status');

                    try {
                        if (statusEl) statusEl.textContent = '⏳ Updating...';

                        const response = await fetch(`/api/opportunity/live?symbol=${symbol}&strike=${strike}&expiration=${expiration}`);
                        const data = await response.json();

                        if (!data.success) {
                            console.error(`Failed to fetch live data for ${symbol} $${strike}:`, data.error);
                            if (statusEl) statusEl.textContent = '⚠️ Error';
                            return;
                        }

                        // Store previous values for change detection
                        const prevPremium = parseFloat(tile.dataset.premium || 0);
                        const prevPrice = parseFloat(tile.dataset.currentPrice || 0);
                        const prevDelta = parseFloat(tile.dataset.delta || 0);

                        // Update stored data
                        tile.dataset.premium = data.premium;
                        tile.dataset.currentPrice = data.current_price;
                        tile.dataset.delta = data.delta;
                        tile.dataset.iv = data.iv;
                        tile.dataset.oi = data.open_interest;
                        tile.dataset.volume = data.volume;

                        // Update Premium with change indicator
                        const premiumEl = tile.querySelector('.live-premium');
                        const premiumChangeEl = tile.querySelector('.live-premium-change');
                        if (premiumEl) {
                            premiumEl.textContent = `$${data.premium.toFixed(2)}`;
                            if (premiumChangeEl && prevPremium > 0) {
                                const change = data.premium - prevPremium;
                                if (Math.abs(change) > 0.01) {
                                    const arrow = change > 0 ? '▲' : '▼';
                                    const color = change > 0 ? '#34d399' : '#fb923c';
                                    premiumChangeEl.textContent = `${arrow} $${Math.abs(change).toFixed(2)}`;
                                    premiumChangeEl.style.color = color;

                                    // Flash animation
                                    premiumEl.style.transition = 'all 0.3s ease';
                                    premiumEl.style.backgroundColor = `${color}33`;
                                    setTimeout(() => {
                                        premiumEl.style.backgroundColor = 'transparent';
                                    }, 1000);
                                }
                            }
                        }

                        // Update Greeks
                        const deltaEl = tile.querySelector('.live-delta');
                        if (deltaEl) {
                            deltaEl.textContent = data.delta.toFixed(2);
                            if (Math.abs(data.delta - prevDelta) > 0.01) {
                                deltaEl.style.transition = 'all 0.3s ease';
                                deltaEl.style.color = '#fbbf24';
                                setTimeout(() => { deltaEl.style.color = '#cbd5e1'; }, 1000);
                            }
                        }

                        const ivEl = tile.querySelector('.live-iv');
                        if (ivEl) ivEl.textContent = data.iv.toFixed(0);

                        // Update Distance
                        const distanceEl = tile.querySelector('.live-distance');
                        if (distanceEl) distanceEl.textContent = data.distance_pct.toFixed(1) + '%';

                        // Update OI, Volume, Spread
                        const oiEl = tile.querySelector('.live-oi');
                        if (oiEl) oiEl.textContent = data.open_interest.toLocaleString();

                        const volumeEl = tile.querySelector('.live-volume');
                        if (volumeEl) volumeEl.textContent = data.volume.toLocaleString();

                        const spreadEl = tile.querySelector('.live-spread');
                        if (spreadEl) {
                            spreadEl.textContent = data.spread_pct.toFixed(1) + '%';
                            spreadEl.style.color = data.spread_pct < 5 ? '#34d399' : data.spread_pct < 10 ? '#fbbf24' : '#fb923c';
                        }

                        // Update status
                        if (statusEl) {
                            const now = new Date();
                            statusEl.textContent = `📡 ${now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}`;
                            statusEl.style.color = '#34d399';
                        }

                    } catch (error) {
                        console.error(`Error updating ${symbol} $${strike}:`, error);
                        if (statusEl) {
                            statusEl.textContent = '⚠️ Failed';
                            statusEl.style.color = '#fb923c';
                        }
                    }
                }

                function stopLiveUpdates() {
                    Object.values(liveUpdateIntervals).forEach(interval => clearInterval(interval));
                    liveUpdateIntervals = {};
                    console.log('Stopped all live updates');
                }

                // Start live updates when on scanner tab
                document.addEventListener('DOMContentLoaded', () => {
                    const scannerTab = document.querySelector('[onclick*="scanner"]');
                    if (scannerTab) {
                        scannerTab.addEventListener('click', () => {
                            setTimeout(startLiveUpdates, 500);
                        });
                    }

                    // Auto-start if scanner tab is default
                    const scannerContent = document.getElementById('scanner');
                    if (scannerContent && scannerContent.classList.contains('active')) {
                        startLiveUpdates();
                    }
                });

                // Cleanup on page unload
                window.addEventListener('beforeunload', stopLiveUpdates);
                </script>

                <!-- 0DTE Opportunities -->
                <div id="zero-dte" class="tab-content">
                        <div class="section-header">
                            <h2>🔥 0DTE Iron Condors & Spreads</h2>
                            <p>High-probability same-day premium sells • Defined risk • Rapid theta decay</p>
                            <small>Last scan: {{ now }}</small>
                        </div>

                        {% if zero_dte_opps %}
                        <div class="grid" id="zero-dte-grid">
                            {% for opp in zero_dte_opps %}
                            <div class="trade-card zero-dte-card">
                                <div class="card-header">
                                    <h3>{{ opp.symbol }} Iron Condor</h3>
                                    <div class="score-badge">
                                        <span class="badge {{ 'fire' if opp.score >= 90 else 'rocket' if opp.score >= 80 else 'check' if opp.score >= 70 else 'zap' if opp.score >= 60 else 'warn' }}">
                                            {% if opp.score >= 90 %}🔥{% elif opp.score >= 80 %}🚀{% elif opp.score >= 70 %}✅{% elif opp.score >= 60 %}⚡{% else %}⚠️{% endif %}
                                        </span>
                                        Score: {{ opp.score }}/100
                                    </div>
                                </div>

                                <div class="card-body">
                                    <div class="detail-row">
                                        <strong>Underlying:</strong> ${{ opp.underlying_price|round(2) }}
                                    </div>

                                    <div class="detail-row spread-row">
                                        <strong>Put Spread:</strong> 
                                        <span class="short-leg">{{ opp.short_put }}</span> → 
                                        <span class="long-leg">{{ opp.long_put }}P</span>
                                    </div>

                                    <div class="detail-row spread-row">
                                        <strong>Call Spread:</strong> 
                                        <span class="short-leg">{{ opp.short_call }}</span> → 
                                        <span class="long-leg">{{ opp.long_call }}C</span>
                                    </div>

                                    <div class="detail-row highlight">
                                        <strong>Credit:</strong> 
                                        <span class="profit">${{ opp.total_credit|round(2) }}</span>
                                    </div>

                                    <div class="detail-row highlight">
                                        <strong>Max Risk:</strong> 
                                        <span class="loss">${{ opp.max_risk_per_contract|round(2) }}</span>
                                        <span class="risk-badge {{ 'safe' if opp.risk_pct_capital < 1 else 'caution' if opp.risk_pct_capital < 2 else 'danger' }}">
                                            ({{ opp.risk_pct_capital|round(1) }}% capital)
                                        </span>
                                    </div>

                                    <div class="detail-row">
                                        <strong>Profit Target (60%):</strong> 
                                        <span class="target">${{ opp.profit_target|round(2) }}</span>
                                    </div>

                                    <div class="detail-row">
                                        <strong>Approx Prob:</strong> 
                                        <span class="prob">{{ opp.prob_approx }}%</span>
                                    </div>
                                </div>

                                <!-- Grok Quick Recommendation -->
                                <div class="grok-recommendation" style="margin:16px 0; padding:14px; background:linear-gradient(135deg, rgba(16,185,129,0.2), rgba(52,211,153,0.1)); border-radius:12px; border-left:4px solid #10b981;">
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                                        <strong style="color:#34d399; font-size:1.1rem;">🎯 Quick Trade Suggestion</strong>
                                    </div>

                                    {% set grok_rec = opp.grok_recommendation|default({}) %}
                                    {% set rec_side = grok_rec.recommendation|default('NEUTRAL') %}
                                    {% set rec_confidence = grok_rec.confidence|default(3) %}
                                    {% set rec_reasoning = grok_rec.reasoning|default('Analysis pending...') %}

                                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; font-size:0.95rem;">
                                        <!-- SELL PUT side -->
                                        <div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:8px; position:relative;
                                            {% if rec_side == 'SELL_PUT' %}
                                            border:2px solid #34d399; box-shadow:0 0 15px rgba(52,211,153,0.4);
                                            {% endif %}">
                                            {% if rec_side == 'SELL_PUT' %}
                                            <div style="position:absolute; top:-10px; right:8px; background:linear-gradient(135deg, #10b981, #34d399); color:white; padding:3px 10px; border-radius:12px; font-size:0.7rem; font-weight:bold;">
                                                🤖 GROK PICK
                                            </div>
                                            {% endif %}
                                            <div style="color:#94a3b8; font-size:0.8rem; margin-bottom:4px;">SELL PUT</div>
                                            <div style="color:#34d399; font-weight:bold; font-size:1.1rem;">${{ opp.short_put }} Strike</div>
                                            <div style="color:#a7f3d0; font-size:0.85rem;">Credit: ${{ (opp.total_credit * 0.4)|round(2) }}</div>
                                        </div>
                                        <!-- SELL CALL side -->
                                        <div style="background:rgba(0,0,0,0.2); padding:10px; border-radius:8px; position:relative;
                                            {% if rec_side == 'SELL_CALL' %}
                                            border:2px solid #fb923c; box-shadow:0 0 15px rgba(251,146,60,0.4);
                                            {% endif %}">
                                            {% if rec_side == 'SELL_CALL' %}
                                            <div style="position:absolute; top:-10px; right:8px; background:linear-gradient(135deg, #f97316, #fb923c); color:white; padding:3px 10px; border-radius:12px; font-size:0.7rem; font-weight:bold;">
                                                🤖 GROK PICK
                                            </div>
                                            {% endif %}
                                            <div style="color:#94a3b8; font-size:0.8rem; margin-bottom:4px;">SELL CALL</div>
                                            <div style="color:#fb923c; font-weight:bold; font-size:1.1rem;">${{ opp.short_call }} Strike</div>
                                            <div style="color:#fed7aa; font-size:0.85rem;">Credit: ${{ (opp.total_credit * 0.6)|round(2) }}</div>
                                        </div>
                                    </div>

                                    <!-- Grok Recommendation Reasoning -->
                                    {% if rec_side != 'NEUTRAL' %}
                                    <div style="margin-top:14px; padding:12px; background:linear-gradient(135deg, rgba(139,92,246,0.15), rgba(168,85,247,0.1)); border-radius:10px; border-left:3px solid #a78bfa;">
                                        <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
                                            <span style="font-size:1rem;">🧠</span>
                                            <strong style="color:#c4b5fd; font-size:0.9rem;">Grok's Recommendation</strong>
                                            <span style="background:{% if rec_side == 'SELL_PUT' %}rgba(52,211,153,0.3){% else %}rgba(251,146,60,0.3){% endif %}; color:{% if rec_side == 'SELL_PUT' %}#34d399{% else %}#fb923c{% endif %}; padding:2px 8px; border-radius:4px; font-size:0.8rem; font-weight:bold;">
                                                {{ rec_side|replace('_', ' ') }}
                                            </span>
                                            <span style="color:#94a3b8; font-size:0.75rem; margin-left:auto;">
                                                Confidence: {% for i in range(rec_confidence) %}⭐{% endfor %}{% for i in range(5 - rec_confidence) %}☆{% endfor %}
                                            </span>
                                        </div>
                                        <p style="color:#e9d5ff; font-size:0.85rem; margin:0; line-height:1.4;">{{ rec_reasoning }}</p>
                                    </div>
                                    {% endif %}

                                    <div style="margin-top:12px; padding-top:12px; border-top:1px solid rgba(52,211,153,0.3);">
                                        <div style="display:flex; justify-content:space-between; align-items:center;">
                                            <div>
                                                <span style="color:#94a3b8; font-size:0.85rem;">Exit Target (60% profit):</span>
                                                <strong style="color:#34d399; margin-left:8px;">${{ (opp.total_credit * 0.4)|round(2) }}</strong>
                                            </div>
                                            <div style="background:rgba(52,211,153,0.2); padding:6px 12px; border-radius:6px;">
                                                <span style="color:#6ee7b7; font-size:0.9rem; font-weight:600;">{{ opp.prob_approx }}% Win Prob</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <div class="grok-section">
                                    <strong>🤖 Grok Analysis:</strong>
                                    <p class="grok-text">{{ opp.grok_analysis|default('Analysis unavailable') }}</p>
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                        {% else %}
                        <div class="empty-state">
                            <h3>No qualifying 0DTE setups found</h3>
                            <p>Market may be closed, low liquidity, or no iron condors meeting risk filters (safe delta, min credit, max capital risk).</p>
                            <p>Try again during regular hours (9:30 AM – 4:00 PM ET).</p>
                        </div>
                        {% endif %}
                    </div>
                    
                <!-- LEAPS Opportunities -->
                <div id="leaps" class="tab-content">
                    <h2 style="color:#a78bfa; margin-bottom:24px;">
                        🌿 LEAPS Opportunities — Grok's Top Long-Term Picks + Covered Calls
                    </h2>

                    {% if leaps_opps and leaps_opps|length > 0 %}
                        <div class="grid">
                            {% for opp in leaps_opps %}
                                <div class="tile_scanner" style="position:relative; overflow:hidden;">

                                    <!-- Rank + Grok Score Badge (Top Right) -->
                                    <div style="
                                        position:absolute; 
                                        top:12px; right:12px; 
                                        background: linear-gradient(135deg, #7c3aed, #a78bfa); 
                                        color:white; 
                                        padding:10px 16px; 
                                        border-radius:16px; 
                                        font-weight:bold; 
                                        font-size:1.15rem; 
                                        box-shadow:0 6px 16px rgba(124,62,237,0.4);
                                        z-index:10;
                                        text-align:center;
                                        min-width:100px;
                                    ">
                                        #{{ opp.rank|default('?') }}
                                        <br>
                                        {% if opp.grok_score|default(0) > 0 %}
                                            <span style="font-size:0.95rem;">
                                                {{ opp.grok_score }}/100
                                                {% if opp.grok_score >= 90 %}🔥
                                                {% elif opp.grok_score >= 85 %}🚀
                                                {% elif opp.grok_score >= 75 %}✅
                                                {% elif opp.grok_score >= 65 %}⚡
                                                {% else %}📊
                                                {% endif %}
                                            </span>
                                        {% else %}
                                            <span style="font-size:0.9rem; opacity:0.9;">Quantitative</span>
                                        {% endif %}
                                    </div>

                                    <!-- Main Tile Content -->
                                    <h3 style="margin-top:0; color:#e2e8f0; padding-right:140px;">
                                        <strong style="font-size:1.4rem;">{{ opp.symbol|default('N/A') }}</strong>
                                        <span style="color:#94a3b8; font-size:1rem; margin-left:10px;">
                                            Deep ITM LEAPS Call
                                        </span>
                                    </h3>

                                    <div style="margin:20px 0;">
                                        <div style="
                                            background: rgba(88,28,135,0.15); 
                                            padding:20px; 
                                            border-radius:16px; 
                                            border-left:6px solid #a78bfa;
                                            backdrop-filter: blur(6px);
                                        ">
                                            <!-- Contract Header -->
                                            <div style="font-size:1.2rem; margin-bottom:16px; color:#c4b5fd;">
                                                {% set strike_val = opp.strike|default(0)|float %}
                                                <strong>${{ "{:.0f}".format(strike_val) }} CALL</strong>
                                                <span style="color:#cbd5e1; font-size:1rem;">
                                                    • Expires {{ opp.expiration_date|default('N/A') }} ({{ opp.dte|default(0) }} DTE)
                                                </span>
                                            </div>

                                            <!-- Key Metrics Grid -->
                                            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px 20px; font-size:1rem; color:#e2e8f0;">
                                                {% set underlying = opp.underlying_price|default(0)|float %}
                                                {% set premium = opp.premium|default(0)|float %}
                                                {% set delta = opp.delta|default(0)|float %}
                                                {% set iv = opp.iv|default(0)|float %}
                                                {% set itm_pct = opp.distance_itm_pct|default(0)|float %}
                                                {% set capital = (premium * 100)|round(0) %}
                                                {% set breakeven = strike_val + premium %}
                                                {% set leverage = (underlying / premium)|round(1) if premium > 0 else 0 %}

                                                <div>Underlying Price:</div>     <div><strong>${{ "{:.2f}".format(underlying) }}</strong></div>
                                                <div>LEAPS Premium:</div>        <div><strong>${{ "{:.2f}".format(premium) }}</strong></div>
                                                <div>Break-Even:</div>           <div><strong>${{ "{:.2f}".format(breakeven) }}</strong></div>
                                                <div>Leverage Ratio:</div>       <div><strong>{{ leverage }}x</strong></div>
                                                <div>Delta:</div>                <div><strong>{{ "{:.3f}".format(delta) }}</strong>
                                                    {% if delta >= 0.85 %} <span style="color:#10b981;">🟢 Deep ITM</span>
                                                    {% elif delta >= 0.7 %} <span style="color:#facc15;">🟡 ITM</span>
                                                    {% else %} <span style="color:#fb923c;">🟠 Near ATM</span>{% endif %}
                                                </div>
                                                <div>IV:</div>                   <div><strong>{{ "{:.0f}".format(iv) }}%</strong></div>
                                                <div>ITM Depth:</div>            <div><strong>{{ "{:.1f}".format(itm_pct) }}%</strong></div>
                                                <div>Capital Required:</div>     <div><strong>${{ capital|int }}</strong></div>
                                            </div>

                                            <!-- Grok Rationale + Covered Call Strategy -->
                                            <div style="margin-top:24px; padding:18px; background:rgba(88,28,135,0.4); border-radius:14px; border:1px solid #7c3aed;">
                                                <strong style="color:#c4b5fd; font-size:1.1rem;">🤖 Grok's Rationale:</strong>
                                                <div style="margin-top:10px; line-height:1.6; color:#e2e8f0; font-size:0.98rem;">
                                                    {{ opp.reason|default('Strong long-term growth profile with deep ITM structure and favorable volatility.')|replace('\n', '<br>')|safe }}
                                                </div>

                                                {% if opp.cc_idea|default('')|length > 0 %}
                                                    <div style="margin-top:18px; padding:16px; background:rgba(34,197,94,0.25); border-left:5px solid #22c55e; border-radius:10px;">
                                                        <strong style="color:#86efac; font-size:1.1rem;">💡 Covered Call Strategy (PMCC):</strong>
                                                        <div style="margin-top:10px; line-height:1.6; color:#dcfce7; font-size:0.98rem;">
                                                            {{ opp.cc_idea|replace('\n', '<br>')|safe }}
                                                        </div>
                                                    </div>
                                                {% endif %}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty" style="text-align:center; padding:80px; color:#64748b;">
                            <div style="font-size:5rem; margin-bottom:20px;">🌿</div>
                            <h3>No LEAPS opportunities yet</h3>
                            <p>Run the LEAPS scanner to populate Grok's latest long-term picks!</p>
                        </div>
                    {% endif %}
                </div>


                <!-- Open CSPs -->
                <div id="open" class="tab-content">
                    <h2 style="color:#34d399;">📉 Open Cash-Secured Puts</h2>

                    <!-- Live Refresh Controls -->
                    <div class="refresh-controls" style="display:flex; align-items:center; gap:20px; margin-bottom:20px; padding:15px; background:rgba(52,211,153,0.1); border-radius:12px; border:1px solid #34d399;">
                        <button id="csp-refresh-btn" onclick="refreshOpenCSPs(true)" style="padding:10px 20px; background:linear-gradient(135deg,#34d399,#10b981); color:#064e3b; border:none; border-radius:8px; cursor:pointer; font-weight:600; display:flex; align-items:center; gap:8px;">
                            <span id="csp-refresh-icon">🔄</span> Refresh Now
                        </button>
                        <div style="display:flex; align-items:center; gap:10px;">
                            <label style="color:#e2e8f0;">Auto-refresh:</label>
                            <select id="csp-auto-refresh" onchange="setAutoRefresh(this.value)" style="padding:8px 12px; background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px;">
                                <option value="0">Off</option>
                                <option value="300000">5 min</option>
                                <option value="600000" selected>10 min</option>
                                <option value="900000">15 min</option>
                            </select>
                        </div>
                        <div id="csp-last-updated" style="color:#94a3b8; font-size:0.9rem; margin-left:auto;">
                            Last updated: <span id="csp-update-time">{{ now if now else 'N/A' }}</span>
                        </div>
                        <div id="csp-refresh-status" style="color:#34d399; font-size:0.9rem; display:none;">
                            <span class="loading-dots">Refreshing</span>
                        </div>
                    </div>

                    <!-- Portfolio Summary Tile -->
                    {% if open_trades %}
                        {% set total_positions = open_trades|length %}
                        {% set total_contracts = open_trades|sum(attribute='_contracts', start=0) %}
                        {% set total_credit = open_trades|sum(attribute='_total_credit', start=0.0) %}
                        {% set total_pl_dollars = open_trades|sum(attribute='_pl_dollars', start=0.0) %}
                        {% set total_daily_theta = open_trades|sum(attribute='_daily_theta_decay_dollars', start=0.0) %}
                        {% set total_forward_theta = open_trades|sum(attribute='_forward_theta_daily', start=0.0) %}
                        {% set total_dte = open_trades|sum(attribute='_dte', start=0) %}
                        {% set total_progress = open_trades|sum(attribute='_progress_pct', start=0.0) %}
                        {% set total_projected_decay = open_trades|sum(attribute='_projected_decay', start=0.0) %}

                        {% set avg_dte = (total_dte / total_positions)|round(1) if total_positions > 0 else 0 %}
                        {% set avg_progress = (total_progress / total_positions)|round(1) if total_positions > 0 else 0 %}
                        {% set avg_daily_theta = (total_daily_theta / total_positions)|round(3) if total_positions > 0 else 0 %}

                        <div class="tile tile_summary" style="grid-column: 1 / -1; background:linear-gradient(135deg, #064e3b, #065f46); border:2px solid #34d399;">
                            <h3 style="color:#34d399; margin-top:0;">📊 Open CSP Portfolio Summary ({{ total_positions }} positions | {{ total_contracts }} contracts)</h3>
                            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap:20px;">
                                <div>
                                    <strong>Total Credit Received:</strong> <span style="color:#34d399; font-size:1.2rem;">${{ total_credit|safe_format("%.0f") }}</span><br>
                                    <strong>Current P/L:</strong>
                                    <span style="{% if total_pl_dollars >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-size:1.2rem;">
                                        {% if total_pl_dollars >= 0 %}+{% endif %}${{ total_pl_dollars|abs|safe_format("%.0f") }}
                                    </span>
                                </div>
                                <div>
                                    <strong>Avg Progress to Target:</strong> <span style="color:#fbbf24;">{{ avg_progress }}%</span><br>
                                    <strong>Avg DTE:</strong> <span style="color:#a7f3d0;">{{ avg_dte }} days</span>
                                </div>
                                <div>
                                    <strong>Total Realized Daily Theta:</strong> <span style="color:#34d399;">${{ total_daily_theta|safe_format("%.2f") }}</span><br>
                                    <strong>Avg per Position:</strong> <span style="color:#34d399;">${{ avg_daily_theta|safe_format("%.3f") }}</span>
                                </div>
                                <div>
                                    <strong>Total Expected Daily Decay:</strong> <span style="color:#34d399;">${{ total_forward_theta|safe_format("%.2f") }}</span><br>
                                    <strong>Projected Remaining Decay:</strong> <span style="color:#34d399;">~${{ total_projected_decay|safe_format("%.0f") }}</span>
                                </div>
                            </div>
                        </div>
                    {% endif %}

                    <!-- Capital Allocation Optimizer -->
                    <div class="tile tile_allocation" style="grid-column: 1 / -1; background:linear-gradient(135deg,#1e3a2f,#0f172a); border:2px solid #10b981;">
                        <h3 style="color:#34d399; margin-top:0;">💰 Capital Allocation Optimizer</h3>
                        
                        <div style="margin-bottom:20px;">
                            <label style="color:#e0f7ef; font-weight:bold;">Total Wheel Capital: $</label>
                            <input type="number" id="wheel-capital-input" value="{{ current_wheel_capital }}" 
                                   style="padding:8px; width:150px; background:#0f172a; color:white; border:1px solid #334155; border-radius:8px; margin-left:8px;">
                            <button onclick="updateAllocation()" style="margin-left:8px; padding:8px 16px; background:#34d399; color:#064e3b; border:none; border-radius:8px; cursor:pointer;">
                                Update
                            </button>
                        </div>

                        <div id="allocation-display" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap:20px;">
                            <div>
                                <strong>Total Wheel Capital:</strong> 
                                <span style="color:#34d399;">${{ allocation_data['total_capital']|safe_format("%.0f") }}</span><br>
                                <strong>At Risk:</strong> 
                                <span style="{% if allocation_data['pct_allocated'] > 80 %}color:#fb923c{% else %}color:#34d399{% endif %}">
                                    ${{ allocation_data['capital_at_risk']|safe_format("%.0f") }} ({{ allocation_data['pct_allocated'] }}%)
                                </span><br>
                                <strong>Remaining:</strong> ${{ allocation_data['remaining_capital']|safe_format("%.0f") }}
                            </div>

                            <div style="max-height:300px; overflow-y:auto; padding-right:8px;">
                            <strong>Sector Exposure</strong><br>
                                {% if allocation_data['sector_exposure'] %}
                                    {% for sector, amount in allocation_data['sector_exposure'].items()|list|sort(attribute=1, reverse=true) %}
                                        {% set amount_val = amount|safe_float %}
                                        {% set capital_risk_val = allocation_data['capital_at_risk']|safe_float %}
                                        <strong>{{ sector }}:</strong> ${{ amount_val|safe_format("%.0f") }}
                                        ({{ ((amount_val / capital_risk_val * 100) if capital_risk_val > 0 else 0)|safe_format("%.1f") }}%)<br>
                                    {% endfor %}
                                {% else %}
                                    <em>No open positions</em>
                                {% endif %}
                            </div>

                            <div>
                                <strong>Positions Open:</strong> {{ allocation_data['positions_open'] }} / {{ allocation_data['max_positions'] }}<br>
                                <strong>Room for:</strong> 
                                <span style="color:#34d399;">{{ allocation_data['room_for_trades'] }} new trades</span><br>
                                <strong>Suggested Max per Trade:</strong> ${{ allocation_data['max_per_trade']|safe_format("%.0f") }}
                            </div>
                        </div>
                    </div>

                    <script>
                    const baseData = {
                        capital_at_risk: {{ allocation_data['capital_at_risk'] }},
                        positions_open: {{ allocation_data['positions_open'] }},
                        max_positions: {{ allocation_data['max_positions'] }},
                        sector_exposure: {{ allocation_data['sector_exposure']|tojson }}
                    };

                    function updateAllocation() {
                        const input = document.getElementById('wheel-capital-input');
                        let total_capital = parseFloat(input.value) || 250000;

                        const at_risk = baseData.capital_at_risk;
                        const pct = total_capital > 0 ? (at_risk / total_capital * 100).toFixed(1) : 0;
                        const remaining = total_capital - at_risk;
                        const max_per_trade = total_capital * 0.20;  // 20%
                        const room_for = Math.floor(remaining / max_per_trade);

                        let sectorHTML = '';
                        if (Object.keys(baseData.sector_exposure).length > 0) {
                            for (const [sector, amount] of Object.entries(baseData.sector_exposure)) {
                                const sector_pct = at_risk > 0 ? (amount / at_risk * 100).toFixed(1) : 0;
                                sectorHTML += `${sector}: $${amount.toLocaleString()} (${sector_pct}%)<br>`;
                            }
                        } else {
                            sectorHTML = '<em>No open positions</em>';
                        }

                        document.getElementById('allocation-display').innerHTML = `
                            <div>
                                <strong>Total Wheel Capital:</strong> 
                                <span style="color:#34d399;">$${total_capital.toLocaleString()}</span><br>
                                <strong>At Risk:</strong> 
                                <span style="${pct > 80 ? 'color:#fb923c' : 'color:#34d399'}">
                                    $${at_risk.toLocaleString()} (${pct}%)
                                </span>
                            </div>
                            <div>
                                <strong>Positions:</strong> ${baseData.positions_open} / ${baseData.max_positions}<br>
                                <strong>Room for:</strong> 
                                <span style="color:#34d399;">${room_for} new trades</span>
                            </div>
                            <div>
                                <strong>Suggested Max per Trade:</strong> $${max_per_trade.toLocaleString()}<br>
                                <strong>Remaining Capital:</strong> $${remaining.toLocaleString()}
                            </div>
                            <div>
                                <strong>Sector Exposure</strong><br>
                                ${sectorHTML}
                            </div>
                        `;
                    }

                    // Initial load
                    updateAllocation();
                    </script>

                    <script>
                    // Grok Compare Functionality
                    let selectedOpportunities = [];

                    // Update selected count and button visibility
                    function updateCompareButton() {
                        const count = selectedOpportunities.length;
                        document.getElementById('selected-count').textContent = count;
                        document.getElementById('compare-controls').style.display = count === 2 ? 'block' : 'none';
                    }

                    // Handle checkbox changes
                    document.addEventListener('change', function(e) {
                        if (e.target.classList.contains('compare-checkbox')) {
                            const oppData = JSON.parse(e.target.dataset.opp);

                            if (e.target.checked) {
                                if (selectedOpportunities.length < 2) {
                                    selectedOpportunities.push(oppData);
                                } else {
                                    e.target.checked = false;
                                    alert('Please select only 2 opportunities to compare');
                                }
                            } else {
                                selectedOpportunities = selectedOpportunities.filter(o =>
                                    !(o.symbol === oppData.symbol && o.strike === oppData.strike)
                                );
                            }

                            updateCompareButton();
                        }
                    });

                    // Handle compare button click
                    document.getElementById('grok-compare-btn').addEventListener('click', async function() {
                        if (selectedOpportunities.length !== 2) {
                            alert('Please select exactly 2 opportunities to compare');
                            return;
                        }

                        const opp1 = selectedOpportunities[0];
                        const opp2 = selectedOpportunities[1];

                        // Show modal with loading
                        const modal = document.getElementById('compare-modal');
                        const result = document.getElementById('comparison-result');
                        result.innerHTML = '🤖 Analyzing with Grok AI...\n\nThis may take a few seconds...';
                        modal.style.display = 'flex';

                        try {
                            const response = await fetch('/api/grok_compare', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    opp1: {
                                        symbol: opp1.symbol,
                                        strike: opp1.strike,
                                        grok_score: opp1.grok_trade_score || 0,
                                        delta: opp1.delta || 0,
                                        distance: opp1.distance || 0,
                                        dte: opp1.dte || 0,
                                        rsi: opp1.rsi || 50,
                                        premium: opp1.premium || 0,
                                        capital: opp1.capital || 0,
                                        grok_reason: opp1.grok_reason || 'N/A'
                                    },
                                    opp2: {
                                        symbol: opp2.symbol,
                                        strike: opp2.strike,
                                        grok_score: opp2.grok_trade_score || 0,
                                        delta: opp2.delta || 0,
                                        distance: opp2.distance || 0,
                                        dte: opp2.dte || 0,
                                        rsi: opp2.rsi || 50,
                                        premium: opp2.premium || 0,
                                        capital: opp2.capital || 0,
                                        grok_reason: opp2.grok_reason || 'N/A'
                                    }
                                })
                            });

                            const data = await response.json();

                            if (data.success) {
                                result.innerHTML = data.comparison;
                            } else {
                                result.innerHTML = '❌ Error: ' + (data.error || 'Comparison failed');
                            }
                        } catch (error) {
                            result.innerHTML = '❌ Error connecting to Grok API: ' + error.message;
                        }
                    });

                    // Close modal
                    document.getElementById('close-modal').addEventListener('click', function() {
                        document.getElementById('compare-modal').style.display = 'none';
                    });

                    // Close modal on outside click
                    document.getElementById('compare-modal').addEventListener('click', function(e) {
                        if (e.target.id === 'compare-modal') {
                            this.style.display = 'none';
                        }
                    });
                    </script>

                    <!-- Sorting Dropdown -->
                    {% if open_trades %}
                        <div style="margin: 40px 0 30px 0; text-align: center;">
                            <label for="csp-sort" style="display: block; margin-bottom: 12px;">
                                🔄 Sort Open CSPs by:
                            </label>
                            <select id="csp-sort" class="csp-sort" onchange="sortCSPTiles()">
                                <option value="score-desc">Trade Score (Highest → Lowest)</option>
                                <option value="score-asc">Trade Score (Lowest → Highest)</option>
                                <option value="progress-desc">Progress to Target (High → Low)</option>
                                <option value="progress-asc">Progress to Target (Low → High)</option>
                                <option value="dte-asc">DTE (Soonest First)</option>
                                <option value="dte-desc">DTE (Longest First)</option>
                                <option value="daily-theta-desc">Realized Daily Theta (Highest)</option>
                                <option value="forward-theta-desc">Expected Daily Decay (Highest)</option>
                                <option value="pl-desc">Current P/L $ (Highest Profit)</option>
                                <option value="pl-asc">Current P/L $ (Biggest Loss First)</option>
                                <option value="default">Default Order</option>
                            </select>
                        </div>
                    {% endif %}
                    {% if open_trades %}
                        <div class="grid" id="open-csp-grid">
                            {% for trade in open_trades %}
                                <div class="tile tile_csp"
                                    data-score="{{ trade['grok_trade_score']|default(0)|int }}"
                                    data-progress="{{ trade['_progress_pct']|round(0) }}"
                                    data-dte="{{ trade['_dte']|default(trade['DTE']|default(0)|int) }}"
                                    data-daily-theta="{{ trade['_daily_theta_decay_dollars']|default(0) }}"
                                    data-forward-theta="{{ trade['_forward_theta_daily']|default(0) }}"
                                    data-pl="{{ trade['_pl_dollars']|default(0) }}">

                                    <!-- VARIABLE SETS — INSIDE LOOP -->
                                    {% set symbol = trade['Symbol']|default('N/A') %}
                                    {% set strike = trade['Strike']|default(0)|float %}
                                    {% set entry_prem = trade['Entry Premium']|default(0)|float %}
                                    {% set current_mark = trade['_current_mark']|default(0)|float %}
                                    {% set bid = trade['_bid']|default(0)|float %}
                                    {% set ask = trade['_ask']|default(0)|float %}
                                    {% set pl_dollars = trade['_pl_dollars']|default(0) %}
                                    {% set progress = trade['_progress_pct']|default(0) %}
                                    {% set underlying = trade['_underlying_price']|default(trade['Underlying Price']|default(0)|safe_float) %}
                                    {% set delta = trade['_delta']|default(trade['Delta']|default(0)|safe_float) %}
                                    {% set iv = trade['_iv']|default(trade['IV']|default(0)|safe_float) %}
                                    {% set dte = trade['_dte']|default(trade['DTE']|default(0)|int) %}
                                    {% set days_open = trade['_days_open']|default(trade['Days Since Entry']|default(0)|int) %}
                                    {% set daily_theta = trade['_daily_theta_decay_dollars']|default(0) %}
                                    {% set forward_theta = trade['_forward_theta_daily']|default(0) %}

                                    <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px;">
                                        <div>
                                            <h3 style="margin:0;">
                                                <!-- FIXED: Use consistent key 'Symbol' (capital S) -->
                                                {{ get_company_name(trade['Symbol']) or trade['Symbol']|default('N/A') }}
                                                <span style="color:#94a3b8; margin-left:8px;">({{ trade['Symbol']|default('N/A') }})</span>
                                            </h3>
                                            <p style="color:#94a3b8; font-size:0.95rem; margin:4px 0 0 0;">
                                                📅 Exp: {{ trade['Exp Date']|default('N/A') }}
                                                | DTE: <strong>{{ dte }}</strong>
                                                | Open: <strong>{{ days_open }} days</strong>
                                            </p>
                                        </div>

                                        <!-- Enhanced Score Badge -->
                                        <div style="text-align:right; min-width:140px;">
                                            <div style="display:inline-block; padding:8px 16px; border-radius:12px; background:linear-gradient(135deg, {{ trade['score_badge_color']|default('#94a3b8') }}22, {{ trade['score_badge_color']|default('#94a3b8') }}44); border:2px solid {{ trade['score_badge_color']|default('#94a3b8') }};">
                                                <div style="font-size:1.8rem; margin-bottom:2px;">{{ trade['score_badge']|default('⚠️') }}</div>
                                                <div style="font-size:1.1rem; font-weight:bold; color:{{ trade['score_badge_color']|default('#94a3b8') }};">{{ trade['grok_trade_score']|default('0')|int }}/100</div>
                                                <div style="font-size:0.85rem; color:#94a3b8; margin-top:2px;">Grade: {{ trade['score_letter_grade']|default('C') }}</div>
                                            </div>
                                            {% if trade['trade_score_data'] and trade['trade_score_data'].get('component_scores') %}
                                            <div style="margin-top:8px; font-size:0.75rem; color:#94a3b8; line-height:1.3;">
                                                Greeks: {{ trade['trade_score_data']['component_scores'].get('greeks', 0)|int }}<br>
                                                Risk: {{ trade['trade_score_data']['component_scores'].get('risk', 0)|int }}<br>
                                                Profit: {{ trade['trade_score_data']['component_scores'].get('profitability', 0)|int }}<br>
                                                Mgmt: {{ trade['trade_score_data']['component_scores'].get('management', 0)|int }}
                                            </div>
                                            {% endif %}
                                        </div>
                                    </div>

                                    <p style="font-size:1.1rem; margin:16px 0;">
                                        💰 <strong>Entry:</strong> 
                                        <strong style="color:#34d399;">${{ entry_prem|safe_format("%.2f") }}</strong>
                                        <span style="margin-left:16px; color:#94a3b8;">
                                            @ <strong>${{ strike|safe_format("%.0f") }}</strong> strike
                                        </span>
                                    </p>

                                    <!-- DYNAMIC EXIT SUGGESTION — PROMINENT -->
                                    {% if trade.exit_suggestion %}
                                        <div style="margin:20px 0; padding:16px; border-radius:12px; background:{% if trade.exit_suggestion == 'CLOSE NOW' %}rgba(251,146,60,0.25){% else %}rgba(52,211,153,0.2){% endif %}; border-left:6px solid {% if trade.exit_suggestion == 'CLOSE NOW' %}#fb923c{% else %}#34d399{% endif %};">
                                            <strong style="font-size:1.3rem; color:{% if trade.exit_suggestion == 'CLOSE NOW' %}#fb923c{% else %}#34d399{% endif %};">
                                                🎯 Exit Suggestion: {{ trade.exit_suggestion }}
                                            </strong><br>
                                            <span style="color:#e2e8f0; font-size:1rem;">{{ trade.exit_reason }}</span>
                                        </div>
                                    {% endif %}

                                    <!-- Progress Bar -->
                                    <div style="margin:10px 0;">
                                        <p>📈 Progress to 50% Target:
                                            <strong style="color:{% if progress >= 50 %}#34d399{% else %}#f59e0b{% endif %}">
                                                {{ progress|safe_format("%.1f") }}%
                                            </strong>
                                        </p>
                                        <div class="progress-container visible" style="height:36px;">
                                            <div class="progress-fill" style="width: {{ [progress, 0]|max }}%;
                                                background: linear-gradient(90deg,
                                                    {% if progress >= 90 %}#166534{% elif progress >= 70 %}#34d399{% elif progress >= 40 %}#f59e0b{% else %}#fb923c{% endif %},
                                                    {% if progress >= 90 %}#34d399{% elif progress >= 70 %}#6ee7b7{% elif progress >= 40 %}#fbbf24{% else %}#fca5a5{% endif %});">
                                                <div class="progress-text" style="font-size:20px;">{{ progress|safe_format("%.1f") }}%</div>
                                                <div class="progress-glow"></div>
                                            </div>
                                        </div>
                                    </div>

                                    <!-- Current Market Data -->
                                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin:10px 0;">
                                        <div>
                                            <strong>Current Mark:</strong>
                                            <span data-field="current-mark" style="color:#fbbf24; font-size:1.1rem;">{{ trade['Current Mark']|default('$0.00') }}</span><br>
                                            <strong>Bid/Ask:</strong> <span data-field="bid-ask">{{ trade['Bid']|default('$0.00') }} / {{ trade['Ask']|default('$0.00') }}</span>
                                            {% if trade['_using_fallback_quotes']|default(false) %}
                                                <div class="quote-note">
                                                    ⚠️ Market Closed — Showing Last Known Values
                                                </div>
                                            {% endif %}
                                        </div>
                                        <div>
                                            <strong>P/L:</strong>
                                            <span data-field="pl" style="{% if pl_dollars >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-size:1.1rem;">
                                                {% if pl_dollars >= 0 %}+{% endif %}${{ pl_dollars|abs|safe_format("%.0f") }}
                                            </span>
                                        </div>
                                    </div>

                                    <!-- Theta Row -->
                                    <div class="theta-row">
                                        <div>
                                            <strong style="color:#d1fae5;">⏳ Daily Theta Decay ($)</strong><br>
                                            <span data-field="daily-theta" style="color:#34d399; font-size:1.3rem;">${{ daily_theta|safe_format("%.3f") }}</span>
                                        </div>
                                        <div>
                                            <strong style="color:#d1fae5;">Projected Daily Decay ($)</strong><br>
                                            <span data-field="forward-theta" style="color:#34d399; font-size:1.3rem;">${{ forward_theta|safe_format("%.2f") }}</span>
                                        </div>
                                    </div>

                                    <!-- Assignment Simulator -->
                                    <div class="assignment-sim">
                                        <strong style="color:#0ea5e9;">📊 Assignment Simulator</strong>
                                        <p><strong>If expires worthless:</strong> <span style="color:#34d399;">{{ trade['profit_if_expires_formatted']|default('+$0') }}</span></p>
                                        <p><strong>If assigned today:</strong><br>
                                            New cost basis: ${{ trade['cost_basis_if_assigned']|default(0)|float|safe_format("%.2f") }}<br>
                                            Current value: {{ trade['current_value_formatted']|default('$0') }}<br>
                                            Unrealized: <span style="{% if trade['unrealized_if_assigned']|default(0)|float > 0 %}color:#34d399{% else %}color:#fb923c{% endif %}">
                                                {{ trade['unrealized_formatted']|default('$0 gain') }}
                                            </span>
                                        </p>
                                        <p><strong>Risk Level:</strong> 
                                            <span style="color:{% if trade['assignment_risk'] == 'Low' %}#34d399{% elif trade['assignment_risk'] == 'Moderate' %}#f59e0b{% else %}#fb923c{% endif %}">
                                                {{ trade['assignment_risk']|default('Low') }}
                                            </span>
                                        </p>
                                    </div>

                                    <!-- Live Pricing Info for Close -->
                                    <div style="margin:16px 0; padding:12px; background:rgba(30,41,59,0.8); border-radius:10px; border:1px solid #334155;">
                                        <div style="display:flex; justify-content:space-between; color:#e2e8f0; font-size:0.95rem; margin-bottom:8px;">
                                            <span>Bid: <strong style="color:#34d399;">${{ bid|safe_format("%.2f") }}</strong></span>
                                            <span>Mark: <strong style="color:#60a5fa;">${{ current_mark|safe_format("%.2f") }}</strong></span>
                                            <span>Ask: <strong style="color:#fb923c;">${{ ask|safe_format("%.2f") }}</strong></span>
                                        </div>
                                        <div style="color:#94a3b8; font-size:0.85rem; text-align:center;">
                                            {% set contracts_qty = trade['Contracts Qty']|default(1)|int %}
                                            Contracts: {{ contracts_qty }} • Total to Close: ${{ (ask * 100 * contracts_qty)|safe_format("%.2f") }}
                                        </div>
                                    </div>

                                    <!-- Action Buttons -->
                                    <div class="action-buttons-csp">
                                        <button class="action-btn action-btn-close" onclick="quickAction('close50', '{{ symbol }}')">
                                            Option Closed
                                        </button>
                                        <button class="action-btn action-btn-roll" onclick="quickAction('suggest_roll', '{{ symbol }}')">
                                            Suggest Roll
                                        </button>
                                        <button class="refresh-btn" onclick="updateGrokInsight('{{ trade.Symbol }}', '{{ trade.Strike }}', '{{ trade['Exp Date'] }}', this)">🔄 Update Insight</button>
                                        <button class="action-btn"
                                            style="background:linear-gradient(135deg, #10b981, #059669); margin-top:8px; width:100%;"
                                            onclick="buyToClose('{{ trade.Symbol }}', {{ trade.Strike }}, '{{ trade['Exp Date'] }}', {{ trade['Contracts Qty']|default(1) }}, {{ ask }}, this)">
                                            💰 Buy To Close @ ${{ ask|safe_format("%.2f") }}
                                        </button>
                                    </div>

                                    <!-- Grok Analysis -->
                                    <div class="grok-insight">
                                        {% set prob = trade.get('grok_profit_prob', 'N/A') %}
                                        {% set oneliner = trade.get('grok_one_liner', 'Hold and collect theta. Monitor if price approaches strike.') %}

                                        <div class="grok-probability">
                                            {% if prob != 'N/A' and prob %}
                                                {% set prob_pct = (prob|safe_float) * 100 %}
                                                🤖 Grok 50% Profit Odds: {{ prob_pct|safe_format("%.0f") }}%
                                            {% else %}
                                                🤖 Grok 50% Profit Odds: N/A
                                            {% endif %}
                                        </div>
                                        <div class="grok-oneliner">
                                            {{ oneliner }}
                                            {% if trade['grok_analysis'] %}
                                                <br><br>
                                                {{ trade['grok_analysis']|replace('\n', '<br>') }}
                                            {% endif %}
                                        </div>
                                    </div>
                                </div> <!-- ← THIS CLOSING DIV WAS MISSING — NOW ADDED -->
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No open cash-secured puts at this time.</div>
                    {% endif %}

                    <!-- CSP Dynamic Refresh JavaScript -->
                    <script>
                    // Auto-refresh interval tracker
                    let cspAutoRefreshInterval = null;

                    // Initialize auto-refresh on page load (10 min default)
                    document.addEventListener('DOMContentLoaded', function() {
                        setAutoRefresh(600000);  // 10 minutes default
                    });

                    function setAutoRefresh(intervalMs) {
                        // Clear existing interval
                        if (cspAutoRefreshInterval) {
                            clearInterval(cspAutoRefreshInterval);
                            cspAutoRefreshInterval = null;
                        }

                        // Set new interval if not 0
                        if (intervalMs && parseInt(intervalMs) > 0) {
                            cspAutoRefreshInterval = setInterval(() => refreshOpenCSPs(false), parseInt(intervalMs));
                            console.log('CSP auto-refresh set to', intervalMs, 'ms');
                        } else {
                            console.log('CSP auto-refresh disabled');
                        }
                    }

                    async function refreshOpenCSPs(forceRefresh = false) {
                        const refreshBtn = document.getElementById('csp-refresh-btn');
                        const refreshIcon = document.getElementById('csp-refresh-icon');
                        const refreshStatus = document.getElementById('csp-refresh-status');
                        const updateTimeEl = document.getElementById('csp-update-time');

                        // Show loading state
                        if (refreshBtn) refreshBtn.disabled = true;
                        if (refreshIcon) refreshIcon.innerHTML = '⏳';
                        if (refreshStatus) refreshStatus.style.display = 'inline';

                        try {
                            const url = forceRefresh ? '/api/open_csps?force_refresh=true' : '/api/open_csps';
                            const response = await fetch(url);

                            // Check if response is OK
                            if (!response.ok) {
                                let errorMsg = `Server error (${response.status})`;
                                try {
                                    const errorData = await response.json();
                                    errorMsg = errorData.error || errorMsg;
                                } catch (e) {
                                    errorMsg = await response.text() || errorMsg;
                                }
                                throw new Error(errorMsg);
                            }

                            const data = await response.json();

                            if (data.error) {
                                throw new Error(data.error);
                            }

                            // Update the summary section
                            updateCSPSummary(data.summary);

                            // Update the CSP tiles
                            updateCSPTiles(data.csps);

                            // Update last refresh time
                            if (updateTimeEl) {
                                const now = new Date();
                                updateTimeEl.textContent = now.toLocaleTimeString();
                            }

                            console.log('CSP refresh complete:', data.csps.length, 'positions,', data.quotes_fetched, 'live quotes');

                        } catch (error) {
                            console.error('CSP refresh failed:', error);
                            // Show detailed error message
                            const errorMsg = error.message || 'Unknown error occurred';
                            alert(`Failed to refresh CSPs:\n\n${errorMsg}\n\nCheck browser console for details.`);
                        } finally {
                            // Restore button state
                            if (refreshBtn) refreshBtn.disabled = false;
                            if (refreshIcon) refreshIcon.innerHTML = '🔄';
                            if (refreshStatus) refreshStatus.style.display = 'none';
                        }
                    }

                    function updateCSPSummary(summary) {
                        // Find and update the summary tile if it exists
                        const summaryTile = document.querySelector('.tile_summary');
                        if (!summaryTile || !summary) return;

                        // Update the key values in the summary
                        const summaryHTML = `
                            <h3 style="color:#34d399; margin-top:0;">📊 Open CSP Portfolio Summary (${summary.positions_count} positions)</h3>
                            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap:20px;">
                                <div>
                                    <strong>Total Credit Received:</strong> <span style="color:#34d399; font-size:1.2rem;">$${summary.total_credit.toLocaleString()}</span><br>
                                    <strong>Current P/L:</strong>
                                    <span style="${summary.total_pl >= 0 ? 'color:#34d399' : 'color:#fb923c'}; font-size:1.2rem;">
                                        ${summary.total_pl >= 0 ? '+' : ''}$${Math.abs(summary.total_pl).toLocaleString()}
                                    </span>
                                </div>
                                <div>
                                    <strong>Avg Progress to Target:</strong> <span style="color:#fbbf24;">${summary.avg_progress}%</span><br>
                                    <strong>Avg DTE:</strong> <span style="color:#a7f3d0;">${summary.avg_dte} days</span>
                                </div>
                                <div>
                                    <strong>Total Realized Daily Theta:</strong> <span style="color:#34d399;">$${summary.total_realized_theta.toFixed(2)}</span><br>
                                    <strong>Avg per Position:</strong> <span style="color:#34d399;">$${summary.avg_theta_per_pos.toFixed(3)}</span>
                                </div>
                                <div>
                                    <strong>Total Expected Daily Decay:</strong> <span style="color:#34d399;">$${summary.total_expected_decay.toFixed(2)}</span><br>
                                    <strong>Projected Remaining Decay:</strong> <span style="color:#34d399;">~$${summary.projected_remaining.toLocaleString()}</span>
                                </div>
                            </div>
                        `;
                        summaryTile.innerHTML = summaryHTML;
                    }

                    function updateCSPTiles(csps) {
                        const grid = document.getElementById('open-csp-grid');
                        if (!grid) return;

                        // Update each tile with fresh data (match by symbol + strike + exp)
                        csps.forEach(csp => {
                            const tiles = grid.querySelectorAll('.tile_csp');
                            tiles.forEach(tile => {
                                // Match tile by data attributes or content
                                const tileSymbol = tile.querySelector('h3 span')?.textContent?.match(/\\(([^)]+)\\)/)?.[1];
                                const tileScore = parseFloat(tile.dataset.score || 0);

                                if (tileSymbol === csp.Symbol) {
                                    // Update data attributes for sorting
                                    tile.dataset.score = csp.grok_trade_score || 0;
                                    tile.dataset.progress = Math.round(csp._progress_pct || 0);
                                    tile.dataset.dte = csp._dte || 0;
                                    tile.dataset.dailyTheta = csp._daily_theta_decay_dollars || 0;
                                    tile.dataset.forwardTheta = csp._forward_theta_daily || 0;
                                    tile.dataset.pl = csp._pl_dollars || 0;

                                    // Update current mark display
                                    const markEl = tile.querySelector('[data-field="current-mark"]');
                                    if (markEl && csp._current_mark !== undefined) {
                                        markEl.textContent = '$' + csp._current_mark.toFixed(2);
                                    }

                                    // Update P/L display
                                    const plElements = tile.querySelectorAll('[data-field="pl"]');
                                    plElements.forEach(plEl => {
                                        const pl = csp._pl_dollars || 0;
                                        plEl.textContent = (pl >= 0 ? '+' : '') + '$' + Math.abs(pl).toFixed(0);
                                        plEl.style.color = pl >= 0 ? '#34d399' : '#fb923c';
                                    });

                                    // Update progress bar
                                    const progressFill = tile.querySelector('.progress-fill');
                                    const progressText = tile.querySelector('.progress-text');
                                    if (progressFill && csp._progress_pct !== undefined) {
                                        const progress = csp._progress_pct;
                                        progressFill.style.width = Math.max(progress, 0) + '%';
                                        if (progressText) progressText.textContent = progress.toFixed(1) + '%';

                                        // Update progress bar color
                                        let gradient;
                                        if (progress >= 90) gradient = 'linear-gradient(90deg, #166534, #34d399)';
                                        else if (progress >= 70) gradient = 'linear-gradient(90deg, #34d399, #6ee7b7)';
                                        else if (progress >= 40) gradient = 'linear-gradient(90deg, #f59e0b, #fbbf24)';
                                        else gradient = 'linear-gradient(90deg, #fb923c, #fca5a5)';
                                        progressFill.style.background = gradient;
                                    }

                                    // Update bid/ask/mark
                                    const bidAskEl = tile.querySelector('[data-field="bid-ask"]');
                                    if (bidAskEl && csp._bid !== undefined && csp._ask !== undefined) {
                                        bidAskEl.innerHTML = `$${csp._bid.toFixed(2)} / $${csp._ask.toFixed(2)}`;
                                    }

                                    // Update theta values
                                    const dailyThetaEl = tile.querySelector('[data-field="daily-theta"]');
                                    if (dailyThetaEl && csp._daily_theta_decay_dollars !== undefined) {
                                        dailyThetaEl.textContent = '$' + csp._daily_theta_decay_dollars.toFixed(3);
                                    }

                                    const forwardThetaEl = tile.querySelector('[data-field="forward-theta"]');
                                    if (forwardThetaEl && csp._forward_theta_daily !== undefined) {
                                        forwardThetaEl.textContent = '$' + csp._forward_theta_daily.toFixed(2);
                                    }
                                }
                            });
                        });

                        // Re-apply current sort after data update
                        const sortSelect = document.getElementById('csp-sort');
                        if (sortSelect && typeof sortCSPTiles === 'function') {
                            sortCSPTiles();
                        }
                    }
                    </script>
                </div>

                <!-- Analytics Tab (Combined) -->
                <div id="analytics" class="tab-content">
                    <h2 style="color:#60a5fa; margin-bottom:24px;">📈 Trading Analytics Dashboard</h2>

                    <!-- Sub-tabs for Analytics -->
                    <div class="sub-tabs" style="display:flex; gap:12px; margin-bottom:32px; flex-wrap:wrap;">
                        <button class="sub-tab active" onclick="showAnalyticsSubTab('performance')">
                            📊 Performance
                        </button>
                        <button class="sub-tab" onclick="showAnalyticsSubTab('greeks')">
                            📉 Portfolio Greeks
                        </button>
                    </div>

                    <!-- Performance Overview Sub-tab -->
                    <div id="analytics-performance" class="analytics-subtab">
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; margin-bottom:24px;">
                            <h3 style="color:#e2e8f0; margin-bottom:20px;">📊 Trade Performance Summary</h3>
                            {% if trade_history and trade_history|length > 0 %}
                            {% set total_trades = trade_history|length %}
                            {% set ns = namespace(wins=0, losses=0, total_pnl=0.0, total_win_pnl=0.0, total_loss_pnl=0.0, total_days_held=0) %}
                            {% for trade in trade_history %}
                                {% set pnl_raw = trade.get('Net Profit $', '0')|string|replace('$', '')|replace(',', '') %}
                                {% set pnl = pnl_raw|safe_float %}
                                {% set days = trade.get('Days Held', 0)|safe_float %}
                                {% if pnl > 0 %}
                                    {% set ns.wins = ns.wins + 1 %}
                                    {% set ns.total_win_pnl = ns.total_win_pnl + pnl %}
                                {% else %}
                                    {% set ns.losses = ns.losses + 1 %}
                                    {% set ns.total_loss_pnl = ns.total_loss_pnl + (pnl|abs) %}
                                {% endif %}
                                {% set ns.total_pnl = ns.total_pnl + pnl %}
                                {% set ns.total_days_held = ns.total_days_held + days %}
                            {% endfor %}
                            {% set win_rate = ((ns.wins / total_trades) * 100)|round(1) if total_trades > 0 else 0 %}
                            {% set avg_win = (ns.total_win_pnl / ns.wins)|round(2) if ns.wins > 0 else 0 %}
                            {% set avg_loss = (ns.total_loss_pnl / ns.losses)|round(2) if ns.losses > 0 else 0 %}
                            {% set avg_days_held = (ns.total_days_held / total_trades)|round(1) if total_trades > 0 else 0 %}
                            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:20px;">
                                <div style="text-align:center; padding:20px; background:rgba(52,211,153,0.1); border-radius:12px; border:1px solid #34d399;">
                                    <div style="font-size:2.5rem; color:#34d399; font-weight:bold;">{{ win_rate }}%</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; margin-top:8px;">Win Rate</div>
                                    <div style="color:#cbd5e1; font-size:0.85rem; margin-top:4px;">{{ ns.wins }}W / {{ ns.losses }}L</div>
                                </div>
                                <div style="text-align:center; padding:20px; background:rgba(96,165,250,0.1); border-radius:12px; border:1px solid #60a5fa;">
                                    <div style="font-size:2.5rem; {% if ns.total_pnl >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-weight:bold;">${{ ns.total_pnl|round(2) }}</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; margin-top:8px;">Total P/L</div>
                                    <div style="color:#cbd5e1; font-size:0.85rem; margin-top:4px;">{{ total_trades }} trades</div>
                                </div>
                                <div style="text-align:center; padding:20px; background:rgba(251,191,36,0.1); border-radius:12px; border:1px solid #fbbf24;">
                                    <div style="font-size:2.5rem; color:#34d399; font-weight:bold;">${{ avg_win }}</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; margin-top:8px;">Avg Win</div>
                                    <div style="color:#cbd5e1; font-size:0.85rem; margin-top:4px;">Avg Loss: ${{ avg_loss }}</div>
                                </div>
                                <div style="text-align:center; padding:20px; background:rgba(168,85,247,0.1); border-radius:12px; border:1px solid #a855f7;">
                                    <div style="font-size:2.5rem; color:#a855f7; font-weight:bold;">{{ avg_days_held }}</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; margin-top:8px;">Avg Days Held</div>
                                    <div style="color:#cbd5e1; font-size:0.85rem; margin-top:4px;">{{ total_trades }} closed trades</div>
                                </div>
                            </div>
                            {% else %}
                            <p style="color:#94a3b8; text-align:center; padding:20px;">No trade history available</p>
                            {% endif %}
                        </div>

                        <!-- Current Open Trades Table -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; margin-bottom:24px;">
                            <h3 style="color:#e2e8f0; margin-bottom:20px;">📈 Current Open Trades</h3>
                            {% if open_trades and open_trades|length > 0 %}
                                <div style="overflow-x:auto;">
                                    <table style="width:100%; border-collapse:collapse; color:#e2e8f0;">
                                        <thead>
                                            <tr style="border-bottom:2px solid #334155;">
                                                <th style="padding:12px; text-align:left; color:#94a3b8; font-weight:600;">Symbol</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Strike</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Exp</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">DTE</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Contracts</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Entry $</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Current $</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">P/L</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Progress</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for trade in open_trades %}
                                            <tr style="border-bottom:1px solid #334155;">
                                                <td style="padding:12px; font-weight:600;">{{ trade['Symbol'] }}</td>
                                                <td style="padding:12px; text-align:right;">${{ trade['Strike']|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right;">{{ trade['Exp Date'] }}</td>
                                                <td style="padding:12px; text-align:right;">{{ trade.get('_dte', 'N/A') }}</td>
                                                <td style="padding:12px; text-align:right;">{{ trade['Contracts Qty']|default(1) }}</td>
                                                <td style="padding:12px; text-align:right; color:#34d399;">${{ trade['Entry Premium']|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right; color:#60a5fa;">${{ trade.get('_current_premium', 0)|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right; {% if trade.get('_pl_dollars', 0) >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-weight:600;">
                                                    {% if trade.get('_pl_dollars', 0) >= 0 %}+{% endif %}${{ trade.get('_pl_dollars', 0)|safe_format("%.0f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#fbbf24; font-weight:600;">{{ trade.get('_progress_pct', 0)|safe_format("%.0f") }}%</td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            {% else %}
                                <p style="color:#94a3b8; text-align:center; padding:20px;">No open trades</p>
                            {% endif %}
                        </div>

                        <!-- Trade History Table (Server-side rendered) -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; margin-bottom:24px;">
                            <h3 style="color:#e2e8f0; margin-bottom:20px;">📋 Trade History (Closed Trades)</h3>
                            {% if trade_history and trade_history|length > 0 %}
                                <div style="overflow-x:auto; max-height:500px; overflow-y:auto;">
                                    <table style="width:100%; border-collapse:collapse; color:#e2e8f0;">
                                        <thead style="position:sticky; top:0; background:#1e293b; z-index:10;">
                                            <tr style="border-bottom:2px solid #334155;">
                                                <th style="padding:12px; text-align:left; color:#94a3b8; font-weight:600;">Symbol</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Strike</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Entry</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Exit</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Days</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Entry $</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Exit $</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Net P/L</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">ROI%</th>
                                                <th style="padding:12px; text-align:center; color:#94a3b8; font-weight:600;">Result</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for trade in trade_history %}
                                            {% set pnl_raw = trade.get('Net Profit $', '0')|string|replace('$', '')|replace(',', '') %}
                                            {% set pnl = pnl_raw|safe_float %}
                                            {% set roi_raw = trade.get('ROI %', '0')|string|replace('%', '') %}
                                            {% set roi = roi_raw|safe_float %}
                                            {% set strike_raw = trade.get('Strike', '0')|string|replace('$', '')|replace(',', '') %}
                                            {% set entry_prem_raw = trade.get('Entry Premium', '0')|string|replace('$', '')|replace(',', '') %}
                                            {% set exit_prem_raw = trade.get('Exit Premium', '0')|string|replace('$', '')|replace(',', '') %}
                                            {% set win_loss = trade.get('Win/Loss', '') %}
                                            <tr style="border-bottom:1px solid #334155;">
                                                <td style="padding:12px; font-weight:600;">{{ trade.get('Symbol', 'N/A') }}</td>
                                                <td style="padding:12px; text-align:right;">${{ strike_raw|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right; font-size:0.85rem;">{{ trade.get('Entry Date', 'N/A') }}</td>
                                                <td style="padding:12px; text-align:right; font-size:0.85rem;">{{ trade.get('Exit Date', 'N/A') }}</td>
                                                <td style="padding:12px; text-align:right;">{{ trade.get('Days Held', 0) }}</td>
                                                <td style="padding:12px; text-align:right; color:#34d399;">${{ entry_prem_raw|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right; color:#60a5fa;">${{ exit_prem_raw|safe_format("%.2f") }}</td>
                                                <td style="padding:12px; text-align:right; {% if pnl >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-weight:600;">
                                                    {% if pnl >= 0 %}+{% endif %}${{ pnl|safe_format("%.2f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; {% if roi >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}; font-weight:600;">
                                                    {% if roi >= 0 %}+{% endif %}{{ roi|safe_format("%.2f") }}%
                                                </td>
                                                <td style="padding:12px; text-align:center;">
                                                    {% if win_loss == 'WIN' %}
                                                    <span style="background:#10b981; color:white; padding:4px 10px; border-radius:12px; font-size:0.8rem; font-weight:600;">WIN</span>
                                                    {% elif win_loss == 'LOSS' %}
                                                    <span style="background:#ef4444; color:white; padding:4px 10px; border-radius:12px; font-size:0.8rem; font-weight:600;">LOSS</span>
                                                    {% else %}
                                                    <span style="background:#6b7280; color:white; padding:4px 10px; border-radius:12px; font-size:0.8rem;">{{ win_loss or 'N/A' }}</span>
                                                    {% endif %}
                                                </td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                                <div style="color:#94a3b8; font-size:0.85rem; margin-top:12px; text-align:right;">
                                    Showing {{ trade_history|length }} closed trades
                                </div>
                            {% else %}
                                <p style="color:#94a3b8; text-align:center; padding:20px;">No closed trades found in Trade_History sheet</p>
                            {% endif %}
                        </div>
                    </div>

                    <!-- Portfolio Greeks Sub-tab -->
                    <div id="analytics-greeks" class="analytics-subtab" style="display:none;">
                        {% if open_trades %}
                            <!-- Aggregate Greeks Cards -->
                            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(250px, 1fr)); gap:24px; margin-bottom:40px;">
                                <!-- Delta Card -->
                                <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                    <div style="font-size:3rem; margin-bottom:12px;">Δ</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Delta</div>
                                    <div id="analytics-portfolio-delta" style="font-size:2.5rem; font-weight:bold; color:#60a5fa;">--</div>
                                    <div id="analytics-delta-exposure" style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Loading...</div>
                                </div>

                                <!-- Gamma Card -->
                                <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                    <div style="font-size:3rem; margin-bottom:12px;">Γ</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Gamma</div>
                                    <div id="analytics-portfolio-gamma" style="font-size:2.5rem; font-weight:bold; color:#a78bfa;">--</div>
                                    <div style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Delta acceleration</div>
                                </div>

                                <!-- Theta Card -->
                                <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                    <div style="font-size:3rem; margin-bottom:12px;">Θ</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Theta</div>
                                    <div id="analytics-portfolio-theta" style="font-size:2.5rem; font-weight:bold; color:#34d399;">--</div>
                                    <div id="analytics-theta-daily" style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Loading...</div>
                                </div>

                                <!-- Vega Card -->
                                <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                    <div style="font-size:3rem; margin-bottom:12px;">ν</div>
                                    <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Vega</div>
                                    <div id="analytics-portfolio-vega" style="font-size:2.5rem; font-weight:bold; color:#fbbf24;">--</div>
                                    <div style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">IV sensitivity</div>
                                </div>
                            </div>

                            <!-- Directional Exposure Gauge -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:32px; border-radius:20px; border:2px solid #334155; margin-bottom:32px;">
                                <h3 style="color:#e2e8f0; margin-bottom:24px; text-align:center;">Directional Exposure</h3>
                                <svg id="analytics-exposure-gauge" width="100%" height="200" viewBox="0 0 400 200" style="max-width:600px; margin:0 auto; display:block;">
                                    <!-- Gauge background -->
                                    <defs>
                                        <linearGradient id="analyticsGaugeGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                                            <stop offset="0%" style="stop-color:#ef4444;stop-opacity:1" />
                                            <stop offset="50%" style="stop-color:#94a3b8;stop-opacity:1" />
                                            <stop offset="100%" style="stop-color:#34d399;stop-opacity:1" />
                                        </linearGradient>
                                    </defs>
                                    <path d="M 50 150 A 150 150 0 0 1 350 150" stroke="url(#analyticsGaugeGradient)" stroke-width="20" fill="none"/>
                                    <text x="50" y="180" fill="#ef4444" font-size="14" text-anchor="middle">Bearish</text>
                                    <text x="200" y="180" fill="#94a3b8" font-size="14" text-anchor="middle">Neutral</text>
                                    <text x="350" y="180" fill="#34d399" font-size="14" text-anchor="middle">Bullish</text>
                                    <!-- Needle -->
                                    <line id="analytics-gauge-needle" x1="200" y1="150" x2="200" y2="50" stroke="#60a5fa" stroke-width="4" stroke-linecap="round"/>
                                    <circle cx="200" cy="150" r="8" fill="#60a5fa"/>
                                </svg>
                                <div id="analytics-exposure-label" style="text-align:center; font-size:1.2rem; font-weight:bold; color:#e2e8f0; margin-top:16px;">
                                    Loading exposure...
                                </div>
                            </div>

                            <!-- Position Greeks Breakdown Table -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155;">
                                <h3 style="color:#e2e8f0; margin-bottom:20px;">Position Greeks Breakdown</h3>
                                <div style="overflow-x:auto;">
                                    <table style="width:100%; border-collapse:collapse;">
                                        <thead>
                                            <tr style="border-bottom:2px solid #334155;">
                                                <th style="padding:12px; text-align:left; color:#94a3b8; font-weight:600;">Symbol</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Delta</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Gamma</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Theta</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Vega</th>
                                                <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">DTE</th>
                                            </tr>
                                        </thead>
                                        <tbody id="analytics-greeks-table-body">
                                            {% for trade in open_trades %}
                                            <tr style="border-bottom:1px solid #334155;">
                                                <td style="padding:12px; color:#e2e8f0; font-weight:500;">
                                                    {{ trade['Symbol']|default('N/A') }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#60a5fa; font-weight:600;">
                                                    {{ trade.get('Delta', 0)|float|safe_format("%.3f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#a78bfa;">
                                                    {{ trade.get('Gamma', 0)|float|safe_format("%.3f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#34d399;">
                                                    {{ trade.get('Theta', 0)|float|safe_format("%.3f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#fbbf24;">
                                                    {{ trade.get('Vega', 0)|float|safe_format("%.3f") }}
                                                </td>
                                                <td style="padding:12px; text-align:right; color:#94a3b8;">
                                                    {{ trade.get('DTE', 0)|int }}
                                                </td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>

                            <!-- JavaScript to calculate and display aggregate Greeks in Analytics tab -->
                            <script>
                                // Initialize Greeks calculations when Analytics Greeks tab is shown
                                function initAnalyticsGreeks() {
                                    const trades = {{ open_trades|tojson|safe }};

                                    let totalDelta = 0;
                                    let totalGamma = 0;
                                    let totalTheta = 0;
                                    let totalVega = 0;

                                    trades.forEach(trade => {
                                        totalDelta += parseFloat(trade.Delta || 0);
                                        totalGamma += parseFloat(trade.Gamma || 0);
                                        totalTheta += parseFloat(trade.Theta || 0);
                                        totalVega += parseFloat(trade.Vega || 0);
                                    });

                                    // Update Delta
                                    document.getElementById('analytics-portfolio-delta').textContent = totalDelta.toFixed(3);
                                    const deltaExposure = Math.abs(totalDelta) < 0.10 ? 'Neutral' :
                                                         totalDelta > 0 ? 'Bullish' : 'Bearish';
                                    document.getElementById('analytics-delta-exposure').textContent = deltaExposure + ' positioning';
                                    document.getElementById('analytics-delta-exposure').style.color =
                                        totalDelta > 0.10 ? '#34d399' : totalDelta < -0.10 ? '#ef4444' : '#94a3b8';

                                    // Update Gamma
                                    document.getElementById('analytics-portfolio-gamma').textContent = totalGamma.toFixed(3);

                                    // Update Theta (convert to daily dollar amount)
                                    const thetaDaily = totalTheta * 100; // Per contract to dollars
                                    document.getElementById('analytics-portfolio-theta').textContent = totalTheta.toFixed(3);
                                    document.getElementById('analytics-theta-daily').textContent =
                                        '$' + thetaDaily.toFixed(2) + '/day decay';

                                    // Update Vega
                                    document.getElementById('analytics-portfolio-vega').textContent = totalVega.toFixed(3);

                                    // Update directional gauge
                                    // Map delta from -1 to +1 to angle from 180° to 0°
                                    const clampedDelta = Math.max(-1, Math.min(1, totalDelta));
                                    const angle = 180 - ((clampedDelta + 1) * 90); // 180° = bearish, 90° = neutral, 0° = bullish
                                    const radians = angle * (Math.PI / 180);
                                    const needleLength = 100;
                                    const x2 = 200 + needleLength * Math.cos(radians);
                                    const y2 = 150 - needleLength * Math.sin(radians);

                                    const needle = document.getElementById('analytics-gauge-needle');
                                    needle.setAttribute('x2', x2);
                                    needle.setAttribute('y2', y2);

                                    // Update exposure label
                                    let exposureText = 'Delta-Neutral';
                                    if (Math.abs(totalDelta) >= 0.5) {
                                        exposureText = totalDelta > 0 ? 'Strong Bullish Bias' : 'Strong Bearish Bias';
                                    } else if (Math.abs(totalDelta) >= 0.25) {
                                        exposureText = totalDelta > 0 ? 'Moderate Bullish Bias' : 'Moderate Bearish Bias';
                                    } else if (Math.abs(totalDelta) >= 0.10) {
                                        exposureText = totalDelta > 0 ? 'Slight Bullish Bias' : 'Slight Bearish Bias';
                                    }
                                    document.getElementById('analytics-exposure-label').textContent = exposureText;
                                }

                                // Initialize when page loads and when switching to greeks tab
                                if (document.getElementById('analytics-greeks').style.display !== 'none') {
                                    initAnalyticsGreeks();
                                }
                            </script>
                        {% else %}
                            <div class="empty">No open positions to analyze.</div>
                        {% endif %}
                    </div>

                    <script>
                    function showAnalyticsSubTab(subtab) {
                        // Hide all sub-tabs
                        document.querySelectorAll('.analytics-subtab').forEach(el => el.style.display = 'none');
                        // Remove active class from all sub-tab buttons
                        document.querySelectorAll('.sub-tab').forEach(btn => btn.classList.remove('active'));

                        // Show selected sub-tab
                        document.getElementById('analytics-' + subtab).style.display = 'block';
                        // Add active class to clicked button
                        event.target.classList.add('active');

                        // Initialize Portfolio Greeks when that sub-tab is shown
                        if (subtab === 'greeks' && typeof initAnalyticsGreeks === 'function') {
                            initAnalyticsGreeks();
                        }
                    }
                    </script>
                </div>

                <!-- Portfolio Greeks Dashboard (Hidden - now part of Analytics) -->
                <div id="portfolio-greeks" class="tab-content" style="display:none;">
                    <h2 style="color:#60a5fa; margin-bottom:32px;">📊 Portfolio Greeks & Directional Exposure</h2>

                    {% if open_trades %}
                        <!-- Aggregate Greeks Cards -->
                        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(250px, 1fr)); gap:24px; margin-bottom:40px;">
                            <!-- Delta Card -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                <div style="font-size:3rem; margin-bottom:12px;">Δ</div>
                                <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Delta</div>
                                <div id="portfolio-delta" style="font-size:2.5rem; font-weight:bold; color:#60a5fa;">--</div>
                                <div id="delta-exposure" style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Loading...</div>
                            </div>

                            <!-- Gamma Card -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                <div style="font-size:3rem; margin-bottom:12px;">Γ</div>
                                <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Gamma</div>
                                <div id="portfolio-gamma" style="font-size:2.5rem; font-weight:bold; color:#a78bfa;">--</div>
                                <div style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Delta acceleration</div>
                            </div>

                            <!-- Theta Card -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                <div style="font-size:3rem; margin-bottom:12px;">Θ</div>
                                <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Theta</div>
                                <div id="portfolio-theta" style="font-size:2.5rem; font-weight:bold; color:#34d399;">--</div>
                                <div id="theta-daily" style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">Loading...</div>
                            </div>

                            <!-- Vega Card -->
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; text-align:center;">
                                <div style="font-size:3rem; margin-bottom:12px;">ν</div>
                                <div style="color:#94a3b8; font-size:0.9rem; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Portfolio Vega</div>
                                <div id="portfolio-vega" style="font-size:2.5rem; font-weight:bold; color:#fbbf24;">--</div>
                                <div style="color:#94a3b8; font-size:0.85rem; margin-top:8px;">IV sensitivity</div>
                            </div>
                        </div>

                        <!-- Directional Exposure Gauge -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:32px; border-radius:20px; border:2px solid #334155; margin-bottom:32px;">
                            <h3 style="color:#e2e8f0; margin-bottom:24px; text-align:center;">Directional Exposure</h3>
                            <svg id="exposure-gauge" width="100%" height="200" viewBox="0 0 400 200" style="max-width:600px; margin:0 auto; display:block;">
                                <!-- Gauge background -->
                                <defs>
                                    <linearGradient id="gaugeGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                                        <stop offset="0%" style="stop-color:#ef4444;stop-opacity:1" />
                                        <stop offset="50%" style="stop-color:#94a3b8;stop-opacity:1" />
                                        <stop offset="100%" style="stop-color:#34d399;stop-opacity:1" />
                                    </linearGradient>
                                </defs>
                                <path d="M 50 150 A 150 150 0 0 1 350 150" stroke="url(#gaugeGradient)" stroke-width="20" fill="none"/>
                                <text x="50" y="180" fill="#ef4444" font-size="14" text-anchor="middle">Bearish</text>
                                <text x="200" y="180" fill="#94a3b8" font-size="14" text-anchor="middle">Neutral</text>
                                <text x="350" y="180" fill="#34d399" font-size="14" text-anchor="middle">Bullish</text>
                                <!-- Needle -->
                                <line id="gauge-needle" x1="200" y1="150" x2="200" y2="50" stroke="#60a5fa" stroke-width="4" stroke-linecap="round"/>
                                <circle cx="200" cy="150" r="8" fill="#60a5fa"/>
                            </svg>
                            <div id="exposure-label" style="text-align:center; font-size:1.2rem; font-weight:bold; color:#e2e8f0; margin-top:16px;">
                                Loading exposure...
                            </div>
                        </div>

                        <!-- Position Greeks Breakdown Table -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155;">
                            <h3 style="color:#e2e8f0; margin-bottom:20px;">Position Greeks Breakdown</h3>
                            <div style="overflow-x:auto;">
                                <table style="width:100%; border-collapse:collapse;">
                                    <thead>
                                        <tr style="border-bottom:2px solid #334155;">
                                            <th style="padding:12px; text-align:left; color:#94a3b8; font-weight:600;">Symbol</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Delta</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Gamma</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Theta</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Vega</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">DTE</th>
                                        </tr>
                                    </thead>
                                    <tbody id="greeks-table-body">
                                        {% for trade in open_trades %}
                                        <tr style="border-bottom:1px solid #334155;">
                                            <td style="padding:12px; color:#e2e8f0; font-weight:500;">
                                                {{ trade['Symbol']|default('N/A') }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#60a5fa; font-weight:600;">
                                                {{ trade.get('Delta', 0)|float|safe_format("%.3f") }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#a78bfa;">
                                                {{ trade.get('Gamma', 0)|float|safe_format("%.3f") }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#34d399;">
                                                {{ trade.get('Theta', 0)|float|safe_format("%.3f") }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#fbbf24;">
                                                {{ trade.get('Vega', 0)|float|safe_format("%.3f") }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#94a3b8;">
                                                {{ trade.get('DTE', 0)|int }}
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>

                        <!-- JavaScript to calculate and display aggregate Greeks -->
                        <script>
                            (function() {
                                // Calculate aggregate Greeks from trades
                                const trades = {{ open_trades|tojson|safe }};

                                let totalDelta = 0;
                                let totalGamma = 0;
                                let totalTheta = 0;
                                let totalVega = 0;

                                trades.forEach(trade => {
                                    totalDelta += parseFloat(trade.Delta || 0);
                                    totalGamma += parseFloat(trade.Gamma || 0);
                                    totalTheta += parseFloat(trade.Theta || 0);
                                    totalVega += parseFloat(trade.Vega || 0);
                                });

                                // Update Delta
                                document.getElementById('portfolio-delta').textContent = totalDelta.toFixed(3);
                                const deltaExposure = Math.abs(totalDelta) < 0.10 ? 'Neutral' :
                                                     totalDelta > 0 ? 'Bullish' : 'Bearish';
                                document.getElementById('delta-exposure').textContent = deltaExposure + ' positioning';
                                document.getElementById('delta-exposure').style.color =
                                    totalDelta > 0.10 ? '#34d399' : totalDelta < -0.10 ? '#ef4444' : '#94a3b8';

                                // Update Gamma
                                document.getElementById('portfolio-gamma').textContent = totalGamma.toFixed(3);

                                // Update Theta (convert to daily dollar amount)
                                const thetaDaily = totalTheta * 100; // Per contract to dollars
                                document.getElementById('portfolio-theta').textContent = totalTheta.toFixed(3);
                                document.getElementById('theta-daily').textContent =
                                    '$' + thetaDaily.toFixed(2) + '/day decay';

                                // Update Vega
                                document.getElementById('portfolio-vega').textContent = totalVega.toFixed(3);

                                // Update directional gauge
                                // Map delta from -1 to +1 to angle from 180° to 0°
                                const clampedDelta = Math.max(-1, Math.min(1, totalDelta));
                                const angle = 180 - ((clampedDelta + 1) * 90); // 180° = bearish, 90° = neutral, 0° = bullish
                                const radians = angle * (Math.PI / 180);
                                const needleLength = 100;
                                const x2 = 200 + needleLength * Math.cos(radians);
                                const y2 = 150 - needleLength * Math.sin(radians);

                                const needle = document.getElementById('gauge-needle');
                                needle.setAttribute('x2', x2);
                                needle.setAttribute('y2', y2);

                                // Update exposure label
                                let exposureText = 'Delta-Neutral';
                                if (Math.abs(totalDelta) >= 0.5) {
                                    exposureText = totalDelta > 0 ? 'Strong Bullish Bias' : 'Strong Bearish Bias';
                                } else if (Math.abs(totalDelta) >= 0.25) {
                                    exposureText = totalDelta > 0 ? 'Moderate Bullish Bias' : 'Moderate Bearish Bias';
                                } else if (Math.abs(totalDelta) >= 0.10) {
                                    exposureText = totalDelta > 0 ? 'Slight Bullish Bias' : 'Slight Bearish Bias';
                                }
                                document.getElementById('exposure-label').textContent = exposureText;
                            })();
                        </script>
                    {% else %}
                        <div class="empty">No open positions to analyze.</div>
                    {% endif %}
                </div>

                <!-- Risk Dashboard -->
                <div id="risk-dashboard" class="tab-content">
                    <h2 style="color:#60a5fa; margin-bottom:32px;">⚠️ Risk Dashboard & Portfolio Health</h2>

                    {% if open_trades %}
                        <!-- Portfolio Heat Gauge -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:32px; border-radius:20px; border:2px solid #334155; margin-bottom:32px;">
                            <h3 style="color:#e2e8f0; margin-bottom:24px; text-align:center;">Portfolio Heat (Capital at Risk)</h3>
                            <svg id="heat-gauge" width="100%" height="250" viewBox="0 0 400 250" style="max-width:600px; margin:0 auto; display:block;">
                                <!-- Circular gauge background -->
                                <defs>
                                    <linearGradient id="heatGradient">
                                        <stop offset="0%" style="stop-color:#34d399;stop-opacity:1" />
                                        <stop offset="50%" style="stop-color:#fbbf24;stop-opacity:1" />
                                        <stop offset="100%" style="stop-color:#ef4444;stop-opacity:1" />
                                    </linearGradient>
                                </defs>
                                <!-- Background arc (0-100%) -->
                                <path d="M 60 200 A 140 140 0 0 1 340 200" stroke="url(#heatGradient)" stroke-width="30" fill="none" stroke-linecap="round"/>
                                <!-- Percentage markers -->
                                <text x="60" y="220" fill="#34d399" font-size="12" text-anchor="middle">0%</text>
                                <text x="200" y="45" fill="#fbbf24" font-size="12" text-anchor="middle">10%</text>
                                <text x="340" y="220" fill="#ef4444" font-size="12" text-anchor="middle">20%</text>
                                <!-- Center text -->
                                <text id="heat-percentage" x="200" y="180" fill="#e2e8f0" font-size="48" font-weight="bold" text-anchor="middle">--</text>
                                <text x="200" y="205" fill="#94a3b8" font-size="14" text-anchor="middle">Portfolio Heat</text>
                                <!-- Heat indicator (will be drawn dynamically) -->
                                <path id="heat-indicator" stroke="#60a5fa" stroke-width="30" fill="none" stroke-linecap="round"/>
                            </svg>
                            <div id="heat-status" style="text-align:center; font-size:1.1rem; font-weight:600; margin-top:16px; color:#e2e8f0;">
                                Calculating...
                            </div>
                        </div>

                        <!-- Risk Metrics Cards -->
                        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:20px; margin-bottom:32px;">
                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:24px; border-radius:16px; border:2px solid #334155;">
                                <div style="color:#94a3b8; font-size:0.85rem; text-transform:uppercase; margin-bottom:8px;">Total Capital at Risk</div>
                                <div id="total-risk" style="font-size:2rem; font-weight:bold; color:#ef4444;">$--</div>
                            </div>

                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:24px; border-radius:16px; border:2px solid #334155;">
                                <div style="color:#94a3b8; font-size:0.85rem; text-transform:uppercase; margin-bottom:8px;">Open Positions</div>
                                <div id="position-count" style="font-size:2rem; font-weight:bold; color:#60a5fa;">{{ open_trades|length }}</div>
                            </div>

                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:24px; border-radius:16px; border:2px solid #334155;">
                                <div style="color:#94a3b8; font-size:0.85rem; text-transform:uppercase; margin-bottom:8px;">Max Single Position Risk</div>
                                <div id="max-position-risk" style="font-size:2rem; font-weight:bold; color:#fbbf24;">$--</div>
                            </div>

                            <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:24px; border-radius:16px; border:2px solid #334155;">
                                <div style="color:#94a3b8; font-size:0.85rem; text-transform:uppercase; margin-bottom:8px;">Remaining Capacity</div>
                                <div id="remaining-capacity" style="font-size:2rem; font-weight:bold; color:#34d399;">--</div>
                            </div>
                        </div>

                        <!-- Position Risk Breakdown -->
                        <div style="background:linear-gradient(135deg, #1e293b, #0f172a); padding:28px; border-radius:20px; border:2px solid #334155; margin-bottom:32px;">
                            <h3 style="color:#e2e8f0; margin-bottom:20px;">Position Risk Breakdown</h3>
                            <div style="overflow-x:auto;">
                                <table style="width:100%; border-collapse:collapse;">
                                    <thead>
                                        <tr style="border-bottom:2px solid #334155;">
                                            <th style="padding:12px; text-align:left; color:#94a3b8; font-weight:600;">Symbol</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Max Loss</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">% of Portfolio</th>
                                            <th style="padding:12px; text-align:center; color:#94a3b8; font-weight:600;">Risk Level</th>
                                            <th style="padding:12px; text-align:right; color:#94a3b8; font-weight:600;">Distance to Strike</th>
                                        </tr>
                                    </thead>
                                    <tbody id="risk-table-body">
                                        {% for trade in open_trades %}
                                        {% set strike = trade.get('Strike', 0)|safe_float %}
                                        {% set underlying = trade.get('_underlying_price', trade.get('Underlying Price', 0))|safe_float %}
                                        {% set contracts = trade.get('Contracts Qty', 1)|int %}
                                        {% set max_loss = strike * contracts * 100 %}
                                        {% set distance_pct = ((underlying - strike) / underlying * 100) if underlying > 0 else 0 %}
                                        <tr style="border-bottom:1px solid #334155;" data-max-loss="{{ max_loss }}">
                                            <td style="padding:12px; color:#e2e8f0; font-weight:500;">
                                                {{ trade['Symbol']|default('N/A') }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#ef4444; font-weight:600;">
                                                ${{ "{:.0f}".format(max_loss) }}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:#fbbf24;" class="risk-percent">
                                                --%
                                            </td>
                                            <td style="padding:12px; text-align:center;">
                                                {% if distance_pct > 15 %}
                                                <span style="background:#34d39933; color:#34d399; padding:4px 12px; border-radius:12px; font-size:0.85rem; font-weight:600;">LOW</span>
                                                {% elif distance_pct > 5 %}
                                                <span style="background:#fbbf2433; color:#fbbf24; padding:4px 12px; border-radius:12px; font-size:0.85rem; font-weight:600;">MODERATE</span>
                                                {% else %}
                                                <span style="background:#ef444433; color:#ef4444; padding:4px 12px; border-radius:12px; font-size:0.85rem; font-weight:600;">HIGH</span>
                                                {% endif %}
                                            </td>
                                            <td style="padding:12px; text-align:right; color:{% if distance_pct > 15 %}#34d399{% elif distance_pct > 5 %}#fbbf24{% else %}#ef4444{% endif %};">
                                                {{ distance_pct|safe_format("%.1f") }}%
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>

                        <!-- Risk Alerts -->
                        <div id="risk-alerts-container" style="display:none;">
                            <div style="background:rgba(239, 68, 68, 0.1); border:2px solid #ef4444; border-radius:16px; padding:24px; margin-bottom:24px;">
                                <h3 style="color:#ef4444; margin-bottom:16px;">🚨 Risk Alerts</h3>
                                <ul id="risk-alerts-list" style="list-style:none; padding:0;">
                                    <!-- Alerts will be added dynamically -->
                                </ul>
                            </div>
                        </div>

                        <!-- JavaScript for Risk Calculations -->
                        <script>
                            (function() {
                                const trades = {{ open_trades|tojson|safe }};
                                const totalCapital = {{ WHEEL_CAPITAL }};
                                const maxHeatPercent = 0.20; // 20% max portfolio heat

                                // Calculate total risk and metrics
                                let totalRisk = 0;
                                let maxSingleRisk = 0;
                                const risks = [];

                                trades.forEach(trade => {
                                    // Handle Strike as string (may have $ sign) or number
                                    let strikeVal = trade.Strike || trade._strike || 0;
                                    if (typeof strikeVal === 'string') {
                                        strikeVal = strikeVal.replace(/[$,]/g, '');
                                    }
                                    const strike = parseFloat(strikeVal) || 0;
                                    const contracts = parseInt(trade['Contracts Qty'] || 1);
                                    const maxLoss = strike * contracts * 100;

                                    totalRisk += maxLoss;
                                    if (maxLoss > maxSingleRisk) maxSingleRisk = maxLoss;
                                    risks.push({
                                        symbol: trade.Symbol,
                                        maxLoss: maxLoss
                                    });
                                });

                                // Calculate portfolio heat
                                const portfolioHeat = (totalRisk / totalCapital);
                                const heatPercent = (portfolioHeat * 100).toFixed(1);

                                // Update metrics
                                document.getElementById('heat-percentage').textContent = heatPercent + '%';
                                document.getElementById('total-risk').textContent = '$' + totalRisk.toLocaleString(undefined, {maximumFractionDigits: 0});
                                document.getElementById('max-position-risk').textContent = '$' + maxSingleRisk.toLocaleString(undefined, {maximumFractionDigits: 0});

                                const remainingCapacity = Math.max(0, (maxHeatPercent - portfolioHeat) * 100);
                                document.getElementById('remaining-capacity').textContent = remainingCapacity.toFixed(1) + '%';

                                // Update heat status
                                let heatStatus = '';
                                let heatColor = '';
                                if (portfolioHeat > maxHeatPercent) {
                                    heatStatus = '🚨 CRITICAL - Exceeds 20% limit';
                                    heatColor = '#ef4444';
                                } else if (portfolioHeat > 0.15) {
                                    heatStatus = '⚠️ HIGH - Approaching limit';
                                    heatColor = '#fb923c';
                                } else if (portfolioHeat > 0.10) {
                                    heatStatus = '💛 MODERATE - Good capacity remaining';
                                    heatColor = '#fbbf24';
                                } else {
                                    heatStatus = '✅ LOW - Plenty of capacity';
                                    heatColor = '#34d399';
                                }
                                const statusEl = document.getElementById('heat-status');
                                statusEl.textContent = heatStatus;
                                statusEl.style.color = heatColor;

                                // Draw heat gauge indicator
                                const clampedHeat = Math.min(portfolioHeat, 0.20); // Cap at 20%
                                const heatRatio = clampedHeat / 0.20; // 0 to 1
                                const startAngle = 180; // Left side
                                const endAngle = 180 - (heatRatio * 180); // Sweep to right

                                const startRad = startAngle * Math.PI / 180;
                                const endRad = endAngle * Math.PI / 180;

                                const cx = 200, cy = 200, radius = 140;
                                const x1 = cx + radius * Math.cos(startRad);
                                const y1 = cy - radius * Math.sin(startRad);
                                const x2 = cx + radius * Math.cos(endRad);
                                const y2 = cy - radius * Math.sin(endRad);

                                const largeArcFlag = heatRatio > 0.5 ? 1 : 0;
                                const path = `M ${x1} ${y1} A ${radius} ${radius} 0 ${largeArcFlag} 1 ${x2} ${y2}`;

                                document.getElementById('heat-indicator').setAttribute('d', path);
                                document.getElementById('heat-indicator').setAttribute('stroke', heatColor);

                                // Update risk percentages in table
                                document.querySelectorAll('#risk-table-body tr').forEach((row, idx) => {
                                    const maxLoss = parseFloat(row.dataset.maxLoss || 0);
                                    const riskPct = ((maxLoss / totalCapital) * 100).toFixed(1);
                                    row.querySelector('.risk-percent').textContent = riskPct + '%';
                                });

                                // Generate risk alerts
                                const alerts = [];
                                if (portfolioHeat > maxHeatPercent) {
                                    alerts.push('Portfolio heat exceeds 20% limit - consider closing or reducing positions');
                                }
                                if (maxSingleRisk > totalCapital * 0.05) {
                                    alerts.push('Single position risk exceeds 5% of capital - consider reducing size');
                                }
                                if (trades.length >= 10) {
                                    alerts.push('Maximum position count reached (10 positions) - cannot add new trades');
                                }

                                // Display alerts if any
                                if (alerts.length > 0) {
                                    const alertsContainer = document.getElementById('risk-alerts-container');
                                    const alertsList = document.getElementById('risk-alerts-list');
                                    alertsList.innerHTML = alerts.map(alert =>
                                        `<li style="padding:8px 0; color:#e2e8f0;">• ${alert}</li>`
                                    ).join('');
                                    alertsContainer.style.display = 'block';
                                }
                            })();
                        </script>
                    {% else %}
                        <div class="empty">No open positions to analyze.</div>
                    {% endif %}
                </div>

                <!-- Covered Calls -->
                <div id="cc" class="tab-content">
                    <h2 style="color:#60a5fa;">💸 Covered Call Suggestions</h2>
                    {% if covered_calls %}
                        <div class="grid">
                            {% for item in covered_calls %}
                                <div class="tile">
                                    <h3>{{ item.symbol }} — {{ item.shares }} shares</h3>
                                    <table>
                                        <tr><th>Strike</th><th>DTE</th><th>Bid</th><th>Income</th><th>Ann%</th></tr>
                                        {% for c in item.calls %}
                                            {% set c_strike = (c.strike|string)|float %}
                                            {% set c_bid = (c.bid|string)|float %}
                                            {% set income = (c.total_income|string)|float %}
                                            {% set ann = (c.annualized|string)|float %}
                                            <tr>
                                                <td>${{ c_strike|safe_format("%.2f") }}C</td>
                                                <td>{{ c.dte|default(0) }}</td>
                                                <td>${{ c_bid|safe_format("%.2f") }}</td>
                                                <td>${{ income|safe_format("%.0f") }}</td>
                                                <td style="color:{% if ann > 20 %}#166534{% elif ann > 15 %}#34d399{% elif ann > 10 %}#f59e0b{% else %}#fb923c{% endif %}">
                                                    {{ ann|safe_format("%.1f") }}%
                                                </td>
                                            </tr>
                                        {% endfor %}
                                    </table>
                                    {% if item.calls %}
                                        {% set top_call = item.calls[0] %}
                                        {% set prob_keep = (top_call.prob_keep|string)|float %}
                                        {% set called_price = (top_call.if_called_from_price|string)|float %}
                                        {% set called_cost = (top_call.if_called_vs_cost|string)|float %}
                                        {% set distance = (top_call.distance_pct|string)|float %}
                                        <div style="margin-top:15px;">
                                            <p>~{{ prob_keep|safe_format("%.0f") }}% chance to keep shares</p>
                                            <p>If called vs current price: 
                                                <strong style="{% if called_price > 0.01 %}color:#34d399{% elif called_price < -0.09 %}color:#fb923c{% else %}color:#fbbf24{% endif %};">
                                                    {% if called_price > 0.01 %}
                                                        +{{ called_price|safe_format("%.1f") }}%
                                                    {% elif called_price < -0.01 %}
                                                        {{ called_price|safe_format("%.1f") }}%
                                                    {% else %}
                                                        ~0.0% (breakeven)
                                                    {% endif %}
                                                </strong>
                                            </p>
                                            <p>If called vs cost basis: 
                                                <strong>+{{ called_cost|safe_format("%.1f") }}%</strong>
                                            </p>
                                            <p>Distance: {{ distance|safe_format("%.1f") }}% <strong>{{ top_call.distance_badge|default('N/A') }}</strong></p>
                                        </div>
                                    {% endif %}
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No positions with 100+ shares found for covered calls.</div>
                    {% endif %}
                    <div style="margin-top:40px;">

                        <!-- 📘 Quick Guide: How to Sell a Conservative Covered Call -->
                        <div class="accordion">
                            <div class="accordion-header" onclick="toggleAccordion(this)">
                                <h3 style="color:#34d399; margin:0;">📘 Quick Guide: How to Sell a Conservative Covered Call</h3>
                                <span style="font-size:1.5rem;">▼</span>
                            </div>
                            <div class="accordion-content">
                                <div style="padding:20px; background:#064e3b; border-radius:12px; line-height:1.8;">
                                    <ol style="color:#d1fae5; padding-left:20px;">
                                        <li><strong>Own 100+ shares</strong> of a strong stock you’re happy holding long-term.</li>
                                        <li>Wait for a <strong>green day or small pullback</strong> — never sell calls when the stock is crashing.</li>
                                        <li>Choose expiration <strong>21–60 days out</strong> (monthly or 45-day cycles work best).</li>
                                        <li>Select a strike <strong>3–8% above current price</strong> (delta 0.20–0.35 for safety).</li>
                                        <li>Aim for premium that gives <strong>1–3% monthly return</strong> (annualized 12–36%).</li>
                                        <li>Use <strong>limit orders</strong> at midpoint or better — never market orders.</li>
                                        <li>Set GTC buy-back order at <strong>50–70% profit</strong> to capture gains early.</li>
                                        <li>Avoid selling over earnings or major news events.</li>
                                        <li>If called away → great! You sold at your target price + kept premium.</li>
                                        <li>Repeat the process — wheel keeps turning!</li>
                                    </ol>
                                    <p style="margin-top:20px; font-style:italic; color:#a7f3d0;">
                                        💡 Conservative covered calls = steady income with limited downside. Perfect for buy-and-hold stocks.
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Dividends -->
                <div id="dividends" class="tab-content">
                    <h2 style="color:#60a5fa;">💰 Dividend & Total Return Tracker</h2>
                    {% if dividend_tiles %}
                        <div class="grid">
                            {% for tile in dividend_tiles %}
                                <div class="tile tile_dividend {% if tile['roc_warning'] %}has-roc{% endif %}">
                                    {% if tile['roc_warning'] %}
                                        <div class="roc_banner">
                                            ⚠️ HIGH ROC RISK
                                        </div>
                                    {% endif %}
                                    <h3>{{ tile['symbol'] }}</h3>
                                    <p class="div_info">
                                        <strong>{{ tile['qty'] }} shares</strong><br>
                                        Avg Cost: ${{ tile['avg_price']|safe_format("%.2f") }} | Cost Basis: ${{ tile['cost_basis']|safe_format("%.0f") }}
                                    </p>
                                    <p class="div_info">
                                        Value: <strong>${{ tile['market_value']|safe_format("%.0f") }}</strong><br>
                                        Unrealized P/L:
                                        <span style="{% if tile['unrealized_pl'] >= 0 %}color:#34d399{% else %}color:#fb923c{% endif %}">
                                            {% if tile['unrealized_pl'] >= 0 %}+{% endif %}${{ tile['unrealized_pl']|abs|safe_format("%.0f") }} ({{ tile['unrealized_pl_pct']|safe_format("%.1f") }}%)
                                        </span>
                                    </p>
                                    <div class="div_income">
                                        <strong>Historical Income</strong><br>
                                        Lifetime: ${{ tile['total_div']|safe_format("%.0f") }} | YTD: ${{ tile['ytd_div']|safe_format("%.0f") }}
                                    </div>
                                    <div class="div_yoc">
                                        Yield on Cost: {{ tile['yoc']|safe_format("%.2f") }}%
                                    </div>
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No dividend data available.</div>
                    {% endif %}
                </div>

                <!-- Calendar -->
                <div id="calendar" class="tab-content">
                    <h2 style="color:#60a5fa;">📅 Recent Distributions</h2>
                    {% if calendar_events %}
                        <div class="grid">
                            {% for event in calendar_events %}
                                <div class="tile" style="border-left-color:#34d399;">
                                    <h3>{{ event.date }} — Total ${{ event.total|safe_format("%.2f") }}</h3>
                                    <div style="margin-top:10px; padding-left:10px;">
                                        {% for item in event['items'] %}
                                            <p style="margin:8px 0;">
                                                <strong>{{ item['symbol'] }}</strong>:
                                                <span style="color:#34d399; font-weight:bold;">${{ item['amount']|safe_format("%.2f") }}</span>
                                            </p>
                                        {% endfor %}
                                    </div>
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No Recent distributions found.</div>
                    {% endif %}
                </div>


                <!-- Trade History -->
                <div id="history" class="tab-content">
                    <h2 style="color:#60a5fa;">📈 Trade History & Performance Analyzer</h2>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <button class="script-button" id="btn-analyze-trades" onclick="analyzeTrades()" style="background: linear-gradient(135deg, #8b5cf6, #7c3aed);">
                            🤖 Run Grok Performance Analysis
                            <div class="progress-container" id="progress-analyze-trades">
                                <div class="progress-fill" id="fill-analyze-trades">
                                    <div class="progress-glow"></div>
                                    <div class="progress-text" id="text-analyze-trades">0%</div>
                                </div>
                            </div>
                        </button>
                    </div>
                    
                    <div id="grok-trade-analysis" style="margin-top:20px;">
                        Click "Run Grok Performance Analysis" to get AI insights on your trade history.
                    </div>
                    
                    <h3 style="color:#60a5fa; margin-top:40px;">Closed Trades</h3>
                    {% if trade_history %}
                        <div class="grid">
                            {% for trade in trade_history %}
                                {# === SAFE NUMERIC PARSING === #}
                                {% set raw_strike = trade.get('Strike') or trade.get('Strike Price') or trade.get('Put Strike') or trade.get('strike') or '0' %}
                                {% set strike = raw_strike|string|replace('$','')|replace(',','')|trim|float(0) %}

                                {% set raw_entry = trade.get('Entry Premium') or trade.get('Credit Received') or '0' %}
                                {% set entry_prem = raw_entry|string|replace('$','')|replace(',','')|trim|float(0) %}

                                {% set iv_entry = trade.get('IV at Entry') or trade.get('IV Entry') or trade.get('IV@Entry') or 'N/A' %}
                                {% set rsi_entry = trade.get('RSI at Entry') or trade.get('RSI Entry') or trade.get('RSI@Entry') or 'N/A' %}

                                {% set raw_exit = trade.get('Exit Premium') or trade.get('Debit Paid') or '0' %}
                                {% set exit_prem = raw_exit|string|replace('$','')|replace(',','')|trim|float(0) %}

                                {% set raw_profit = trade.get('Net Profit $') or trade.get('P/L $') or trade.get('Profit/Loss') or '0' %}
                                {% set net_profit = raw_profit|string|replace('$','')|replace(',','')|trim|float(0) %}

                                {% set raw_roi = trade.get('ROI %') or trade.get('Return %') or '0' %}
                                {% set roi_pct = raw_roi|string|replace('%','')|replace(',','')|trim|float(0) %}

                                {% set raw_ann = trade.get('Annualized ROI%') or trade.get('Annualized Return %') or '0' %}
                                {% set ann_roi = raw_ann|string|replace('%','')|replace(',','')|trim|float(0) %}

                                <div class="tile" style="border-left-color:{% if net_profit > 0 %}#34d399{% else %}#fb923c{% endif %};">
                                    <h3>
                                        {{ trade.Symbol|default('N/A') }}
                                        {% if strike > 0.01 %}
                                            ${{ strike|safe_format("%.2f") }}P
                                        {% else %}
                                            Put
                                        {% endif %}
                                        — {{ trade.Strategy|default('Wheel') }}
                                    </h3>
                                    <p>📅 {{ trade['Entry Date']|default('N/A') }} → {{ trade['Exit Date']|default('N/A') }} ({{ trade['Days Held']|default('—') }} days)</p>
                                    <p>💰 Entry: ${{ entry_prem|safe_format("%.2f") }} → Exit: ${{ exit_prem|safe_format("%.2f") }}</p>
                                    <p style="color:{% if net_profit > 0 %}#34d399{% else %}#fb923c{% endif %}; font-weight:bold;">
                                        Net P/L: ${{ net_profit|safe_format("%.2f") }} ({{ roi_pct|safe_format("%.1f") }}% | {{ ann_roi|safe_format("%.1f") }}% ann)
                                    </p>
                                    <p>📊 Entry Conditions:
                                        IV: <strong style="color:{% if iv_entry|float(default=0) > 50 %}#34d399{% elif iv_entry|float(default=0) > 30 %}#f59e0b{% else %}#fb923c{% endif %}">
                                            {{ iv_entry }}
                                        </strong> | 
                                        RSI: <strong style="color:{% if rsi_entry|float(default=100) < 40 %}#34d399{% elif rsi_entry|float(default=100) < 60 %}#f59e0b{% else %}#fb923c{% endif %}">
                                            {{ rsi_entry }}
                                        </strong>
                                    </p>
                                    <p>Profit Captured: {{ trade['Profit Captured']|default('N/A') }}% | Exit: {{ trade['Exit Reason']|default('N/A') }}</p>
                                    <p><strong>Result:</strong> 
                                        <span style="color:{% if trade['Win/Loss']|lower in ['win', 'won', 'profit'] %}#34d399{% else %}#fb923c{% endif %}; font-weight:bold;">
                                            {{ trade['Win/Loss']|default('N/A')|upper }}
                                        </span>
                                    </p>
                                    {% if trade['Notes'] or trade['Note'] %}
                                        <div class="grok-box">
                                            <strong>Notes:</strong> {{ trade['Notes'] or trade['Note'] or '' }}
                                        </div>
                                    {% endif %}
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty">No closed trades recorded.</div>
                    {% endif %}
                    <!--- Grok DNA --->
                    <div style="margin:40px 0; padding:24px; background:linear-gradient(135deg,#064e3b,#065f46); border:2px solid #10b981; border-radius:20px;">
                        <h3 style="color:#34d399; margin-top:0;">🧬 Your Trading DNA (Personalized from History)</h3>
                        <div id="trading-dna" style="color:#e0f7ef; line-height:1.7; min-height:100px;">
                            Loading your personal winning formula...
                        </div>
                        <button onclick="loadDNA()" style="margin-top:16px; padding:10px 20px; background:#34d399; color:#064e3b; border:none; border-radius:8px; cursor:pointer;">
                            Refresh DNA
                        </button>
                    </div>
                </div>

                <!-- Grok Insights -->
                <div id="grok" class="tab-content">
                    <h2 style="color:#60a5fa; margin-bottom: 32px;">🤖 Grok AI Insights</h2>

                    <!-- Sub-Tabs -->
                    <div class="sub-tabs">
                        <button class="sub-tab active" onclick="openSubTab(event, 'quick-analysis')">
                            🚀 Quick Wheel & Sentiment
                        </button>
                        <button class="sub-tab" onclick="openSubTab(event, 'advanced-analyzer')">
                            🔬 Advanced Trade Analyzer
                        </button>
                    </div>

                    <!-- Quick Analysis Tab -->
                    <div id="quick-analysis" class="sub-tab-content" style="display:block;">
                        <div class="tile" style="padding:32px; background:linear-gradient(135deg,#1e293b,#0f172a); border:2px solid #334155; border-radius:20px;">
                            <p style="font-size:1.1rem; margin-bottom:20px; color:#cbd5e1;">
                                Quick wheel suitability & market sentiment for any ticker:
                            </p>

                            <div style="display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap;">
                                <input type="text" id="grok-symbol" placeholder="e.g., HOOD, TQQQ, SPY" 
                                    style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0; flex:1; min-width:200px; font-size:1rem;">
                                <button onclick="askGrok()" 
                                        style="padding:14px 32px; background:#3b82f6; color:white; border:none; border-radius:12px; cursor:pointer; font-weight:bold; font-size:1rem;">
                                    Ask Grok
                                </button>
                            </div>

                            <select id="grok-preset" onchange="document.getElementById('grok-symbol').value = this.value" 
                                    style="padding:12px; border-radius:12px; background:#1e293b; color:#e2e8f0; width:100%; margin-bottom:24px;">
                                <option value="">-- Quick Picks: Your Holdings & Hot Tickers --</option>
                                {% for sym in grok_symbols %}
                                    <option value="{{ sym }}">{{ sym }}</option>
                                {% endfor %}
                                <option value="">---</option>
                                <option value="SPY">SPY - Market Benchmark</option>
                                <option value="TQQQ">TQQQ - Leveraged Tech</option>
                                <option value="HOOD">HOOD - Meme/Retail</option>
                                <option value="TSLA">TSLA - Tech</option>
                                <option value="LEU">LEU - Energy Play</option>
                            </select>

                            <div id="grok-response" 
                                style="min-height:500px; overflow-y:auto;
                                        background:linear-gradient(135deg,rgba(22,101,52,0.95),rgba(6,78,59,1)); 
                                        border:2px solid #10b981; border-radius:20px; padding:28px; 
                                        color:#e0f7ef; font-size:1.1rem; line-height:1.8; box-shadow:0 12px 40px rgba(0,0,0,0.6);
                                        word-wrap:break-word; white-space:pre-wrap;">
                                <div id="grok-response-content">
                                    Enter a symbol above and click "Ask Grok" for real-time wheel analysis.
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Advanced Analyzer Tab -->
                    <div id="advanced-analyzer" class="sub-tab-content" style="display:none;">
                        <div class="tile" style="padding:32px; background:linear-gradient(135deg,#1e293b,#0f172a); border:2px solid #334155; border-radius:20px;">
                            <p style="font-size:1.1rem; margin-bottom:28px; color:#cbd5e1;">
                                Analyze a specific option trade (CSP, LEAPS, Covered Call, etc.). Grok will evaluate probability, risk/reward, breakeven, and give a clear recommendation.
                            </p>

                            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:32px;">
                                <!-- All your input fields here (unchanged) -->
                                <input type="text" id="opt-symbol" placeholder="Symbol (required)" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <select id="opt-type" style="padding:14px; border-radius:12px; background:#0f172a; color:#e2e8f0;">
                                    <option value="Put">Put</option><option value="Call">Call</option>
                                </select>
                                <select id="opt-direction" style="padding:14px; border-radius:12px; background:#0f172a; color:#e2e8f0;">
                                    <option value="Sell">Sell</option><option value="Buy">Buy</option>
                                </select>
                                <select id="opt-strategy" style="padding:14px; border-radius:12px; background:#0f172a; color:#e2e8f0;">
                                    <option value="">General Analysis</option>
                                    <option value="CSP">Cash-Secured Put (Wheel)</option>
                                    <option value="LEAPS">LEAPS (Long Call)</option>
                                    <option value="CC">Covered Call</option>
                                    <option value="PMCC">Poor Man's Covered Call</option>
                                </select>
                                <input type="number" id="opt-strike" placeholder="Strike (required)" step="0.5" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-premium" placeholder="Premium (required)" step="0.01" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-dte" placeholder="DTE (required)" step="1" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-delta" placeholder="Delta (optional)" step="0.01" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-theta" placeholder="Theta (optional)" step="0.01" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-vega" placeholder="Vega (optional)" step="0.01" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                                <input type="number" id="opt-iv" placeholder="IV % (optional)" step="0.1" style="padding:14px; border-radius:12px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;">
                            </div>

                            <div style="display:flex; justify-content:center; margin-bottom:36px;">
                                <button onclick="analyzeOptionTrade()" 
                                        style="padding:16px 40px; background:#10b981; color:white; border:none; border-radius:12px; cursor:pointer; font-weight:bold; font-size:1.2rem; box-shadow:0 8px 20px rgba(16,185,129,0.3);">
                                    🔍 Analyze Trade with Grok
                                </button>
                            </div>

                            <div id="opt-response" 
                                style="min-height:600px; overflow-y:auto;
                                        background:linear-gradient(135deg,rgba(22,101,52,0.95),rgba(6,78,59,1)); 
                                        border:2px solid #10b981; border-radius:20px; padding:32px; 
                                        color:#e0f7ef; font-size:1.1rem; line-height:1.9; box-shadow:0 12px 40px rgba(0,0,0,0.6);
                                        word-wrap:break-word; white-space:pre-wrap;">
                                Fill in the fields above and click "Analyze Trade with Grok".
                            </div>
                        </div>
                    </div>
                </div>

                <script>
                function openSubTab(evt, tabName) {
                    // Hide all content
                    document.querySelectorAll('.sub-tab-content').forEach(content => {
                        content.style.display = 'none';
                    });

                    // Remove active from all buttons
                    document.querySelectorAll('.sub-tab').forEach(btn => {
                        btn.classList.remove('active');
                    });

                    // Show selected
                    document.getElementById(tabName).style.display = 'block';
                    evt.currentTarget.classList.add('active');
                }
                </script>

                <!-- Sub-Tab CSS -->
                <style>
                .sub-tabs {
                    display: flex;
                    gap: 12px;
                    margin-bottom: 32px;
                    flex-wrap: wrap;
                }

                .sub-tab {
                    padding: 14px 28px;
                    background: #1e293b;
                    color: #94a3b8;
                    border: none;
                    border-radius: 16px;
                    cursor: pointer;
                    font-size: 1.1rem;
                    font-weight: bold;
                    transition: all 0.3s;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                }

                .sub-tab:hover {
                    background: #263344;
                    color: #e2e8f0;
                }

                .sub-tab.active {
                    background: linear-gradient(135deg, #3b82f6, #60a5fa);
                    color: white;
                    box-shadow: 0 8px 20px rgba(59,130,246,0.4);
                }
                </style>

                <script>
                async function updateGrokInsight(symbol, strike, exp, button) {
                    button.disabled = true;
                    button.textContent = 'Updating...';

                    // Navigate from button -> parent (action-buttons-csp) -> next sibling (grok-insight) -> find grok-oneliner
                    const grokInsightDiv = button.parentElement.nextElementSibling;
                    const grokOnelinerDiv = grokInsightDiv ? grokInsightDiv.querySelector('.grok-oneliner') : null;

                    if (!grokOnelinerDiv) {
                        console.error('Could not find grok-oneliner div');
                        button.disabled = false;
                        button.textContent = '🔄 Update Insight';
                        return;
                    }

                    const originalText = grokOnelinerDiv.textContent;  // Backup in case of failure

                    grokOnelinerDiv.innerHTML = '<em style="color:#9ca3af;">Fetching updated insight...</em>';
                    
                    try {
                        const resp = await fetch(`/grok/update_csp?symbol=${encodeURIComponent(symbol)}&strike=${encodeURIComponent(strike)}&exp=${encodeURIComponent(exp)}`);
                        if (!resp.ok) {
                            throw new Error(`HTTP error ${resp.status}`);
                        }
                        const data = await resp.json();
                        
                        if (data.error) {
                            grokOnelinerDiv.innerHTML = `<span style="color:#fb923c;">Error: ${data.error}</span>`;
                        } else {
                            grokOnelinerDiv.textContent = data.analysis;
                            // Optional: Add a subtle highlight for the update
                            grokOnelinerDiv.style.transition = 'background 0.3s';
                            grokOnelinerDiv.style.background = 'rgba(16,185,129,0.15)';
                            setTimeout(() => { grokOnelinerDiv.style.background = 'transparent'; }, 2000);
                        }
                    } catch (e) {
                        grokOnelinerDiv.textContent = originalText;
                        alert(`Update failed: ${e.message || 'Check console'}`);
                        console.error(e);
                    } finally {
                        button.disabled = false;
                        button.textContent = '🔄 Update Insight';
                    }
                }

                async function closeTrade(symbol, strike, expDate) {
                    if (!confirm(`Mark ${symbol} ${strike}P exp ${expDate} as CLOSED?\n\nThis will move it to Trade History with Exit Date = today.\nYou can fill P/L and notes later.`)) {
                        return;
                    }

                    try {
                        const resp = await fetch('/csp/mark_closed', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                symbol: symbol,
                                strike: strike,
                                exp_date: expDate
                            })
                        });

                        const data = await resp.json();
                        alert(data.status || "Trade marked as closed!");
                        // Optional: refresh page after delay
                        setTimeout(() => location.reload(), 1500);
                    } catch (e) {
                        alert("Failed to mark trade closed — check server");
                        console.error(e);
                    }
                }

                async function sellToOpen(symbol, strike, expiration, contracts, premium, button) {
                    // Confirm with user
                    if (!confirm(`Sell To Open\n\n${contracts}x ${symbol} $${strike}P exp ${expiration}\nLimit Price: $${premium}\n\nThis will place a LIVE order to Schwab. Continue?`)) {
                        return;
                    }

                    button.disabled = true;
                    button.textContent = 'Placing Order...';

                    try {
                        const response = await fetch('/api/schwab/sell_to_open', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                symbol: symbol,
                                strike: strike,
                                expiration: expiration,
                                contracts: contracts,
                                limit_price: premium,
                                dry_run: false
                            })
                        });

                        const result = await response.json();

                        if (result.success) {
                            alert(`✅ Order Placed!\n\nOrder ID: ${result.order_id}\n${result.message}`);
                            button.textContent = '✅ Order Placed';
                            button.style.background = 'linear-gradient(135deg, #34d399, #10b981)';
                        } else {
                            alert(`❌ Order Failed\n\n${result.message}`);
                            button.textContent = '📈 Sell To Open';
                            button.disabled = false;
                        }
                    } catch (error) {
                        alert(`❌ Error placing order\n\n${error.message}`);
                        button.textContent = '📈 Sell To Open';
                        button.disabled = false;
                    }
                }

                async function buyToClose(symbol, strike, expiration, contracts, limit_price, button) {
                    // Confirm with user
                    if (!confirm(`Buy To Close\n\n${contracts}x ${symbol} $${strike}P exp ${expiration}\nLimit Price: $${limit_price}\n\nThis will place a LIVE order to Schwab. Continue?`)) {
                        return;
                    }

                    button.disabled = true;
                    button.textContent = 'Placing Order...';

                    try {
                        const response = await fetch('/api/schwab/buy_to_close', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                symbol: symbol,
                                strike: strike,
                                expiration: expiration,
                                contracts: contracts,
                                limit_price: limit_price,
                                dry_run: false
                            })
                        });

                        const result = await response.json();

                        if (result.success) {
                            alert(`✅ Order Placed!\n\nOrder ID: ${result.order_id}\n${result.message}`);
                            button.textContent = '✅ Closed';
                            button.style.background = 'linear-gradient(135deg, #34d399, #10b981)';
                            setTimeout(() => location.reload(), 2000);  // Refresh after 2 seconds
                        } else {
                            alert(`❌ Order Failed\n\n${result.message}`);
                            button.textContent = '💰 Buy To Close';
                            button.disabled = false;
                        }
                    } catch (error) {
                        alert(`❌ Error placing order\n\n${error.message}`);
                        button.textContent = '💰 Buy To Close';
                        button.disabled = false;
                    }
                }

                async function quickAction(action, symbol) {
                    const resp = await fetch(`/csp/${action}`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({symbol: symbol})
                    });
                    const data = await resp.json();
                    if (action === 'suggest_roll') {
                        alert(`Grok Roll Suggestion for ${symbol}:\n\n${data.suggestion}`);
                    } else {
                        alert(data.status || "Action triggered");
                    }
                }

                async function loadDNA() {
                    const div = document.getElementById('trading-dna');
                    if (!div) return;

                    div.innerHTML = "🔍 Analyzing your trade history with Grok...";

                    try {
                        const resp = await fetch('/grok/dna');
                        const data = await resp.json();

                        let clean = data.dna
                            .replace(/Your Personal Trading DNA/gi, '')  // Remove duplicates
                            .replace(/🧬/g, '')  // Remove emoji duplicates
                            .trim();

                        if (clean.startsWith('<br>') || clean.startsWith('\n')) {
                            clean = clean.replace(/^[\n<br>]+/, '');
                        }

                        div.innerHTML = `
                            <strong style="color:#34d399; font-size:1.4rem;">🧬 Your Personal Trading DNA</strong>
                            <div style="margin-top:16px; line-height:1.8; color:#e0f7ef; font-size:1.05rem;">
                                ${clean.replace(/\n/g, '<br>')}
                            </div>
                        `;
                    } catch (e) {
                        div.innerHTML = "<strong style='color:#fb923c;'>Failed to load DNA</strong>";
                    }
                }

                // Load on page load
                loadDNA();

                function openTab(tabId) {
                    // Hide all tab contents
                    document.querySelectorAll('.tab-content').forEach(tab => {
                        tab.classList.remove('active');
                        tab.style.display = 'none';
                    });
                    // Remove active from all buttons
                    document.querySelectorAll('.tab-btn').forEach(btn => {
                        btn.classList.remove('active');
                    });
                    // Show selected tab
                    const targetTab = document.getElementById(tabId);
                    if (targetTab) {
                        targetTab.classList.add('active');
                        targetTab.style.display = 'block';
                    }
                    // Highlight button
                    const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
                    if (activeBtn) activeBtn.classList.add('active');
                }

                // On page load: show first tab
                document.addEventListener('DOMContentLoaded', () => {
                    const firstTabBtn = document.querySelector('.tab-btn');
                    if (firstTabBtn) {
                        const firstTabId = firstTabBtn.getAttribute('data-tab');
                        openTab(firstTabId);
                    }
                });
                async function runScript(type) {
                    const taskMap = {
                        'scanner': '/run/scanner',
                        'open_trades_refresh': '/run/open_trades_refresh',
                        'covered_calls': '/run/covered_calls',
                        'dividends': '/run/dividends',
                        'leaps': '/run/leaps'
                    };
                    const url = taskMap[type];
                    if (!url) {
                        document.getElementById('status').textContent = 'Unknown script!';
                        return;
                    }

                    const containerId = `progress-${type}`;
                    const fillId = `fill-${type}`;
                    const textId = `text-${type}`;

                    // Reset and show progress
                    const container = document.getElementById(containerId);
                    const fill = document.getElementById(fillId);
                    const text = document.getElementById(textId);
                    if (!container || !fill || !text) return;

                    fill.style.width = '0%';
                    text.textContent = 'Starting...';
                    container.classList.add('visible');

                    try {
                        const resp = await fetch(url);
                        const data = await resp.json();
                        if (data.task_id) {
                            pollProgress(data.task_id, fillId, textId, containerId, type);
                        } else {
                            simulateProgress(fillId, textId, containerId);
                        }
                    } catch (e) {
                        text.textContent = 'Failed ❌';
                        console.error(e);
                    }
                }

                function sortCSPTiles() {
                    const select = document.getElementById('csp-sort');
                    if (!select) return;
                    const sortBy = select.value;
                    const container = document.getElementById('open-csp-grid'); 
                    if (!container) return;

                    // Get all individual trade tiles (exclude summary tile)
                    const tiles = Array.from(container.querySelectorAll('.tile')).filter(tile => 
                        !tile.classList.contains('summary-tile')
                    );

                    tiles.sort((a, b) => {
                        let aVal = 0, bVal = 0;

                        switch (sortBy) {
                            case 'score-desc':
                                aVal = parseFloat(a.dataset.score) || 0;
                                bVal = parseFloat(b.dataset.score) || 0;
                                return bVal - aVal;
                            case 'score-asc':
                                aVal = parseFloat(a.dataset.score) || 0;
                                bVal = parseFloat(b.dataset.score) || 0;
                                return aVal - bVal;
                            case 'progress-desc':
                                aVal = parseFloat(a.dataset.progress) || 0;
                                bVal = parseFloat(b.dataset.progress) || 0;
                                return bVal - aVal;
                            case 'progress-asc':
                                aVal = parseFloat(a.dataset.progress) || 0;
                                bVal = parseFloat(b.dataset.progress) || 0;
                                return aVal - bVal;
                            case 'dte-asc':
                                aVal = parseInt(a.dataset.dte) || 9999;
                                bVal = parseInt(b.dataset.dte) || 9999;
                                return aVal - bVal;
                            case 'dte-desc':
                                aVal = parseInt(a.dataset.dte) || 0;
                                bVal = parseInt(b.dataset.dte) || 0;
                                return bVal - aVal;
                            case 'daily-theta-desc':
                                aVal = parseFloat(a.dataset.dailyTheta) || 0;
                                bVal = parseFloat(b.dataset.dailyTheta) || 0;
                                return bVal - aVal;
                            case 'forward-theta-desc':
                                aVal = parseFloat(a.dataset.forwardTheta) || 0;
                                bVal = parseFloat(b.dataset.forwardTheta) || 0;
                                return bVal - aVal;
                            case 'pl-desc':
                                aVal = parseFloat(a.dataset.pl) || 0;
                                bVal = parseFloat(b.dataset.pl) || 0;
                                return bVal - aVal;
                            case 'pl-asc':
                                aVal = parseFloat(a.dataset.pl) || 0;
                                bVal = parseFloat(b.dataset.pl) || 0;
                                return aVal - bVal;
                            default:
                                return 0; // Keep original order
                        }
                    });

                    // Clear container and re-append: summary first, then sorted tiles
                    const summary = container.querySelector('.summary-tile');
                    container.innerHTML = '';
                    if (summary) container.appendChild(summary);
                    tiles.forEach(tile => container.appendChild(tile));
                }

                async function askGrok() {
                    const input = document.getElementById('grok-symbol');
                    const symbol = input.value.trim().toUpperCase();
                    if (!symbol) return;

                    const responseDiv = document.getElementById('grok-response');
                    responseDiv.innerHTML = `<strong style="color:#9ff2d6;">🔍 Analyzing ${symbol}...</strong>`;

                    try {
                        const resp = await fetch(`/grok/analyze/${symbol}`);
                        const data = await resp.json();

                        if (data.error) {
                            responseDiv.innerHTML = `<strong style="color:#fb923c;">Error:</strong> ${data.error}`;
                        } else {
                            let analysis = data.analysis
                                .replace(/\*\*(.*?)\*\*/g, '<strong style="color:#9ff2d6;">$1</strong>')
                                .replace(/\n/g, '<br>');

                            responseDiv.innerHTML = `
                                <strong style="color:#34d399; font-size:1.4rem;">🤖 Grok Analysis for ${symbol}:</strong><br><br>
                                <div style="line-height:1.8;">${analysis}</div>
                            `;
                        }
                    } catch (e) {
                        responseDiv.innerHTML = `<strong style="color:#fb923c;">Connection failed</strong>`;
                    }
                }

                async function analyzeOptionTrade() {
                    const symbol = document.getElementById('opt-symbol').value.trim().toUpperCase();
                    const type = document.getElementById('opt-type').value;
                    const direction = document.getElementById('opt-direction').value;
                    const strategy = document.getElementById('opt-strategy').value;
                    const strike = document.getElementById('opt-strike').value;
                    const premium = document.getElementById('opt-premium').value;
                    const dte = document.getElementById('opt-dte').value;

                    if (!symbol || !strike || !premium || !dte) {
                        alert("Please fill at least: Symbol, Strike, Premium, and DTE");
                        return;
                    }

                    const responseDiv = document.getElementById('opt-response');
                    responseDiv.innerHTML = `<strong style="color:#9ff2d6;">🔍 Analyzing ${direction} ${type} on ${symbol}...</strong>`;

                    const payload = {
                        symbol, type, direction, strategy,
                        strike: parseFloat(strike),
                        premium: parseFloat(premium),
                        dte: parseInt(dte),
                        delta: document.getElementById('opt-delta').value ? parseFloat(document.getElementById('opt-delta').value) : null,
                        theta: document.getElementById('opt-theta').value ? parseFloat(document.getElementById('opt-theta').value) : null,
                        vega: document.getElementById('opt-vega').value ? parseFloat(document.getElementById('opt-vega').value) : null,
                        iv: document.getElementById('opt-iv').value ? parseFloat(document.getElementById('opt-iv').value) : null
                    };

                    try {
                        const resp = await fetch('/grok/analyze_option', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        const data = await resp.json();

                        if (data.error) {
                            responseDiv.innerHTML = `<strong style="color:#fb923c;">Error:</strong> ${data.error}`;
                        } else {
                            let analysis = data.analysis
                                .replace(/\*\*(.*?)\*\*/g, '<strong style="color:#9ff2d6;">$1</strong>')
                                .replace(/\n/g, '<br>');

                            responseDiv.innerHTML = `
                                <strong style="color:#34d399; font-size:1.4rem;">🤖 Grok Option Trade Analysis</strong><br><br>
                                <div style="line-height:1.8;">${analysis}</div>
                            `;
                        }
                    } catch (e) {
                        responseDiv.innerHTML = `<strong style="color:#fb923c;">Connection failed — check server console</strong>`;
                        console.error(e);
                    }
                }

                function pollProgress(taskId, fillId, textId, containerId, type) {
                    const interval = setInterval(async () => {
                        try {
                            const resp = await fetch(`/progress/${taskId}`);
                            const data = await resp.json();
                            const progress = data.progress || 0;
                            document.getElementById(fillId).style.width = progress + '%';
                            document.getElementById(textId).textContent = progress >= 100 ? 'Complete! ✔️' : `${progress}%`;

                            if (data.status === 'complete' || progress >= 100) {
                                clearInterval(interval);
                                setTimeout(() => hideProgress(containerId), 1500);
                                if (type === 'open_trades_refresh') {
                                    setTimeout(() => location.reload(), 2000);
                                }
                            }
                        } catch (e) {
                            clearInterval(interval);
                            document.getElementById(textId).textContent = 'Error';
                        }
                    }, 1000);
                }

                function simulateProgress(fillId, textId, containerId) {
                    let pct = 0;
                    const interval = setInterval(() => {
                        pct += Math.random() * 15 + 5;
                        if (pct > 100) pct = 100;
                        document.getElementById(fillId).style.width = pct + '%';
                        document.getElementById(textId).textContent = pct >= 100 ? 'Complete! ✔️' : `${Math.floor(pct)}%`;
                        if (pct >= 100) {
                            clearInterval(interval);
                            setTimeout(() => hideProgress(containerId), 1500);
                        }
                    }, 800);
                }

                function hideProgress(containerId) {
                    const container = document.getElementById(containerId);
                    container.style.opacity = '0';
                    setTimeout(() => {
                        container.classList.remove('visible');
                        container.style.opacity = '1';
                    }, 600);
                }

                function toggleAccordion(header) {
                    const content = header.nextElementSibling;
                    const arrow = header.querySelector('span');
                    if (content.classList.contains('open')) {
                        content.classList.remove('open');
                        arrow.textContent = '▼';
                    } else {
                        document.querySelectorAll('.accordion-content.open').forEach(c => {
                            c.classList.remove('open');
                            c.previousElementSibling.querySelector('span').textContent = '▼';
                        });
                        content.classList.add('open');
                        arrow.textContent = '▲';
                    }
                }

                function calculateWhatIfCC() {
                    const shares = parseFloat(document.getElementById('cc-shares').value) || 100;
                    const basis = parseFloat(document.getElementById('cc-basis').value) || 0;
                    const strike = parseFloat(document.getElementById('cc-strike').value) || 0;
                    const premium = parseFloat(document.getElementById('cc-premium').value) || 0;

                    if (!basis || !strike || !premium) {
                        alert("Please fill in all fields");
                        return;
                    }

                    const totalBasis = basis * shares;
                    const totalPremium = premium * shares / 100;  // per share to total
                    const netBasis = totalBasis - totalPremium;

                    const ifCalledProfit = (strike * shares) + totalPremium - totalBasis;
                    const ifCalledReturn = (ifCalledProfit / netBasis) * 100;

                    const ifNotCalledProfit = totalPremium;
                    const ifNotCalledReturn = (ifNotCalledProfit / netBasis) * 100;

                    const breakeven = basis - premium;
                    const maxProfit = ifCalledProfit;

                    document.getElementById('cc-results').style.display = 'block';
                    document.getElementById('cc-if-called').innerHTML = `<strong>If Called Away:</strong> Profit $${ifCalledProfit.toFixed(2)} (${ifCalledReturn.toFixed(1)}% return)`;
                    document.getElementById('cc-if-not-called').innerHTML = `<strong>If Not Called:</strong> Keep premium: $${ifNotCalledProfit.toFixed(2)} (${ifNotCalledReturn.toFixed(1)}% return) + still own shares`;
                    document.getElementById('cc-breakeven').innerHTML = `<strong>Downside Breakeven:</strong> $${breakeven.toFixed(2)} (stock can drop this much before loss)`;
                    document.getElementById('cc-max-profit').innerHTML = `<strong>Max Profit:</strong> $${maxProfit.toFixed(2)} if called at strike`;
                }

                async function analyzeSymbol(symbol) {
                    const container = document.getElementById('grok-symbol-analysis');
                    container.innerHTML = `<strong>🔍 Analyzing ${symbol}...</strong>`;
                    container.style.display = 'block';
                    try {
                        const resp = await fetch(`/grok/analyze/${symbol}`);
                        const data = await resp.json();
                        if (data.error) {
                            container.innerHTML = `<strong style="color:#fb923c;">Error:</strong> ${data.error}`;
                        } else {
                            container.innerHTML = `
                                <strong style="color:#34d399;">🤖 Grok Analysis for ${symbol}:</strong><br><br>
                                ${data.analysis.replace(/\n/g, '<br>')}
                            `;
                        }
                    } catch (e) {
                        container.innerHTML = `<strong style="color:#fb923c;">Failed — check server</strong>`;
                    }
                }

                async function analyzeTrades() {
                    const responseDiv = document.getElementById('grok-trade-analysis');
                    const progressContainer = document.getElementById('progress-analyze-trades');
                    const progressFill = document.getElementById('fill-analyze-trades');
                    const progressText = document.getElementById('text-analyze-trades');

                    responseDiv.innerHTML = "<strong>🔍 Analyzing your trade history with Grok...</strong>";

                    if (progressContainer) {
                        progressContainer.classList.add('visible');
                        progressFill.style.width = '30%';
                        progressText.textContent = '30%';
                    }

                    try {
                        const resp = await fetch('/grok/trade_analysis', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({})
                        });

                        if (progressFill) progressFill.style.width = '70%';
                        if (progressText) progressText.textContent = '70%';

                        const data = await resp.json();

                        if (data.error) {
                            responseDiv.innerHTML = `<strong style="color:#fb923c;">Error:</strong> ${data.error}`;
                        } else {
                            let raw = data.analysis.trim();

                            // Clean up Grok artifacts
                            raw = raw
                                .replace(/\\+/g, '')                    // Remove backslashes
                                .replace(/\*\*(.*?)\*\*/g, '<strong style="color:#9ff2d6;">$1</strong>')
                                .replace(/### (.*)/g, '<h3 style="color:#34d399; margin:32px 0 16px 0; font-size:1.6rem; border-bottom:2px solid #10b981; padding-bottom:8px;">$1</h3>')
                                .replace(/## (.*)/g, '<h4 style="color:#60a5fa; margin:24px 0 12px 0; font-size:1.4rem;">$1</h4>')
                                .replace(/\n{3,}/g, '\n\n');             // Collapse extra newlines

                            // Split into sections and handle tables intelligently
                            let formatted = '';
                            const sections = raw.split('\n\n');

                            for (let section of sections) {
                                section = section.trim();
                                if (!section) continue;

                                // Detect if section is a table (has multiple lines with consistent structure)
                                const lines = section.split('\n');
                                const hasPipes = lines.some(l => l.includes('|') && l.split('|').length >= 3);
                                const hasDashes = lines.some(l => l.includes('---'));

                                if (hasPipes && hasDashes && lines.length >= 3) {
                                    // It's a table — render as HTML table
                                    formatted += '<table style="width:100%; margin:24px 0; background:rgba(6,78,59,0.6); border-radius:12px; overflow:hidden; box-shadow:0 6px 20px rgba(0,0,0,0.4); border-collapse:collapse;">';
                                    for (let i = 0; i < lines.length; i++) {
                                        let line = lines[i].trim();
                                        if (line.includes('---')) continue; // skip separator
                                        if (!line) continue;

                                        const cells = line.split('|').map(c => c.trim()).filter(c => c);
                                        const isHeader = i === 0 || cells.some(c => c.toLowerCase().includes('category') || c.toLowerCase().includes('win rate'));
                                        const rowTag = isHeader ? 'th' : 'td';
                                        const rowStyle = isHeader ? 'background:linear-gradient(135deg,#065f46,#064e3b); color:#34d399; font-weight:bold;' : '';

                                        formatted += `<tr style="${rowStyle}">`;
                                        for (let cell of cells) {
                                            formatted += `<${rowTag} style="padding:14px; text-align:center; border-bottom:1px solid #334155;">${cell}</${rowTag}>`;
                                        }
                                        formatted += '</tr>';
                                    }
                                    formatted += '</table>';
                                } else {
                                    // Regular text paragraph
                                    formatted += `<p style="margin:20px 0; line-height:1.8; color:#e0f7ef;">${section.replace(/\n/g, '<br>')}</p>`;
                                }
                            }

                            responseDiv.innerHTML = `
                                <strong style="color:#34d399; font-size:1.8rem; display:block; margin-bottom:24px;">🤖 Grok Trade Performance Analysis</strong>
                                <div style="font-size:1.05rem;">
                                    ${formatted}
                                </div>
                            `;
                        }

                        if (progressFill) progressFill.style.width = '100%';
                        if (progressText) progressText.textContent = 'Complete! ✔️';
                        setTimeout(() => {
                            if (progressContainer) progressContainer.classList.remove('visible');
                        }, 2000);

                    } catch (e) {
                        responseDiv.innerHTML = `<strong style="color:#fb923c;">Connection failed — check server</strong>`;
                        console.error(e);
                    }
                }

                window.addEventListener('load', () => {
                    const grid = document.getElementById('open-csp-grid');
                    if (grid) {
                        grid.style.display = 'none';
                        grid.offsetHeight; // Trigger reflow
                        grid.style.display = 'grid';
                    }
                });

                async function refreshDashboard() {
                    if (!confirm("Refresh the entire dashboard?\nThis will re-run all bots and regenerate the page.")) {
                        return;
                    }

                    const btn = event.target;
                    btn.disabled = true;
                    btn.innerHTML = "Refreshing...";

                    try {
                        const resp = await fetch('/refresh_dashboard', {method: 'POST'});
                        const data = await resp.json();
                        alert(data.status || "Refresh triggered!");

                        // Auto-reload after delay
                        setTimeout(() => {
                            location.reload();
                        }, 5000);
                    } catch (e) {
                        alert("Refresh failed — check server");
                        console.error(e);
                    } finally {
                        setTimeout(() => {
                            btn.disabled = false;
                            btn.innerHTML = "🔄 Refresh Dashboard";
                        }, 10000);
                    }
                }

            </script>
        </body>
        </html>
    """

    template = env.from_string(template_str)
    now = datetime.now(ET_TZ).strftime('%b %d, %Y %I:%M %p')

    # Convert date objects to strings for JSON serialization
    def sanitize_for_json(data):
        """Convert date/datetime objects to strings recursively"""
        if isinstance(data, list):
            return [sanitize_for_json(item) for item in data]
        elif isinstance(data, dict):
            return {k: sanitize_for_json(v) for k, v in data.items()}
        elif isinstance(data, (date, datetime)):
            return data.isoformat() if hasattr(data, 'isoformat') else str(data)
        return data

    # Sanitize open_trades for JSON serialization in JavaScript
    open_trades_json = sanitize_for_json(open_trades)

    html_content = template.render(
        now=now,
        grok_sentiment=grok_sentiment,
        grok_summary=grok_summary,
        open_trades=open_trades_json,
        covered_calls=covered_calls,
        simple_scanner_opps=simple_scanner_opps,
        dividend_tiles=dividend_tiles,
        portfolio_summary=portfolio_summary,
        calendar_events=calendar_events,
        grok_symbols=grok_symbols,
        trade_history=trade_history,
        allocation_data=allocation_data,
        current_wheel_capital=total_wheel_capital,
        WHEEL_CAPITAL=total_wheel_capital,  # For Risk Dashboard JS
        leaps_opps=leaps_opps,
        get_company_name=get_company_name,
        captured_leaps=captured_leaps,
        zero_dte_opps=zero_dte_opps,
        PROFIT_TARGET_PCT=0.60
    )

    with open(DASHBOARD_FILE, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"✅ Ultimate dashboard generated: {os.path.abspath(DASHBOARD_FILE)}")

if __name__ == "__main__":
    asyncio.run(run_all_bots())
    dividend_tracker_bot.send_alert = original_div_send
    simple_options_scanner.send_alert = original_scanner_send
    leaps_scanner.send_alert = original_leaps_send
    generate_html()