# simple_options_scanner.py — Grok + Regime Aware Wheel Scanner (Dec 2025)
# No Massive/Polygon — Schwab API + yfinance only

import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning, module="trendln")
warnings.filterwarnings("ignore", category=FutureWarning)

import sys
import os
import json
import logging
import dotenv
import asyncio
import csv
import requests
from tqdm.asyncio import tqdm_asyncio
from tqdm import tqdm
from pathlib import Path
import io
import numpy as np

# ==================== WINDOWS FIX ====================
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

dotenv.load_dotenv()
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schwab.client import Client
from schwab import auth
import pandas as pd
import yfinance as yf
import trendln
from datetime import datetime, timedelta
import pytz
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from telegram import Bot as telegram_bot
from grok_utils import get_grok_opportunity_analysis, get_grok_sentiment_cached, get_grok_analysis, parse_grok_batch_response
from helper_functions import save_cached_scanner, load_sr_cache, save_sr_cache
from schwab_utils import get_client

# ==================== SETUP ====================
API_KEY = os.getenv('SCHWAB_API_KEY')
APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://127.0.0.1')
XAI_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

# ==================== CACHE DIR and FILES ====================

CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)

TOKEN_PATH = CACHE_DIR / 'schwab_token.json'
S_R_CACHE_FILE = CACHE_DIR / Path("support_resistance_cache.json")
GROK_CACHE_FILE = CACHE_DIR / 'grok_sentiment_cache.json'

MAX_CAPITAL_PER_TRADE = float(os.getenv('MAX_CAPITAL_PER_TRADE', 45000))

# Schwab client
c = get_client()
c.set_enforce_enums(False)

ET_TZ = pytz.timezone('US/Eastern')

caputured_opportunities = []

# ==================== SYMBOL TIERS ====================
TIER_1_SYMBOLS = [
    'KO', 'JNJ', 'PG', 'WMT', 'VZ', 'MRK', 'BMY', 'ABBV', 'PEP',
    'XOM', 'CVX', 'O', 'HD', 'LOW', 'TGT', 'COST', 
    'NKE', 'DIS', 'CAT', 'DE', 'UNH', 'SPY', 'QQQ', 'IWM', 'DIA',
    'AAPL', 'MSFT', 'NVDA', 'AMD', 'META', 'AMZN', 'GOOGL', 'NFLX',
    'MMM', 'T', 'BLK', 'C', 'CSCO', 'GE', 'IBM', 'JCI', 'LMT', 'LEU', 'CCJ',
    'MCD', 'RTX', 'USB', 'WFC', 'PNC', 'TFC', 'CEG', 'BWXT', 'NLR', 'VOO', 'SCHD'
]

TIER_2_SYMBOLS = [
    'ADBE', 'CRM', 'ORCL', 'QCOM', 'LRCX', 'MU', 'ASML', 'KLAC', 'MRVL',
    'SNPS', 'CDNS', 'PANW', 'SHOP', 'AVGO', 'TSM', 'JPM', 'BAC', 'GS', 
    'MS', 'V', 'MA', 'AXP', 'PYPL', 'PFE', 'LLY', 'NET', 'DDOG', 'MDB', 
    'ZM', 'CMG', 'SBUX', 'UPS', 'FDX', 'MCK', 'INTC', 'TXN', 'BABA', 
    'CRWD', 'PLTR', 'ROKU', 'SNAP', 'ZS', 'DASH', 'XLE', 'OXY', 
    'HAL', 'SLB', 'FCX', 'CLF', 'APA', 'BP', 'CFG', 'FITB', 
    'KEY', 'RF', 'ZION', 'TQQQ', 'SOXL', 'TDAQ', 'NBIS', 
    'HIMS', 'OKLO', 'SMR', 'URA', 'GTLB', 'BMNR'
]

TIER_3_SYMBOLS = [
    'COIN', 'HOOD', 'SOFI', 'RBLX', 'ABNB', 'UBER', 'DKNG', 'PINS', 'F',
    'NEM', 'VALE', 'DVN', 'COP', 'EOG', 'EXPE', 'NCLH', 'CCL', 'ENPH', 
    'FSLR', 'BNTX', 'TSLA', 'MARA', 'RIOT', 'RIVN', 'IREN', 'CCCX',
    'GM', 'LUV', 'MAR', 'RCL'
]

SIMPLE_WATCHLIST = TIER_1_SYMBOLS + TIER_2_SYMBOLS + TIER_3_SYMBOLS

# ==================== REGIME SETTINGS ====================
REGIME_SETTINGS = {
    "STRONG_BULL": {
        "delta_min": 0.15, "delta_max": 0.40,  # Loosened max
        "dte_min": 10, "dte_max": 40,
        "target_profit_pct": 50,
        "tier_limit": None,
        "name": "Aggressive Bull Wheel",
        "iv_min": 30
    },
    "MILD_BULL": {
        "delta_min": 0.15, "delta_max": 0.37,
        "dte_min": 14, "dte_max": 45,
        "target_profit_pct": 50,
        "tier_limit": None,
        "name": "Classic Wheel",
        "iv_min": 35
    },
    "NEUTRAL_OR_WEAK": {
        "delta_min": 0.15, "delta_max": 0.35,
        "dte_min": 21, "dte_max": 60,
        "target_profit_pct": 50,
        "tier_limit": "TIER_1_2",
        "name": "Balanced Wheel",
        "iv_min": 40
    },
    "CAUTIOUS": {
        "delta_min": 0.10, "delta_max": 0.30,
        "dte_min": 30, "dte_max": 60,
        "target_profit_pct": 50,
        "tier_limit": "TIER_1",
        "name": "Defensive Wheel",
        "iv_min": 50
    },
    "BEARISH_HIGH_VOL": {
        "delta_min": 0.10, "delta_max": 0.30,
        "dte_min": 45, "dte_max": 60,
        "target_profit_pct": 50,
        "tier_limit": "TIER_1",
        "name": "Ultra-Defensive Wheel",
        "iv_min": 50
    }
}

# Loosened global filters
MIN_PREMIUM = 0.20  # Was higher
MIN_ANNUALIZED = 12  # Lower threshold
OTM_BUFFER_PCT = 0.95

# ==================== TELEGRAM ====================
SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN = os.getenv('SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN')
SIMPLE_OPTIONS_SCANNER_CHAT_ID = 7972059629
simple_bot = telegram_bot(token=SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN) if SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN else None

captured_opportunities = []

async def send_alert(message):
    if not simple_bot:
        print("   (Telegram disabled)")
        return
    try:
        await simple_bot.send_message(
            chat_id=SIMPLE_OPTIONS_SCANNER_CHAT_ID,
            text=message,
            disable_web_page_preview=True
        )
        print("   Telegram sent.")
    except Exception as e:
        print(f"   Telegram failed: {e}")

# ==================== COMPANY NAME HELPER ====================
def get_company_name(symbol):
    """Get company long name from yfinance with fallback"""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return info.get('longName', info.get('shortName', symbol))
    except:
        return symbol

# ==================== REGIME DETECTION ====================
async def get_current_regime():
    try:
        vix = yf.Ticker("^VIX").history(period="5d")['Close'].iloc[-1]
        spy = yf.Ticker("SPY").history(period="220d")
        spy['SMA200'] = SMAIndicator(spy['Close'], window=200).sma_indicator()
        current_price = spy['Close'].iloc[-1]
        sma200 = spy['SMA200'].iloc[-1]

        if vix > 35:
            return "BEARISH_HIGH_VOL"
        elif vix > 25:
            return "CAUTIOUS"
        elif current_price > sma200 * 1.05:
            return "STRONG_BULL"
        elif current_price > sma200:
            return "MILD_BULL"
        else:
            return "NEUTRAL_OR_WEAK"
    except Exception as e:
        print(f"   Regime detection failed: {e}. Defaulting to MILD_BULL.")
        return "MILD_BULL"

# ==================== HELPERS ====================
def get_symbol_tier(symbol):
    if symbol in TIER_1_SYMBOLS: return 1
    elif symbol in TIER_2_SYMBOLS: return 2
    else: return 3

def is_red_day(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="70d", interval="1d")
        if len(df) < 14:
            return True, 50.0, df['Close'].iloc[-1] if not df.empty else 0
        
        rsi_indicator = RSIIndicator(close=df['Close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        current_price = df['Close'].iloc[-1]
        
        is_safe = rsi < 70  # Loosened from stricter
        return is_safe, float(rsi), float(current_price)
    except Exception as e:
        print(f"   RSI error {symbol}: {e}")
        return True, 50.0, 0
    
def calculate_support_resistance(symbol, period="2y", force_refresh=False):
    cache = load_sr_cache()
    cache_key = symbol.upper()

    if not force_refresh and cache_key in cache:
        entry = cache[cache_key]
        last_calc_str = entry.get('last_calculated')
        if last_calc_str:
            try:
                last_calc = datetime.fromisoformat(last_calc_str.replace('Z', '+00:00'))
                if datetime.utcnow() - last_calc < timedelta(days=32):
                    return entry.get('levels', {})
            except:
                pass

    print(f"   Calculating fresh S/R levels for {symbol}...")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        
        if hist.empty or len(hist) < 20:
            print(f"   {symbol}: Insufficient history ({len(hist)} bars)")
            return {}

        print(f"   {symbol}: Retrieved {len(hist)} bars of data")

        levels = {}
        
        
        # Define timeframes: MONTHS -> approximate trading days
        timeframes = {
            1: 21,    # 1 month ≈ 21 trading days
            3: 63,    # 3 months ≈ 63 trading days
            6: 126,   # 6 months ≈ 126 trading days
            12: 252   # 12 months ≈ 252 trading days
        }
        
        levels_count = 0
        summary_parts = []

        for months, days_needed in timeframes.items():
            if len(hist) < days_needed:
                continue
            
            data = hist['Close'][-days_needed:]

            try:
                (min_idx, _, mintrend, _), (max_idx, _, maxtrend, _) = trendln.calc_support_resistance(
                    data, 
                    accuracy=8
                )

                support = None
                resistance = None

                # Extract support: last_min = ([indices], (slope, intercept, ...))
                if mintrend and len(mintrend) > 0:
                    last_min = mintrend[-1]
                    # last_min is a tuple: ([indices], (slope, intercept, ...))
                    if isinstance(last_min, tuple) and len(last_min) >= 2:
                        values = last_min[1]  # Get the second element (slope, intercept, ...)
                        if isinstance(values, tuple) and len(values) >= 2:
                            intercept = values[1]  # Get the intercept (second value)
                            if isinstance(intercept, (int, float, type(np.float64(0)))):
                                support = round(float(intercept), 2)

                # Extract resistance: last_max = ([indices], (slope, intercept, ...))
                if maxtrend and len(maxtrend) > 0:
                    last_max = maxtrend[-1]
                    # last_max is a tuple: ([indices], (slope, intercept, ...))
                    if isinstance(last_max, tuple) and len(last_max) >= 2:
                        values = last_max[1]  # Get the second element (slope, intercept, ...)
                        if isinstance(values, tuple) and len(values) >= 2:
                            intercept = values[1]  # Get the intercept (second value)
                            if isinstance(intercept, (int, float, type(np.float64(0)))):
                                resistance = round(float(intercept), 2)

                if support is not None or resistance is not None:
                    levels[months] = {}
                    levels_count += 1
                    if support is not None:
                        levels[months]['support'] = support
                    if resistance is not None:
                        levels[months]['resistance'] = resistance
                    summary_parts.append(f"{months}m:{support}/{resistance}")

            except Exception as e:
                continue

        # if not levels:
        #     print(f"   {symbol}: ⚠️  No S/R levels calculated for any timeframe")
        # else:
        #     print(f"   {symbol}: ✓ Successfully calculated levels for {len(levels)} timeframes")
        
        if levels_count > 0:
            print(f"✓ {', '.join(summary_parts)}")
        else:
            print(f"✗ No levels found")

        cache[cache_key] = {
            'levels': levels,
            'last_calculated': datetime.utcnow().isoformat() + 'Z'
        }
        save_sr_cache(cache)

        return levels

    except Exception as e:
        print(f"   S/R calc failed for {symbol}: {e}")
        import traceback
        traceback.print_exc()
        return {}

    
# ==================== CORE SCANNER ====================
def find_high_probability_options(symbol, current_price, tier, regime, grok_sentiment):
    opportunities = []
    
    try:
        chain_resp = c.get_option_chain(
            symbol=symbol,
            contract_type='PUT',
            from_date=datetime.now().date(),
            to_date=(datetime.now() + timedelta(days=70)).date()
        ).json()
        
        if 'putExpDateMap' not in chain_resp:
            return []

        if current_price == 0:
            current_price = chain_resp.get('underlying', {}).get('last', 100.0)

        is_grok_bull = "BULL" in grok_sentiment
        delta_min = regime["delta_min"] + (0.05 if is_grok_bull else 0)
        delta_max = regime["delta_max"] + (0.05 if is_grok_bull else 0)

        for exp_key, strikes in chain_resp['putExpDateMap'].items():
            try:
                dte = int(exp_key.split(':')[1])
                # Enforce DTE range for all regimes
                if not (regime["dte_min"] <= dte <= regime["dte_max"]):
                    continue
            except: continue

            for strike_str, contracts in strikes.items():
                if not contracts: continue
                opt = contracts[0]
                
                try:
                    strike = float(strike_str)
                    capital_needed = strike * 100
                    if capital_needed > MAX_CAPITAL_PER_TRADE:
                        continue

                    if strike > current_price * OTM_BUFFER_PCT: continue

                    bid = float(opt.get('bidPrice', 0) or 0)
                    ask = float(opt.get('askPrice', 0) or 0)
                    mark = (bid + ask) / 2
                    last = float(opt.get('lastPrice', 0) or 0)
                    close = float(opt.get('closePrice', 0) or 0)

                    premium = bid if bid > 0 else mark if mark > 0 else last if last > 0 else close

                    if premium < MIN_PREMIUM: continue

                    delta = abs(float(opt.get('delta', 0) or 0))
                    if not (delta_min <= delta <= delta_max): continue

                    iv = float(opt.get('volatility', 0) or 0)
                    if iv < regime["iv_min"]:
                            continue

                    income = premium * 100
                    roi = (income / capital_needed) * 100
                    annualized = (roi / dte) * 365 if dte > 0 else 0

                    if annualized < MIN_ANNUALIZED: continue

                    opportunities.append({
                        'symbol': symbol,
                        'strike': strike,
                        'premium': premium,
                        'dte': dte,
                        'annualized_roi': annualized,
                        'delta': delta,
                        'capital': capital_needed,
                        'current_price': current_price,
                        'distance': ((current_price - strike) / current_price) * 100,
                        'contract': opt.get('symbol', ''),
                        'iv': iv,
                        'tier': get_symbol_tier(symbol)
                    })
                except: continue

        opportunities.sort(key=lambda x: x['annualized_roi'], reverse=True)
        return opportunities[:5]  # Keep top 5 per symbol
    except Exception as e:
        print(f"   Chain error {symbol}: {e}")
        return []

def improved_put_score(premium, delta, dte, annualized_roi, iv, vol_surge, rsi, in_uptrend, distance_pct=0, tier=3, capital=0, iv_rank=50, sr_risk_flag="Neutral", regime="MILD_BULL", sr_mult=1.0, **kwargs):
    """
    Updated pre-Grok score with S/R bonus, stronger vol surge, and regime adjustment.
    """
    optimal_delta = 0.30
    delta_bonus = 1 + (1 - abs(delta - optimal_delta) / optimal_delta) * 0.6
    if delta < 0.15:
        delta_bonus *= 0.8
    
    base = premium * delta_bonus
    
    vol_mult = 1 + min(vol_surge, 2.0) * 0.15  # Increased weight for surges
    rsi_mult = 1.15 if rsi < 50 else 1.0
    trend_mult = 1.2 if in_uptrend else 0.9

    # IV Multiplier — Peak at 60-90%, reward high but penalize extreme
    if iv < 30:
        iv_mult = 0.8   # Too low — poor premium
    elif iv < 50:
        iv_mult = 1.1
    elif iv < 60:
        iv_mult = 1.25
    elif iv < 90:
        iv_mult = 1.4   # Sweet spot: high premium, still tradable vol
    elif iv < 120:
        iv_mult = 1.25  # Still good, but starting to get risky
    elif iv < 150:
        iv_mult = 1.1   # Very high — yield great but high risk
    else:
        iv_mult = 0.9   # Extreme — avoid (meme crashes, earnings bombs)
    
    # DTE multiplier
    if dte < 14:
        dte_mult = 0.7  # Strong penalty for very short DTE (high theta risk, low premium)
    elif dte < 21:
        dte_mult = 0.9  # Mild penalty — too short for wheel safety
    elif 21 <= dte <= 30:
        dte_mult = 1.15  # Good — safe entry zone
    elif 30 <= dte <= 45:
        dte_mult = 1.35   # BEST — ideal wheel sweet spot: good premium + manageable theta
    elif 45 <= dte <= 60:
        dte_mult = 1.15  # Still good — longer gives more buffer
    else:  # >60 DTE
        dte_mult = 1.05  # Slightly reduced — too long ties up capital

    # Distance bonus
    if distance_pct > 15:
        distance_mult = 1.3
    elif distance_pct > 10:
        distance_mult = 1.2
    elif distance_pct > 5:
        distance_mult = 1.1
    else:
        distance_mult = 1.0
    
    # Tier multiplier
    if tier == 1:
        tier_mult = 1.15
    elif tier == 2:
        tier_mult = 1.08
    else:
        tier_mult = 1.02
    
    capital_mult = 1.1 if capital < 15000 else 1.0
    
    sr_mult = 1.0
    if "Low" in sr_risk_flag:
        sr_mult = 1.15
    elif "Moderate" in sr_risk_flag:
        sr_mult = 1.0
    elif "High" in sr_risk_flag:
        sr_mult = 0.85

    # NEW: Regime adjustment (e.g., penalize aggressive in cautious regimes)
    regime_mult = 1.0
    if regime in ["CAUTIOUS", "BEARISH_HIGH_VOL"]:
        regime_mult = 0.9 if delta > 0.30 else 1.1  # Favor lower delta in defensive regimes

    # Apply all multipliers
    score = base * iv_mult * dte_mult * vol_mult * rsi_mult * trend_mult * distance_mult * tier_mult * sr_mult * regime_mult * capital_mult
    
    final_score = (score * 8 + annualized_roi * 2) / 2
    return final_score

# ==================== MAIN ====================
async def analyze_symbol(symbol, regime, grok_sentiment):
    print(f"Checking {symbol:<6}", end=" ", flush=True)
    
    tier = get_symbol_tier(symbol)
    is_safe, rsi, price = is_red_day(symbol)
    
    if not is_safe:
        print(f"| RSI {rsi:.1f} (Too Hot) - Skipped")
        return None
    
    print(f"| RSI {rsi:.1f} | Tier {tier}", end=" ")
    
    opps = find_high_probability_options(symbol, price, tier, regime, grok_sentiment)
    if opps:
        print(f"| FOUND {len(opps)}")
        for o in opps:
            o['rsi'] = rsi
            o['regime'] = regime['name']
            o['grok_sentiment'] = grok_sentiment
        
    for o in tqdm(captured_opportunities, desc="Grok Analysis", unit="trade"):
        try:
            prob, oneliner = get_grok_opportunity_analysis(
                symbol=o['symbol'],
                price=o['current_price'],
                strike=o['strike'],
                dte=o['dte'],
                premium=o['premium'],
                delta=o['delta'],
                iv=o.get('iv', 30),
                rsi=o.get('rsi', 50),
                vol_surge=1.0,
                in_uptrend=True
            )
        except Exception as e:
            logging.warning(f"Grok analysis failed for {o['symbol']}: {e}")
            prob = "N/A"
            oneliner = "Analysis unavailable"

        o['grok_profit_prob'] = prob
        o['grok_one_liner'] = oneliner

        return opps
    else:
        print("| No Valid Contracts")
        return None
    

async def main():
    global captured_opportunities

    print("\n" + "="*70)
    print("STARTING Simple Options SCANNER")
    print("="*70)
    
    regime_key = await get_current_regime()
    regime = REGIME_SETTINGS[regime_key]
    grok_sentiment, grok_summary = get_grok_sentiment_cached()
    
    sentiment_emoji = "🚀" if "STRONG" in grok_sentiment else "📈" if "BULL" in grok_sentiment else "😐" if "NEUTRAL" in grok_sentiment else "⚠️" if "CAUTIOUS" in grok_sentiment else "🔴"

    # Use safe printing to avoid Unicode encoding errors on Windows
    try:
        print(f"GROK SENTIMENT: {grok_sentiment.replace('_', ' ')}")
        print(f"{grok_summary}")
        print(f"REGIME: {regime['name']}")
        print(f"-> Delta: {regime['delta_min']:.2f}-{regime['delta_max']:.2f}")
        print(f"-> DTE: {regime['dte_min']}-{regime['dte_max']} days")
        print(f"-> Close Target: {regime['target_profit_pct']}% profit\n")
    except UnicodeEncodeError:
        # Fallback to ASCII-safe output
        safe_summary = grok_summary.encode('ascii', 'replace').decode('ascii')
        print(f"GROK SENTIMENT: {grok_sentiment.replace('_', ' ')}")
        print(safe_summary)
        print(f"REGIME: {regime['name']}")
        print(f"-> Delta: {regime['delta_min']:.2f}-{regime['delta_max']:.2f}")
        print(f"-> DTE: {regime['dte_min']}-{regime['dte_max']} days")
        print(f"-> Close Target: {regime['target_profit_pct']}% profit\n")

    scan_time = datetime.now(ET_TZ)
    watchlist = SIMPLE_WATCHLIST
    if regime['tier_limit'] == "TIER_1":
        watchlist = [s for s in SIMPLE_WATCHLIST if get_symbol_tier(s) == 1]
    elif regime['tier_limit'] == "TIER_1_2":
        watchlist = [s for s in SIMPLE_WATCHLIST if get_symbol_tier(s) <= 2]

    MAX_CONCURRENT = 8
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded_scan(sym):
        async with sem:
            is_safe, rsi, price = is_red_day(sym)
            if not is_safe:
                return []

            opps = find_high_probability_options(sym, price, get_symbol_tier(sym), regime, grok_sentiment)
            
            if not opps:
                return []  # No opportunities → skip S/R fetch entirely
            
            sr_levels = calculate_support_resistance(sym)

            # Attach RSI
            for o in opps:
                o['rsi'] = rsi
                o['support_resistance'] = sr_levels

                sr_risk = "Neutral"
                sr = sr_levels.get(3, {})  # 3-month
                support_3m = sr.get('support')
                if support_3m is not None:
                    if o['strike'] < support_3m * 0.97:
                        sr_risk = "Low (well below 3m support)"
                    elif o['strike'] < support_3m * 1.03:
                        sr_risk = "Moderate (near 3m support)"
                    else:
                        sr_risk = "High (at/above 3m support)"
                o['sr_risk_flag'] = sr_risk

            status = f"Found {len(opps)} opps" if opps else "No qualifying puts"
            print(f" → {status}")
            return opps
    
    print(f"Scanning {len(watchlist)} symbols concurrently (max {MAX_CONCURRENT} at once)...\n")

    tasks = [bounded_scan(sym) for sym in watchlist]
    results = await tqdm_asyncio.gather(*tasks)

    all_opps = [opp for result in results for opp in result]

    if not all_opps:
        print("No opportunities found.")
        await send_alert("No scanner opportunities found this run.")
        return

    # Sort by internal score first
    all_opps.sort(key=lambda x: improved_put_score(
        premium=x['premium'],
        distance_pct=x['distance'],
        delta=x['delta'],
        dte=x['dte'],
        annualized_roi=x['annualized_roi'],
        iv=x['iv'],
        vol_surge=x.get('vol_surge', 0.5),
        rsi=x.get('rsi', 50),
        in_uptrend=True,
        tier=x.get('tier', 3),
        capital=x['capital'], 
        iv_rank=x.get('iv_rank', 50),
        sr_risk_flag=x.get('sr_risk_flag', 'Neutral')
    ), reverse=True)

    for i, opp in enumerate(all_opps):
        opp['global_rank'] = i + 1

    # Then, before sending individual alerts, add top 10 global:

    top_10_msg = "🌟 TOP 10 GLOBAL RANKED OPPORTUNITIES:\n\n"
    for i, opp in enumerate(all_opps[:10], 1):
        top_10_msg += (
            f"#{i} {opp['symbol']} — {opp['dte']} DTE ${opp['strike']:.0f}P\n"
            f"Score: {improved_put_score(opp['premium'], opp['delta'], opp['dte'], opp['annualized_roi'], opp['iv'], opp.get('vol_surge', 0.5), opp.get('rsi', 50), True):.1f}/100\n"
            f"Premium: ${opp['premium']:.2f} | Annualized: {opp['annualized_roi']:.1f}%\n"
            f"Delta: {opp['delta']:.2f} | Distance: {opp['distance']:.1f}%\n\n"
        )
    await send_alert(top_10_msg)


    # Assign global ranks and distance
    for rank, opp in enumerate(all_opps, 1):
        opp['overall_rank'] = rank
        opp['distance_pct'] = opp['distance']

    top_opps = all_opps[:100]  # Top 100 for Grok scoring

    # === GROK SCORING ON TOP 100 (WITH NUMERICAL SCORE) ===
    BATCH_SIZE = 25 # Keep small for reliability
    print(f"Running FAST batched Grok analysis on {len(top_opps)} opportunities ({(len(top_opps)-1)//BATCH_SIZE + 1} batches)...")

    batched_results = []
    for i in range(0, len(top_opps), BATCH_SIZE):
        batch = top_opps[i:i+BATCH_SIZE]

        for opp in batch:
            sr = opp.get('support_resistance', {})
            sr_risk = "Neutral"
            
            if sr and 3 in sr:
                support_3m = sr[3].get('support')
                if support_3m is not None:
                    strike = opp['strike']
                    if strike < support_3m * 0.97:
                        sr_risk = "Low (well below 3m support)"
                    elif strike < support_3m * 1.03:
                        sr_risk = "Moderate (near 3m support)"
                    else:
                        sr_risk = "High (at/above 3m support)"
            
            opp['sr_risk_flag'] = sr_risk

        # STRONGER PROMPT: Force exact format + numbering for fallback
        full_prompt = f"""
            You are an expert options trader specializing in cash-secured puts and the wheel strategy.

            Analyze these {len(batch)} put opportunities in the current regime:
            Regime: {regime['name']} (prioritize downside protection in cautious/bearish regimes)
            Grok Sentiment: {grok_sentiment.replace('_', ' ')}

            KEY PRIORITIES (in rough order of importance):
                1. Premium yield + Annualized ROI (higher = much better)
                2. Safety: 
                    - Lower delta: BEST = .20-.30 delta, GOOD = .31-.37 delta, PENALIZE > .38 delta 
                    - Further OTM distance: BEST = 10% and Above OTM %, GOOD = 5% - 9.9% OTM %, PENALIZE < 4.9 OTM % 
                    - strike well below support
                3. DTE: BEST = 25–45 days (peak score), GOOD = 14-25 or 46–60 days, PENALIZE <14 or >60 days
                4. IV: BEST = 60–100% (high premium without extreme risk), GOOD = 50–60% or 100–120%, PENALIZE <40% (low yield) or >130% (meme/crash risk)
                5. RSI: Lower/oversold = better entry (bonus if <50)
                6. Tier 1 stocks preferred, followed by Tier 2; Tier 3 only in strong bullish regimes, strong premium, and safe setups
                7. S/R Risk: "Low (well below 3m support)" = big boost, "High" = penalty

            Score guide (0–100):
            - 90–100: Exceptional wheel setup (high yield + very safe)
            - 80–89: Strong (great premium + good safety)
            - 70–79: Good (solid but minor flaws)
            - 60–69: Average (ok but not exciting)
            - <60: Weak/Avoid

            CRITICAL: For RECOMMENDATION, you MUST use ONLY one of these exact phrases. No variations allowed:

            - Enter Now          → Best opportunities (high score, safe)
            - Strong Enter       → Very good, aggressive entry
            - Consider Entering  → Decent but with caveats
            - Hold/Monitor       → Neutral, wait for better setup
            - Avoid              → Poor risk/reward

            CRITICAL FORMATTING REQUIREMENT FOR REASON:

            Your REASON field MUST be AT LEAST 25 WORDS. Single-word or short responses will be rejected.

            Write 2-4 complete sentences (minimum 25 words, target 40-50 words) that explain:

            Sentence 1: Lead with the ROI (e.g., "Strong 40% annualized return with...") and primary safety factor (delta/distance/S&R)
            Sentence 2: Mention 2-3 key technical signals (RSI level, IV context, DTE appropriateness)
            Sentence 3: Note support/resistance context and any risk considerations
            Optional Sentence 4: Company/sector sentiment if relevant

            EXAMPLE GOOD REASON:
            "Excellent 54% annualized ROI with strong safety buffer - 0.22 delta and 16% OTM provide solid downside protection. RSI at 60 shows healthy momentum without overbought risk, while 98% IV offers strong premium. Strike sits well below 3-month support at $350, giving substantial cushion even in pullback scenarios."

            EXAMPLE BAD REASON (TOO SHORT):
            "Outstanding" ❌ REJECTED
            "Good setup" ❌ REJECTED

            Respond in EXACT format. One block per opportunity. No extra text, no explanations outside blocks.

            """

        for idx, opp in enumerate(batch, 1):
            # === Support/Resistance Multi-Timeframe Display ===
            sr_risk = opp.get('sr_risk_flag', 'Unknown')

            # === Append Opportunity Details ===
            full_prompt += f"""
                --- OPPORTUNITY {idx} ---
                Symbol: {opp['symbol']}
                Underlying Price: ${opp['current_price']:.2f}
                Strike: ${opp['strike']:.0f} PUT
                DTE: {opp['dte']}
                Premium: ${opp['premium']:.2f}
                Annualized ROI: {opp['annualized_roi']:.1f}%
                Delta: {opp['delta']:.2f}
                IV: {opp['iv']:.0f}%
                RSI: {opp['rsi']:.1f}
                Distance OTM: {opp['distance']:.1f}%
                Tier: {opp.get('tier', 3)}
                S/R Risk (vs 3m support): {sr_risk}

                SCORE: [Your 0-100 score here]
                RECOMMENDATION: [MUST be one of: Enter Now, Strong Enter, Consider Entering, Hold/Monitor, Avoid]
                REASON: [MINIMUM 25 WORDS - Write 2-4 complete sentences explaining ROI, safety metrics, technical factors, and S/R context with specific numbers]
                --- END ---
                """

        try:
            # Call get_grok_analysis correctly with symbol and context parameters
            # Use empty symbol to avoid prepending, and pass prompt as context
            from grok_utils import GROK_API_KEY, GROK_ENDPOINT
            import requests

            # Direct API call to avoid the prepended text from get_grok_analysis
            response_obj = requests.post(
                GROK_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {GROK_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "grok-4-1-fast-reasoning",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": 2000  # Increased for batch analysis
                }
            )

            if response_obj.status_code == 200:
                response = response_obj.json()['choices'][0]['message']['content']
            else:
                print(f"Grok API error: {response_obj.status_code}")
                response = ""
            # print(f"DEBUG: Batch {i//BATCH_SIZE + 1} raw response:\n{response}\n")
        except Exception as e:
            print(f"Batch failed: {e}")
            response = ""

        try:
            blocks = parse_grok_batch_response(response, len(batch))
        except Exception as e:
            print(f"Parsing completely failed: {e}")
            blocks = []

        # Assign to batch (in order)
        for j in range(min(len(blocks), len(batch))):
            block = blocks[j]
            opp = batch[j].copy()

            score = 0
            recommendation = "Avoid"
            reason = "Analysis failed"

            block_lines = [l.strip() for l in block.split('\n') if l.strip()]
            reason_lines = []
            capturing_reason = False

            for line in block_lines:
                low_line = line.lower()

                # Stop capturing reason when we hit a new field or END marker
                if capturing_reason:
                    if low_line.startswith(("score:", "recommendation:", "---")) or "end" in low_line:
                        capturing_reason = False
                    else:
                        reason_lines.append(line)
                        continue

                if low_line.startswith("score:"):
                    try:
                        score = int(line.split(":")[1].strip().split("/")[0].strip())
                    except:
                        pass
                elif low_line.startswith("recommendation:"):
                    recommendation = line.split(":", 1)[1].strip()
                elif low_line.startswith("reason:"):
                    # Start capturing reason - may span multiple lines
                    first_part = line.split(":", 1)[1].strip()
                    if first_part:
                        reason_lines.append(first_part)
                    capturing_reason = True

            # Join all reason lines into a single string
            if reason_lines:
                reason = " ".join(reason_lines).strip()

            # If reason is still too short (single word), try to extract more context
            if len(reason.split()) < 5:
                # Check if there's more text in the block that might be the reason
                full_text = " ".join(block_lines)
                if "REASON:" in full_text.upper():
                    reason_start = full_text.upper().find("REASON:")
                    reason_text = full_text[reason_start + 7:].strip()
                    # Stop at next field marker
                    for marker in ["SCORE:", "RECOMMENDATION:", "---", "END"]:
                        if marker in reason_text.upper():
                            reason_text = reason_text[:reason_text.upper().find(marker)].strip()
                    if len(reason_text) > len(reason):
                        reason = reason_text

            badge = "⚠️"
            if score >= 90: badge = "🔥"
            elif score >= 80: badge = "🚀"
            elif score >= 70: badge = "✅"
            elif score >= 60: badge = "⚡"

            oneliner = f"{recommendation} — {reason}"

            opp.update({
                'grok_profit_prob': f"{score}%",
                'grok_one_liner': oneliner,
                'grok_trade_score': score,
                'score_badge': badge,
                'grok_recommendation': recommendation,
                'grok_reason': reason,
            })
            batched_results.append(opp)

        # If Grok skipped some, fill with defaults (rare)
        for j in range(len(blocks), len(batch)):
            opp = batch[j].copy()
            opp.update({
                'grok_trade_score': 0,
                'score_badge': "⚠️",
                'grok_recommendation': "Avoid",
                'grok_reason': "Grok skipped analysis",
            })
            batched_results.append(opp)

        await asyncio.sleep(1.5)

    top_opps = batched_results

    # Final sort by Grok score
    top_opps.sort(key=lambda x: x.get('grok_trade_score', 0), reverse=True)

    # === FILTER: Only keep opportunities with Grok score >= 80 ===
    high_conviction_opps = [opp for opp in top_opps if opp.get('grok_trade_score', 0) >= 75]

    print(f"After filtering >=80: {len(high_conviction_opps)} high-conviction opportunities")

    if not high_conviction_opps:
        print("No opportunities scored 80 or higher — keeping top 20 for reference")
        high_conviction_opps = top_opps[:30]  # Fallback so dashboard isn't empty

    # Assign overall_rank to filtered list
    for i, opp in enumerate(high_conviction_opps, 1):
        opp['overall_rank'] = i

    # === BUILD TILES BY SYMBOL (from filtered high-conviction only) ===f
    from collections import defaultdict
    symbol_groups = defaultdict(list)
    for opp in high_conviction_opps:
        symbol_groups[opp['symbol']].append(opp)

    sorted_symbols = sorted(symbol_groups.items(),
                            key=lambda x: max(o.get('grok_trade_score', 0) for o in x[1]),
                            reverse=True)

    scanner_tiles = []
    for symbol, opps in sorted_symbols:
        opps.sort(key=lambda x: x.get('grok_trade_score', 0), reverse=True)
        tile = {
            'symbol': symbol,
            'company_name': get_company_name(symbol),
            'best_score': opps[0].get('grok_trade_score', 0),
            'best_badge': opps[0].get('score_badge', '⚠️'),
            'suggestions': opps
        }
        scanner_tiles.append(tile)

    print(f"Built {len(scanner_tiles)} tiles with only 80+ Grok scores")
    

    # === SEND RANKED TILES TO TELEGRAM ===
    header = (
        f"{sentiment_emoji} GROK SENTIMENT: {grok_sentiment.replace('_', ' ')}\n"
        f"{grok_summary}\n\n"
        f"🎯 STRATEGY: {regime['name'].upper()}\n"
        f"Top Scored Opportunities: {len(top_opps)}\n\n"
        f"Ranked by Grok Score (0–100):"
    )
    await send_alert(header)

    MAX_TELEGRAM_ALERTS = 10

    for i, tile in enumerate(scanner_tiles):
        if i >= MAX_TELEGRAM_ALERTS:
            break
        if not tile['suggestions']:
            continue

        best = tile['suggestions'][0]
        if best.get('grok_trade_score', 0) < 60:  # Only send good opportunities
            continue

        distance_emoji = "Far OTM 🟢" if best['distance'] > 5 else "Near OTM 🟡" if best['distance'] > 2 else "ATM 🔴"
        safety = "SAFE" if best['delta'] < 0.30 else "AGGRESSIVE"
        
        close_price = best['premium'] * (1 - regime['target_profit_pct']/100)
        
        msg = (
            f"#{best['overall_rank']} — {tile['symbol']} (Score: {tile['best_score']}/100 {tile['best_badge']})\n"
            f"{best['dte']} DTE ${best['strike']:.0f}P\n"
            f"💵 Price: ${best['current_price']:.2f} | RSI: {best['rsi']:.1f}\n"
            f"📊 IV: {best.get('iv', 0):.0f}%\n\n"
            f"🎯 BEST PUT:\n"
            f"  💰 Premium: ${best['premium']:.2f}\n"
            f"  📈 Annualized: {best['annualized_roi']:.1f}%\n"
            f"  🛡️ Delta: {best['delta']:.2f} ({safety})\n"
            f"  💸 Capital: ${best['capital']:,.0f}\n"
            f"  📏 Distance: {best['distance']:.1f}% {distance_emoji}\n\n"
            f"🤖 Grok: {best['grok_recommendation']}\n"
            f"{best['grok_reason']}\n\n"
            f"EXIT at ${close_price:.2f} → {regime['target_profit_pct']}% target"
        )
        await send_alert(msg)
        await asyncio.sleep(2)

    
    export_to_csv(top_opps, scan_time)
   
    try:
        captured_opportunities = scanner_tiles
        save_cached_scanner(scanner_tiles)
        
        print(f"Cached {len(scanner_tiles)} scanner opportunities")
    except Exception as e:
        print(f"Cache save failed: {e}")
        # Still try to save without emojis
        try:
            clean_tiles = []
            for tile in scanner_tiles:
                clean_tile = tile.copy()
                clean_tile['best_badge'] = str(tile['best_badge']).encode('ascii', 'ignore').decode('ascii')
                clean_tiles.append(clean_tile)
            save_cached_scanner(clean_tiles)
            print(f"Cached {len(clean_tiles)} cleaned opportunities")
        except Exception as e2:
            print(f"Clean cache also failed: {e2}")
    
    
def export_to_csv(opportunities, scan_time):
    if not opportunities: return
    folder = Path("scanner_results")
    folder.mkdir(exist_ok=True)
    filepath = folder / f"wheel_scan_{scan_time.strftime('%Y-%m-%d_%H%M')}.csv"
    
    # REMOVE EMOJIS/UNICODE before saving to CSV
    cleaned_opps = []
    for opp in opportunities:
        clean_opp = opp.copy()
        # Remove any emoji fields
        for key in ['score_badge', 'grok_one_liner', 'grok_reason']:
            if key in clean_opp:
                clean_opp[key] = str(clean_opp[key]).encode('ascii', 'ignore').decode('ascii')
        cleaned_opps.append(clean_opp)
    
    keys = cleaned_opps[0].keys() if cleaned_opps else []
    with open(filepath, 'w', newline='', encoding='utf-8') as f:  # ADD UTF-8
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(cleaned_opps)
    print(f" Saved to {filepath}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()