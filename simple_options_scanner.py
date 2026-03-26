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
from ta.trend import SMAIndicator, MACD as MACDIndicator
from grok_utils import get_grok_opportunity_analysis, get_grok_sentiment_cached
from telegram_utils import send_alert as _send_alert_impl
from helper_functions import save_cached_scanner, load_sr_cache, save_sr_cache
from schwab_utils import get_client
from sector_sentiment import get_sector_scores, get_symbol_sector, get_symbol_sector_score

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

# Schwab client — lazy loaded on first use so server starts even if token is expired
_c = None
def _get_schwab_client():
    global _c
    if _c is None:
        _c = get_client()
        _c.set_enforce_enums(False)
    return _c

ET_TZ = pytz.timezone('US/Eastern')

# ==================== SYMBOL TIERS ====================
# Tier 1: Wheel-grade blue chips — stocks you'd be happy to own if assigned
# Tier 2: Quality growth — higher volatility but institutional backing
# Removed: leveraged ETFs (TQQQ/SOXL — structural decay), crypto miners (MARA/RIOT/IREN — BTC-correlated),
#          duplicate symbols across tiers, ultra-speculative names (CCCX, BMNR)
TIER_1_SYMBOLS = [
    # Dividend Aristocrats & Blue Chips
    'KO', 'JNJ', 'PG', 'WMT', 'VZ', 'MRK', 'BMY', 'ABBV', 'PEP', 'CL', 'KMB',
    # Energy
    'XOM', 'CVX', 'COP', 'PSX', 'MPC', 'VLO',
    # REITs & Stable Income
    'O', 'STAG', 'SPG',
    # Retail & Consumer
    'HD', 'LOW', 'TGT', 'COST', 'TJX',
    # Brands & Consumer Discretionary
    'NKE', 'DIS', 'SBUX', 'MCD',
    # Industrials
    'CAT', 'DE', 'HON', 'MMM', 'BA', 'RTX', 'LMT', 'GD', 'NOC',
    # Healthcare & Insurance
    'UNH', 'CVS', 'CI', 'HUM', 'AMGN', 'GILD',
    # Major ETFs
    'SPY', 'QQQ', 'IWM', 'DIA', 'VOO', 'VTI', 'SCHD', 'JEPI', 'JEPQ',
    # Mega Cap Tech
    'AAPL', 'MSFT', 'NVDA', 'AMD', 'META', 'AMZN', 'GOOGL', 'NFLX', 'AVGO',
    # Financials
    'JPM', 'BAC', 'WFC', 'USB', 'PNC', 'TFC', 'BLK', 'GS', 'MS', 'SCHW', 'AXP', 'V', 'MA',
    # Telecom & Utilities
    'T', 'TMUS', 'NEE', 'DUK', 'SO', 'D',
    # Tech Infrastructure
    'CSCO', 'IBM', 'ORCL', 'CRM', 'NOW',
    # Industrial & Manufacturing
    'GE', 'JCI', 'EMR', 'ITW',
    # Nuclear Energy
    'LEU', 'CCJ', 'CEG', 'BWXT', 'NLR', 'VST',
    # Semiconductors
    'TSM', 'ASML', 'QCOM', 'TXN', 'AMAT', 'LRCX', 'KLAC', 'MU'
]

TIER_2_SYMBOLS = [
    # Enterprise Software & Cloud
    'ADBE', 'SHOP', 'SNOW', 'DDOG', 'MDB', 'NET', 'ZS', 'CRWD', 'PANW', 'FTNT',
    'S', 'TEAM', 'WDAY', 'ZI', 'GTLB',
    # Semiconductors & Hardware
    'MRVL', 'SNPS', 'CDNS', 'INTC', 'ON', 'ADI', 'NXPI', 'MCHP',
    # Pharma & Biotech
    'PFE', 'LLY', 'NVO', 'AZN', 'REGN', 'VRTX', 'BIIB', 'MRNA', 'BNTX',
    # Communication & Collaboration
    'ZM', 'TWLO', 'DOCN', 'U',
    # Consumer & Restaurant
    'CMG', 'YUM', 'QSR', 'DPZ', 'WING', 'CAVA',
    # Logistics & Transportation
    'UPS', 'FDX', 'ODFL', 'XPO', 'JBHT',
    # Healthcare Services & Distribution
    'MCK', 'CAH', 'ABC', 'VEEV',
    # Fintech
    'PYPL', 'SQ', 'COIN', 'SOFI',
    # Entertainment & Gaming
    'RBLX', 'DKNG', 'PINS',
    # Growth Tech
    'PLTR', 'ROKU', 'DASH', 'ABNB', 'UBER',
    # Energy & Resources
    'XLE', 'OXY', 'HAL', 'SLB', 'FCX', 'CLF', 'BP', 'EOG', 'DVN', 'FANG',
    # Regional Banks
    'CFG', 'FITB', 'KEY', 'RF', 'ZION', 'HBAN', 'MTB',
    # Growth / Nuclear
    'HIMS', 'OKLO', 'SMR', 'URA', 'IONQ', 'RDDT', 'ARM'
]

TIER_3_SYMBOLS = [
    # Higher risk — only scanned in STRONG_BULL regime
    'HOOD', 'F', 'NEM', 'VALE', 'EXPE', 'NCLH', 'CCL', 'ENPH',
    'FSLR', 'TSLA', 'RIVN', 'GM', 'LUV', 'MAR', 'RCL'
]

SIMPLE_WATCHLIST = TIER_1_SYMBOLS + TIER_2_SYMBOLS + TIER_3_SYMBOLS

# ==================== REGIME SETTINGS ====================
REGIME_SETTINGS = {
    "STRONG_BULL": {
        "delta_min": 0.15, "delta_max": 0.40,  # Loosened max
        "dte_min": 10, "dte_max": 40,
        "target_profit_pct": 65,
        "tier_limit": None,
        "name": "Aggressive Bull Wheel",
        "iv_min": 30
    },
    "MILD_BULL": {
        "delta_min": 0.15, "delta_max": 0.37,
        "dte_min": 14, "dte_max": 45,
        "target_profit_pct": 55,
        "tier_limit": None,
        "name": "Classic Wheel",
        "iv_min": 35
    },
    "NEUTRAL_OR_WEAK": {
        "delta_min": 0.15, "delta_max": 0.35,
        "dte_min": 21, "dte_max": 45,
        "target_profit_pct": 50,
        "tier_limit": "TIER_1_2",
        "name": "Balanced Wheel",
        "iv_min": 40
    },
    "CAUTIOUS": {
        "delta_min": 0.10, "delta_max": 0.30,
        "dte_min": 30, "dte_max": 45,
        "target_profit_pct": 45,
        "tier_limit": "TIER_1",
        "name": "Defensive Wheel",
        "iv_min": 50
    },
    "BEARISH_HIGH_VOL": {
        "delta_min": 0.10, "delta_max": 0.30,
        "dte_min": 45, "dte_max": 50,
        "target_profit_pct": 40,
        "tier_limit": "TIER_1",
        "name": "Ultra-Defensive Wheel",
        "iv_min": 50
    }
}

# Loosened global filters
MIN_PREMIUM = 0.20  # Was higher
MIN_ANNUALIZED = 12  # Lower threshold
# OTM buffer is now regime-dependent — see _get_otm_buffer()
OTM_BUFFER_PCT_DEFAULT = 0.92  # 8% OTM minimum (was 0.95 / 5%)
OTM_BUFFER_BY_REGIME = {
    'STRONG_BULL': 0.95,       # 5% OTM — looser in strong bull
    'MILD_BULL': 0.93,         # 7% OTM
    'NEUTRAL_OR_WEAK': 0.92,   # 8% OTM
    'CAUTIOUS': 0.90,          # 10% OTM — tighter in cautious
    'BEARISH_HIGH_VOL': 0.88,  # 12% OTM — most conservative
}

# ==================== TELEGRAM ====================
captured_opportunities = []

async def send_alert(message):
    await _send_alert_impl("scanner", message)

# ==================== GROK BATCH SCORING SYSTEM PROMPT ====================
_BATCH_SYSTEM_PROMPT = (
    "You are a quantitative CSP analyst rating wheel-strategy put-selling opportunities.\n"
    "Score each 0-100 using these priorities (highest to lowest):\n"
    "1. Safety: delta best=.20-.30, penalize>.38; OTM best=10%+, penalize<7%; earnings>14d required\n"
    "2. Yield: annualized ROI — reward but never at expense of safety\n"
    "3. DTE: best=25-45d, penalize<14 or >60\n"
    "4. IV: best=60-100%, penalize<40% or >130%; bonus if IV>HV by 20%+\n"
    "5. RSI<50 = oversold bonus; rebound_score 10+/15 = bonus\n"
    "6. Tier 1 preferred, Tier 2 ok, Tier 3 only in strong bull\n"
    "7. S/R: 'Low (well below 3m support)'=boost, 'High'=penalty\n"
    "8. WARN:stock<20%above52wLow = falling knife penalty\n"
    "9. Liquidity: oi>500 and spread<5% = bonus; macd=bullish_crossover = bonus\n"
    "Score bands: 90-100=Exceptional, 80-89=Strong, 70-79=Good, 60-69=Average, <60=Avoid\n"
    "Recommendation MUST be exactly one of: 'Enter Now', 'Strong Enter', 'Consider Entering', 'Hold/Monitor', 'Avoid'\n"
    "Reason: 25-50 words covering ROI, safety factors, technical signals, S/R context.\n"
    "Return ONLY valid JSON — no markdown, no extra text:\n"
    '{"opportunities": [{"idx": <int>, "score": <int 0-100>, "recommendation": "<str>", "reason": "<str>"}]}\n'
    "Include ALL opportunities in order."
)

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
    """Returns dict: {"regime": str, "vix_ratio": float}
    vix_ratio = VIX / VIX3M  (>1 = backwardation/fear, <1 = contango/calm)
    """
    try:
        vix = yf.Ticker("^VIX").history(period="5d")['Close'].iloc[-1]
        spy = yf.Ticker("SPY").history(period="220d")
        spy['SMA200'] = SMAIndicator(spy['Close'], window=200).sma_indicator()
        current_price = spy['Close'].iloc[-1]
        sma200 = spy['SMA200'].iloc[-1]

        # VIX term structure: VIX > VIX3M (backwardation) = genuine fear signal
        vix_backwardation = False
        vix_ratio = 1.0  # neutral default
        try:
            vix3m = yf.Ticker("^VIX3M").history(period="5d")['Close'].iloc[-1]
            if vix3m > 0:
                vix_ratio = round(vix / vix3m, 3)
            vix_backwardation = vix > vix3m
            if vix_backwardation:
                print(f"   VIX term structure: BACKWARDATION (VIX {vix:.1f} > VIX3M {vix3m:.1f}) — elevated fear | ratio={vix_ratio:.3f}")
            else:
                print(f"   VIX term structure: CONTANGO (VIX {vix:.1f} < VIX3M {vix3m:.1f}) — calm | ratio={vix_ratio:.3f}")
        except Exception:
            pass  # VIX3M data not always available

        if vix > 35:
            regime = "BEARISH_HIGH_VOL"
        elif vix > 25 or (vix > 20 and vix_backwardation):
            regime = "CAUTIOUS"
        elif current_price > sma200 * 1.05 and not vix_backwardation:
            regime = "STRONG_BULL"
        elif current_price > sma200:
            regime = "MILD_BULL"
        else:
            regime = "NEUTRAL_OR_WEAK"

        return {"regime": regime, "vix_ratio": vix_ratio}
    except Exception as e:
        print(f"   Regime detection failed: {e}. Defaulting to MILD_BULL.")
        return {"regime": "MILD_BULL", "vix_ratio": 1.0}

# ==================== HELPERS ====================
def get_symbol_tier(symbol):
    if symbol in TIER_1_SYMBOLS: return 1
    elif symbol in TIER_2_SYMBOLS: return 2
    else: return 3

def is_red_day(symbol):
    """Legacy function - kept for backwards compatibility. Use check_rebound_signals() for better filtering."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="70d", interval="1d")
        if len(df) < 14:
            return True, 50.0, df['Close'].iloc[-1] if not df.empty else 0

        rsi_indicator = RSIIndicator(close=df['Close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        current_price = df['Close'].iloc[-1]

        is_safe = rsi < 50  # Stricter for better wheel strategy outcomes
        return is_safe, float(rsi), float(current_price)
    except Exception as e:
        print(f"   RSI error {symbol}: {e}")
        return True, 50.0, 0

def check_rebound_signals(symbol, hist_df=None):
    """
    Comprehensive rebound detection for wheel strategy.
    Returns: (is_good_candidate, signals_dict)

    Tier 1 Signals:
    - Recent drawdown % (5-15% from 20-day high)
    - Bollinger Band position (near lower band)
    - Volume spike on down days (>1.5x average)
    - Consecutive red days (3-5 days)
    - RSI (30-45 range)
    """
    try:
        # Fetch or use provided data
        if hist_df is None:
            ticker = yf.Ticker(symbol)
            hist_df = ticker.history(period="70d", interval="1d")

        if hist_df.empty or len(hist_df) < 30:
            return False, {"error": "Insufficient data"}

        signals = {}
        score = 0

        # 1. RSI Check (30-45 = oversold but not extreme)
        if len(hist_df) >= 14:
            rsi_indicator = RSIIndicator(close=hist_df['Close'], window=14)
            rsi = rsi_indicator.rsi().iloc[-1]
            signals['rsi'] = float(rsi)
            if 30 <= rsi <= 45:
                score += 3
                signals['rsi_signal'] = "Strong oversold"
            elif rsi < 50:
                score += 1
                signals['rsi_signal'] = "Mild oversold"
            else:
                signals['rsi_signal'] = "Not oversold"

        # 2. Recent Drawdown (8-15% from 20-day high is ideal)
        if len(hist_df) >= 20:
            high_20d = hist_df['Close'][-20:].max()
            current_price = hist_df['Close'].iloc[-1]
            drawdown_pct = ((current_price - high_20d) / high_20d) * 100
            signals['drawdown_pct'] = round(drawdown_pct, 2)
            signals['high_20d'] = round(high_20d, 2)

            if -15 <= drawdown_pct <= -8:
                score += 3
                signals['drawdown_signal'] = "Ideal pullback"
            elif -20 <= drawdown_pct <= -5:
                score += 2
                signals['drawdown_signal'] = "Good pullback"
            elif drawdown_pct < -20:
                signals['drawdown_signal'] = "Too steep (falling knife?)"
            else:
                signals['drawdown_signal'] = "No significant pullback"

        # 3. Bollinger Bands (near lower band = oversold)
        if len(hist_df) >= 20:
            sma_20 = hist_df['Close'].rolling(window=20).mean().iloc[-1]
            std_20 = hist_df['Close'].rolling(window=20).std().iloc[-1]
            bb_lower = sma_20 - (2 * std_20)
            bb_upper = sma_20 + (2 * std_20)
            current_price = hist_df['Close'].iloc[-1]

            bb_position = ((current_price - bb_lower) / (bb_upper - bb_lower)) * 100
            signals['bb_position_pct'] = round(bb_position, 1)

            if bb_position <= 20:
                score += 3
                signals['bb_signal'] = "At/below lower band"
            elif bb_position <= 35:
                score += 2
                signals['bb_signal'] = "Near lower band"
            else:
                signals['bb_signal'] = "Not near lower band"

        # 4. Consecutive Red Days (3-5 is ideal)
        consecutive_red = 0
        for i in range(len(hist_df)-1, max(len(hist_df)-10, 0), -1):
            if hist_df['Close'].iloc[i] < hist_df['Close'].iloc[i-1]:
                consecutive_red += 1
            else:
                break
        signals['consecutive_red_days'] = consecutive_red

        if 3 <= consecutive_red <= 5:
            score += 3
            signals['red_days_signal'] = "Ideal selling exhaustion"
        elif consecutive_red >= 2:
            score += 1
            signals['red_days_signal'] = "Some weakness"
        elif consecutive_red >= 6:
            signals['red_days_signal'] = "Extended weakness (caution)"
        else:
            signals['red_days_signal'] = "No selling pressure"

        # 5. Volume Analysis (spike on down days = capitulation)
        if len(hist_df) >= 20:
            avg_volume_20 = hist_df['Volume'][-20:].mean()
            recent_volume = hist_df['Volume'].iloc[-1]
            volume_ratio = recent_volume / avg_volume_20 if avg_volume_20 > 0 else 0
            signals['volume_ratio'] = round(volume_ratio, 2)

            # Check if last day was down
            last_day_down = hist_df['Close'].iloc[-1] < hist_df['Close'].iloc[-2]

            if last_day_down and volume_ratio >= 1.5:
                score += 3
                signals['volume_signal'] = "High volume capitulation"
            elif volume_ratio >= 1.3:
                score += 1
                signals['volume_signal'] = "Above avg volume"
            else:
                signals['volume_signal'] = "Normal volume"

        # Final scoring (max 15 points)
        signals['total_score'] = score
        signals['max_score'] = 15

        # Require at least 7/15 points to be a good candidate
        is_good = score >= 7

        if is_good:
            signals['verdict'] = f"Strong rebound candidate ({score}/15)"
        elif score >= 5:
            signals['verdict'] = f"Moderate candidate ({score}/15)"
        else:
            signals['verdict'] = f"Weak candidate ({score}/15)"

        return is_good, signals

    except Exception as e:
        print(f"   Rebound check error for {symbol}: {e}")
        return False, {"error": str(e)}

def check_quality_filters(symbol, hist_df=None, current_iv=None):
    """
    Additional quality/risk filters for CSP opportunities.

    Checks:
    1. Earnings date (avoid within 14 days)
    2. IV vs HV (prefer high IV premium)
    3. 52-week low distance (avoid falling knives)
    4. MACD signal (momentum turning positive)
    5. Relative strength vs SPY (stock-specific weakness)

    Returns: (passes_filters, quality_signals_dict)
    """
    try:
        if hist_df is None:
            ticker = yf.Ticker(symbol)
            hist_df = ticker.history(period="1y", interval="1d")
        else:
            ticker = yf.Ticker(symbol)

        if hist_df.empty or len(hist_df) < 30:
            return False, {"error": "Insufficient data"}

        signals = {}
        warnings = []

        # 1. Earnings Date Check
        try:
            calendar = ticker.calendar
            if calendar is not None and 'Earnings Date' in calendar:
                earnings_dates = calendar['Earnings Date']
                if isinstance(earnings_dates, list) and len(earnings_dates) > 0:
                    next_earnings = pd.to_datetime(earnings_dates[0])
                    days_to_earnings = (next_earnings - pd.Timestamp.now()).days
                    signals['days_to_earnings'] = days_to_earnings

                    if days_to_earnings < 14:
                        warnings.append(f"Earnings in {days_to_earnings} days")
                        signals['earnings_warning'] = True
                    else:
                        signals['earnings_warning'] = False
                else:
                    signals['days_to_earnings'] = None
                    signals['earnings_warning'] = False
            else:
                signals['days_to_earnings'] = None
                signals['earnings_warning'] = False
        except Exception:
            signals['days_to_earnings'] = None
            signals['earnings_warning'] = False

        # 2. IV vs HV (if IV provided)
        if current_iv and len(hist_df) >= 20:
            returns = hist_df['Close'].pct_change().dropna()
            hv_20 = returns[-20:].std() * np.sqrt(252) * 100  # Annualized HV
            signals['hv_20'] = round(hv_20, 1)
            signals['iv'] = round(current_iv, 1)

            iv_premium_pct = ((current_iv - hv_20) / hv_20) * 100 if hv_20 > 0 else 0
            signals['iv_premium_pct'] = round(iv_premium_pct, 1)

            if iv_premium_pct >= 20:
                signals['iv_signal'] = "High IV premium (good)"
            elif iv_premium_pct >= 0:
                signals['iv_signal'] = "Moderate IV premium"
            else:
                signals['iv_signal'] = "IV below HV (poor)"

            # IV Rank (normalized 0-100 relative to 52-week HV range)
            try:
                if len(hist_df) >= 252:
                    history_for_rank = hist_df
                else:
                    history_for_rank = ticker.history(period="1y", interval="1d")
                hv_series = history_for_rank['Close'].pct_change().dropna().rolling(20).std() * np.sqrt(252) * 100
                hv_52w_high = hv_series.max()
                hv_52w_low = hv_series.min()
                if hv_52w_high > hv_52w_low:
                    iv_rank = ((current_iv - hv_52w_low) / (hv_52w_high - hv_52w_low)) * 100
                    signals['iv_rank'] = round(max(0, min(100, iv_rank)), 1)
                else:
                    signals['iv_rank'] = 50
            except Exception:
                signals['iv_rank'] = 50

        # 3. Distance from 52-Week Low
        low_52w = hist_df['Close'].min()
        current_price = hist_df['Close'].iloc[-1]
        distance_from_low_pct = ((current_price - low_52w) / low_52w) * 100
        signals['distance_from_52w_low_pct'] = round(distance_from_low_pct, 1)

        if distance_from_low_pct < 20:
            warnings.append(f"Only {distance_from_low_pct:.0f}% above 52w low")
            signals['low_distance_warning'] = True
        else:
            signals['low_distance_warning'] = False

        # 4. MACD Signal
        if len(hist_df) >= 35:
            macd_indicator = MACDIndicator(close=hist_df['Close'], window_slow=26, window_fast=12, window_sign=9)
            macd_line = macd_indicator.macd()
            signal_line = macd_indicator.macd_signal()
            macd_hist = macd_indicator.macd_diff()

            if not macd_line.empty and not signal_line.empty:
                current_macd = macd_line.iloc[-1]
                current_signal = signal_line.iloc[-1]
                current_hist = macd_hist.iloc[-1]
                prev_hist = macd_hist.iloc[-2] if len(macd_hist) >= 2 else 0

                signals['macd'] = round(current_macd, 3)
                signals['macd_signal'] = round(current_signal, 3)
                signals['macd_histogram'] = round(current_hist, 3)

                # Check for bullish crossover or positive momentum
                if current_macd > current_signal:
                    signals['macd_status'] = "Bullish (crossed over)"
                elif current_hist > prev_hist and current_hist > -0.5:
                    signals['macd_status'] = "Turning bullish"
                else:
                    signals['macd_status'] = "Bearish"

        # 5. Relative Strength vs SPY
        if len(hist_df) >= 5:
            try:
                spy = yf.Ticker("SPY").history(period="10d", interval="1d")
                if not spy.empty and len(spy) >= 5:
                    stock_return_5d = ((hist_df['Close'].iloc[-1] / hist_df['Close'].iloc[-5]) - 1) * 100
                    spy_return_5d = ((spy['Close'].iloc[-1] / spy['Close'].iloc[-5]) - 1) * 100
                    relative_strength = stock_return_5d - spy_return_5d

                    signals['stock_5d_return'] = round(stock_return_5d, 2)
                    signals['spy_5d_return'] = round(spy_return_5d, 2)
                    signals['relative_strength'] = round(relative_strength, 2)

                    if -7 <= relative_strength <= -2:
                        signals['rs_signal'] = "Ideal underperformance"
                    elif relative_strength < -7:
                        signals['rs_signal'] = "Severe underperformance"
                    else:
                        signals['rs_signal'] = "Not underperforming SPY"
            except Exception:
                pass

        # 6. 20-Day MA Slope
        if len(hist_df) >= 25:
            ma_20 = hist_df['Close'].rolling(window=20).mean()
            if not ma_20.empty and len(ma_20) >= 2:
                current_ma = ma_20.iloc[-1]
                prev_ma = ma_20.iloc[-5] if len(ma_20) >= 5 else ma_20.iloc[-2]
                ma_slope = ((current_ma / prev_ma) - 1) * 100

                signals['ma_20_slope'] = round(ma_slope, 2)

                if ma_slope > 0:
                    signals['ma_signal'] = "Uptrend"
                else:
                    signals['ma_signal'] = "Downtrend"

        # Overall assessment
        signals['warnings'] = warnings
        passes = len(warnings) == 0  # Pass if no critical warnings

        return passes, signals

    except Exception as e:
        print(f"   Quality filter error for {symbol}: {e}")
        return True, {"error": str(e)}  # Default to pass on error

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
        # Use string keys for JSON compatibility
        timeframes = {
            '1': 21,    # 1 month ≈ 21 trading days
            '3': 63,    # 3 months ≈ 63 trading days
            '6': 126,   # 6 months ≈ 126 trading days
            '12': 252   # 12 months ≈ 252 trading days
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
def find_high_probability_options(symbol, current_price, tier, regime, grok_sentiment, regime_key='MILD_BULL'):
    opportunities = []
    
    try:
        chain_resp = _get_schwab_client().get_option_chain(
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

        # Regime-dependent OTM buffer
        otm_buffer = OTM_BUFFER_BY_REGIME.get(regime_key, OTM_BUFFER_PCT_DEFAULT)

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

                    if strike > current_price * otm_buffer: continue

                    bid = float(opt.get('bidPrice', 0) or 0)
                    ask = float(opt.get('askPrice', 0) or 0)
                    mark = (bid + ask) / 2
                    last = float(opt.get('lastPrice', 0) or 0)
                    close = float(opt.get('closePrice', 0) or 0)

                    premium = bid if bid > 0 else mark if mark > 0 else last if last > 0 else close

                    if premium < MIN_PREMIUM: continue

                    # Liquidity filters
                    open_interest = int(opt.get('openInterest', 0) or 0)
                    total_volume = int(opt.get('totalVolume', 0) or 0)

                    # Require minimum liquidity
                    if open_interest < 100:
                        continue  # Skip illiquid options
                    if total_volume < 10 and open_interest < 500:
                        continue  # Low volume OK if high OI

                    # Bid-ask spread check
                    if bid > 0 and ask > 0:
                        spread_pct = ((ask - bid) / premium) * 100 if premium > 0 else 100
                        if spread_pct > 15:  # Skip if spread > 15% of premium
                            continue

                    delta = abs(float(opt.get('delta', 0) or 0))
                    if not (delta_min <= delta <= delta_max): continue

                    iv = float(opt.get('volatility', 0) or 0)
                    if iv < regime["iv_min"]:
                            continue

                    income = premium * 100
                    roi = (income / capital_needed) * 100
                    annualized = (roi / dte) * 365 if dte > 0 else 0

                    if annualized < MIN_ANNUALIZED: continue

                    # Calculate bid-ask spread percentage
                    spread_pct = ((ask - bid) / premium) * 100 if premium > 0 and bid > 0 and ask > 0 else 0

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
                        'tier': get_symbol_tier(symbol),
                        # Liquidity metrics
                        'open_interest': open_interest,
                        'volume': total_volume,
                        'bid_ask_spread_pct': round(spread_pct, 2),
                        'bid': bid,
                        'ask': ask
                    })
                except: continue

        opportunities.sort(key=lambda x: x['annualized_roi'], reverse=True)
        return opportunities[:5]  # Keep top 5 per symbol
    except Exception as e:
        print(f"   Chain error {symbol}: {e}")
        return []

def improved_put_score(premium, delta, dte, annualized_roi, iv, vol_surge, rsi, in_uptrend, distance_pct=0, tier=3, capital=0, iv_rank=50, sr_risk_flag="Neutral", regime="MILD_BULL", sr_mult=1.0, sector_score=50, **kwargs):
    """
    Updated pre-Grok score with S/R bonus, vol surge, and NEW quality filters.

    New additions:
    - rebound_score: 0-15 score for rebound signals
    - quality_signals: earnings, IV premium, 52w low, MACD, RS
    - liquidity: OI, volume, spread
    """
    # Extract new signals from kwargs
    rebound_score = kwargs.get('rebound_score', 0)
    quality_signals = kwargs.get('quality_signals', {})
    open_interest = kwargs.get('open_interest', 0)
    bid_ask_spread_pct = kwargs.get('bid_ask_spread_pct', 0)

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

    # NEW: IV Premium bonus (IV vs HV)
    iv_premium_pct = quality_signals.get('iv_premium_pct', 0)
    if iv_premium_pct >= 30:
        iv_mult *= 1.15  # Very overpriced options
    elif iv_premium_pct >= 20:
        iv_mult *= 1.10  # Good IV premium
    elif iv_premium_pct < 0:
        iv_mult *= 0.95  # IV below HV (poor)
    
    # DTE multiplier — sweet spot is 30-45 DTE (fastest useful theta decay)
    if dte < 14:
        dte_mult = 0.7   # Strong penalty — too short, high gamma risk
    elif dte < 21:
        dte_mult = 0.9   # Mild penalty — too short for wheel safety
    elif 21 <= dte <= 30:
        dte_mult = 1.15  # Good — safe entry zone
    elif 30 <= dte <= 45:
        dte_mult = 1.35  # BEST — ideal wheel sweet spot
    elif 45 < dte <= 50:
        dte_mult = 1.0   # Acceptable for bearish regime only — slower theta
    else:  # >50 DTE
        dte_mult = 0.85  # Penalize — ties up capital too long

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

    # NEW: Rebound score bonus (0-15 scale)
    rebound_mult = 1.0
    if rebound_score >= 10:
        rebound_mult = 1.20  # Strong rebound signals
    elif rebound_score >= 7:
        rebound_mult = 1.10  # Good rebound signals
    elif rebound_score >= 4:
        rebound_mult = 1.05  # Moderate signals

    # NEW: Quality warnings penalties
    quality_mult = 1.0
    warnings = quality_signals.get('warnings', [])
    if warnings:
        # Earnings warning is critical
        if quality_signals.get('earnings_warning'):
            quality_mult *= 0.75  # 25% penalty for upcoming earnings
        # 52w low warning
        if quality_signals.get('low_distance_warning'):
            quality_mult *= 0.85  # 15% penalty for falling knife risk

    # NEW: Liquidity bonus/penalty
    liquidity_mult = 1.0
    if open_interest >= 1000:
        liquidity_mult = 1.10  # Excellent liquidity
    elif open_interest >= 500:
        liquidity_mult = 1.05  # Good liquidity
    elif open_interest < 100:
        liquidity_mult = 0.90  # Poor liquidity

    if bid_ask_spread_pct > 10:
        liquidity_mult *= 0.95  # Wide spread penalty
    elif bid_ask_spread_pct < 5:
        liquidity_mult *= 1.05  # Tight spread bonus

    # NEW: MACD momentum bonus
    macd_status = quality_signals.get('macd_status', '')
    macd_mult = 1.0
    if 'Bullish' in macd_status and 'crossed' in macd_status:
        macd_mult = 1.08  # Strong bullish crossover
    elif 'Turning bullish' in macd_status:
        macd_mult = 1.04  # About to cross

    # NEW: Relative strength bonus (underperforming SPY is good for CSP)
    rs = quality_signals.get('relative_strength', 0)
    rs_mult = 1.0
    if -7 <= rs <= -2:
        rs_mult = 1.08  # Ideal underperformance
    elif rs < -7:
        rs_mult = 0.95  # Too weak

    # Sector momentum multiplier (from sector_sentiment.py scoring)
    if sector_score >= 70:
        sector_mult = 1.12   # Strong Bull sector — tailwind
    elif sector_score >= 55:
        sector_mult = 1.06
    elif sector_score >= 40:
        sector_mult = 1.0
    elif sector_score >= 25:
        sector_mult = 0.94
    else:
        sector_mult = 0.88   # Avoid-ranked sector — headwind

    # IV Rank multiplier — reward selling when options are historically expensive
    iv_rank_val = kwargs.get('iv_rank_val', iv_rank)  # also accept from kwargs
    if iv_rank_val >= 80:
        iv_rank_mult = 1.12  # Top-decile IV — great time to sell vol
    elif iv_rank_val >= 60:
        iv_rank_mult = 1.06
    elif iv_rank_val >= 40:
        iv_rank_mult = 1.0
    elif iv_rank_val >= 20:
        iv_rank_mult = 0.95
    else:
        iv_rank_mult = 0.90  # Historically cheap vol — avoid

    # VIX term structure multiplier (vix_ratio = VIX / VIX3M)
    # >1 = backwardation/fear (penalize), <0.85 = deep contango/calm (reward)
    vix_term_ratio = kwargs.get('vix_term_ratio', 1.0)
    if vix_term_ratio < 0.85:
        vix_term_mult = 1.08   # Deep contango — calm market, good for CSPs
    elif vix_term_ratio > 1.05:
        vix_term_mult = 0.93   # Backwardation — elevated fear, increase caution
    else:
        vix_term_mult = 1.0    # Neutral term structure

    # Apply all multipliers
    score = (base * iv_mult * dte_mult * vol_mult * rsi_mult * trend_mult *
             distance_mult * tier_mult * sr_mult * regime_mult * capital_mult *
             rebound_mult * quality_mult * liquidity_mult * macd_mult * rs_mult *
             sector_mult * iv_rank_mult * vix_term_mult)

    # Safety-first: score is pure multiplier output, annualized ROI is a minor bonus
    # (was: score * 8 + annualized_roi * 2) / 2 — gave too much weight to raw yield)
    final_score = score * 5 + min(annualized_roi * 0.3, 15)
    return final_score

# ==================== MAIN ====================
async def analyze_symbol(symbol, regime, grok_sentiment):
    print(f"Checking {symbol:<6}", end=" ", flush=True)

    tier = get_symbol_tier(symbol)

    # Fetch data once for both checks
    try:
        ticker = yf.Ticker(symbol)
        hist_df = ticker.history(period="70d", interval="1d")
    except Exception as e:
        print(f"| Data fetch failed - Skipped")
        return None

    # Legacy RSI check
    is_safe, rsi, price = is_red_day(symbol)

    # New comprehensive rebound check
    is_rebound_candidate, rebound_signals = check_rebound_signals(symbol, hist_df)

    if not is_safe:
        print(f"| RSI {rsi:.1f} (Too Hot) - Skipped")
        return None

    # Display rebound score
    rebound_score = rebound_signals.get('total_score', 0)
    print(f"| RSI {rsi:.1f} | Rebound {rebound_score}/15 | Tier {tier}", end=" ")

    opps = find_high_probability_options(symbol, price, tier, regime, grok_sentiment)
    if opps:
        print(f"| FOUND {len(opps)}")
        for o in opps:
            o['rsi'] = rsi
            o['regime'] = regime['name']
            o['grok_sentiment'] = grok_sentiment
            # Add rebound signals to opportunity data
            o['rebound_score'] = rebound_score
            o['rebound_signals'] = rebound_signals
            o['is_rebound_candidate'] = is_rebound_candidate
        
        return opps
    else:
        print("| No Valid Contracts")
        return None
    

async def main():
    global captured_opportunities

    print("\n" + "="*70)
    print("STARTING Simple Options SCANNER")
    print("="*70)

    _regime_result = await get_current_regime()
    regime_key = _regime_result["regime"]
    vix_ratio = _regime_result["vix_ratio"]
    regime = REGIME_SETTINGS[regime_key]
    grok_sentiment, grok_summary = get_grok_sentiment_cached()

    # Log regime change (only writes if it actually changed)
    try:
        from trade_outcome_tracker import log_regime_change
        log_regime_change(regime_key)
    except Exception as e:
        print(f"   Regime log failed: {e}")

    # Fetch sector scores once for the entire scan run
    print("Fetching sector sentiment scores...")
    try:
        sector_scores = get_sector_scores()
        top_sectors = sorted(sector_scores.items(), key=lambda x: x[1]['score'], reverse=True)
        print("   Sector scores: " + " | ".join(f"{s}: {d['score']} ({d['label']})" for s, d in top_sectors[:5]))
    except Exception as e:
        print(f"   Sector scoring failed: {e}")
        sector_scores = {}
    
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
            # Fetch historical data once for both checks
            try:
                ticker = yf.Ticker(sym)
                hist_df = ticker.history(period="70d", interval="1d")
            except Exception:
                print(f" → Data fetch failed")
                return []

            # RSI check
            is_safe, rsi, price = is_red_day(sym)
            if not is_safe:
                return []

            # Rebound signals check
            is_rebound_candidate, rebound_signals = check_rebound_signals(sym, hist_df)

            # Quality filters check (earnings, IV, 52w low, MACD, RS)
            # We'll get IV from first opportunity later, for now pass None
            passes_quality, quality_signals = check_quality_filters(sym, hist_df, current_iv=None)

            # P1: Hard-block trades with earnings < 7 DTE
            days_to_earnings = quality_signals.get('days_to_earnings')
            if days_to_earnings is not None and days_to_earnings < 7:
                print(f" → Earnings in {days_to_earnings} days — BLOCKED")
                return []

            opps = find_high_probability_options(sym, price, get_symbol_tier(sym), regime, grok_sentiment, regime_key=regime_key)

            if not opps:
                return []  # No opportunities → skip S/R fetch entirely

            sr_levels = calculate_support_resistance(sym)

            # Attach metadata to each opportunity
            rebound_score = rebound_signals.get('total_score', 0)

            # Update quality signals with actual IV from first opportunity
            if opps and 'iv' in opps[0]:
                passes_quality, quality_signals = check_quality_filters(sym, hist_df, current_iv=opps[0]['iv'])

            # Sector info for this symbol
            sym_sector = get_symbol_sector(sym)
            sym_sector_data = get_symbol_sector_score(sym, sector_scores)
            sym_sector_score = sym_sector_data.get('score', 50)
            sym_sector_label = sym_sector_data.get('label', 'Neutral')

            for o in opps:
                o['rsi'] = rsi
                o['support_resistance'] = sr_levels
                # Add rebound signals
                o['rebound_score'] = rebound_score
                o['rebound_signals'] = rebound_signals
                o['is_rebound_candidate'] = is_rebound_candidate
                # Add quality signals
                o['quality_signals'] = quality_signals
                o['passes_quality_filters'] = passes_quality
                # Sector sentiment
                o['sector'] = sym_sector
                o['sector_score'] = sym_sector_score
                o['sector_label'] = sym_sector_label
                # IV Rank from quality signals
                o['iv_rank'] = quality_signals.get('iv_rank', 50)

                sr_risk = "Neutral"
                sr = sr_levels.get('3', {})  # 3-month
                support_3m = sr.get('support')
                if support_3m is not None:
                    if o['strike'] < support_3m * 0.97:
                        sr_risk = "Low (well below 3m support)"
                    elif o['strike'] < support_3m * 1.03:
                        sr_risk = "Moderate (near 3m support)"
                    else:
                        sr_risk = "High (at/above 3m support)"
                o['sr_risk_flag'] = sr_risk

            warnings_str = f" ⚠️ {len(quality_signals.get('warnings', []))} warnings" if quality_signals.get('warnings') else ""
            status = f"Found {len(opps)} opps (Rebound: {rebound_score}/15{warnings_str})" if opps else "No qualifying puts"
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
        sr_risk_flag=x.get('sr_risk_flag', 'Neutral'),
        sector_score=x.get('sector_score', 50),
        # Quality & rebound signals
        rebound_score=x.get('rebound_score', 0),
        quality_signals=x.get('quality_signals', {}),
        open_interest=x.get('open_interest', 0),
        bid_ask_spread_pct=x.get('bid_ask_spread_pct', 0),
        vix_term_ratio=vix_ratio,
    ), reverse=True)

    # Sector concentration cap: max 2 recommendations per GICS sector
    MAX_PER_SECTOR = 2
    sector_counts: dict = {}
    capped_opps = []
    for opp in all_opps:
        opp_sector = opp.get('sector', get_symbol_sector(opp['symbol']))
        # ETFs and Unknown sectors are not capped
        if opp_sector in ('ETF', 'Unknown'):
            capped_opps.append(opp)
        elif sector_counts.get(opp_sector, 0) < MAX_PER_SECTOR:
            capped_opps.append(opp)
            sector_counts[opp_sector] = sector_counts.get(opp_sector, 0) + 1
    all_opps = capped_opps

    for i, opp in enumerate(all_opps):
        opp['global_rank'] = i + 1

    # Then, before sending individual alerts, add top 10 global:

    top_10_msg = "🌟 TOP 10 GLOBAL RANKED OPPORTUNITIES:\n\n"
    for i, opp in enumerate(all_opps[:10], 1):
        score = improved_put_score(
            premium=opp['premium'],
            delta=opp['delta'],
            dte=opp['dte'],
            annualized_roi=opp['annualized_roi'],
            iv=opp['iv'],
            vol_surge=opp.get('vol_surge', 0.5),
            rsi=opp.get('rsi', 50),
            in_uptrend=True,
            distance_pct=opp['distance'],
            tier=opp.get('tier', 3),
            capital=opp['capital'],
            iv_rank=opp.get('iv_rank', 50),
            sr_risk_flag=opp.get('sr_risk_flag', 'Neutral'),
            sector_score=opp.get('sector_score', 50),
            rebound_score=opp.get('rebound_score', 0),
            quality_signals=opp.get('quality_signals', {}),
            open_interest=opp.get('open_interest', 0),
            bid_ask_spread_pct=opp.get('bid_ask_spread_pct', 0),
            vix_term_ratio=vix_ratio,
        )
        top_10_msg += (
            f"#{i} {opp['symbol']} — {opp['dte']} DTE ${opp['strike']:.0f}P\n"
            f"Score: {score:.1f}/100 | Rebound: {opp.get('rebound_score', 0)}/15\n"
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

            if sr and '3' in sr:
                support_3m = sr['3'].get('support')
                if support_3m is not None:
                    strike = opp['strike']
                    if strike < support_3m * 0.97:
                        sr_risk = "Low (well below 3m support)"
                    elif strike < support_3m * 1.03:
                        sr_risk = "Moderate (near 3m support)"
                    else:
                        sr_risk = "High (at/above 3m support)"
            
            opp['sr_risk_flag'] = sr_risk

        # Build compact per-opportunity data for JSON prompt
        user_lines = [
            f"Regime: {regime['name']} | Sentiment: {grok_sentiment.replace('_', ' ')}",
            f"Score ALL {len(batch)} opportunities below and return them in the JSON array:\n",
        ]
        for idx, opp in enumerate(batch, 1):
            quality = opp.get('quality_signals', {})
            rebound = opp.get('rebound_signals', {})
            warnings = quality.get('warnings', [])
            extras = []
            if quality.get('days_to_earnings'):
                extras.append(f"earns={quality['days_to_earnings']}d")
            if quality.get('iv_premium_pct') is not None:
                extras.append(f"ivprem={quality['iv_premium_pct']}%")
            if quality.get('macd_status'):
                extras.append(f"macd={quality['macd_status']}")
            if quality.get('relative_strength') is not None:
                extras.append(f"vs_spy={quality['relative_strength']}%")
            if rebound.get('total_score'):
                extras.append(f"rebound={rebound['total_score']}/15")
            if opp.get('open_interest'):
                extras.append(f"oi={opp['open_interest']:,}")
            if opp.get('bid_ask_spread_pct'):
                extras.append(f"spread={opp['bid_ask_spread_pct']:.1f}%")
            extras_str = " | " + " | ".join(extras) if extras else ""
            warn_str = f" | WARN:{','.join(warnings)}" if warnings else ""
            user_lines.append(
                f"{idx}. {opp['symbol']} ${opp['strike']:.0f}P | DTE={opp['dte']} | prem=${opp['premium']:.2f}"
                f" | roi={opp['annualized_roi']:.1f}% | delta={opp['delta']:.2f} | iv={opp['iv']:.0f}%"
                f" | rsi={opp['rsi']:.1f} | otm={opp['distance']:.1f}% | tier={opp.get('tier', 3)}"
                f" | sr={opp.get('sr_risk_flag', '?')}{extras_str}{warn_str}"
            )
        user_content = "\n".join(user_lines)

        try:
            from grok_utils import call_grok, MODEL_FAST
            response = call_grok(
                [
                    {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                model=MODEL_FAST,
                max_tokens=2000,
                json_mode=True,
            ) or ""
        except Exception as e:
            print(f"Batch Grok call failed: {e}")
            response = ""

        # Parse JSON response; fall back gracefully on invalid JSON
        try:
            parsed_json = json.loads(response)
            opportunities_json = parsed_json.get("opportunities", [])
        except json.JSONDecodeError as e:
            logging.warning(f"[GROK batch {i//BATCH_SIZE + 1}] JSON parse failed: {e}. Raw: {response[:300]}")
            opportunities_json = []

        # Map JSON results back to opportunity dicts
        json_by_idx = {item.get("idx", j + 1): item for j, item in enumerate(opportunities_json)}

        for j, opp in enumerate(batch):
            item = json_by_idx.get(j + 1, {})
            score = int(item.get("score", 0))
            recommendation = str(item.get("recommendation", "Avoid")).strip()
            reason = str(item.get("reason", "Analysis failed")).strip()

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

        # (all opportunities handled in the loop above via json_by_idx fallback)

        await asyncio.sleep(1.5)

    top_opps = batched_results

    # === MID RE-SCORE: Re-score top 20 FAST scorers with MODEL_MID for higher accuracy ===
    _TOP_N_MID = 20
    top_by_fast = sorted(top_opps, key=lambda x: x.get('grok_trade_score', 0), reverse=True)
    mid_candidates = top_by_fast[:_TOP_N_MID]
    if mid_candidates:
        print(f"Re-scoring top {len(mid_candidates)} with MODEL_MID for higher-accuracy evaluation...")
        mid_lines = [
            f"Regime: {regime['name']} | Sentiment: {grok_sentiment.replace('_', ' ')}",
            f"Score ALL {len(mid_candidates)} opportunities below (deep analysis) and return them in the JSON array:\n",
        ]
        for idx, opp in enumerate(mid_candidates, 1):
            quality = opp.get('quality_signals', {})
            rebound = opp.get('rebound_signals', {})
            warnings = quality.get('warnings', [])
            extras = []
            if quality.get('days_to_earnings'):
                extras.append(f"earns={quality['days_to_earnings']}d")
            if quality.get('iv_premium_pct') is not None:
                extras.append(f"ivprem={quality['iv_premium_pct']}%")
            if quality.get('macd_status'):
                extras.append(f"macd={quality['macd_status']}")
            if quality.get('relative_strength') is not None:
                extras.append(f"vs_spy={quality['relative_strength']}%")
            if rebound.get('total_score'):
                extras.append(f"rebound={rebound['total_score']}/15")
            if opp.get('open_interest'):
                extras.append(f"oi={opp['open_interest']:,}")
            if opp.get('bid_ask_spread_pct'):
                extras.append(f"spread={opp['bid_ask_spread_pct']:.1f}%")
            extras_str = " | " + " | ".join(extras) if extras else ""
            warn_str = f" | WARN:{','.join(warnings)}" if warnings else ""
            mid_lines.append(
                f"{idx}. {opp['symbol']} ${opp['strike']:.0f}P | DTE={opp['dte']} | prem=${opp['premium']:.2f}"
                f" | roi={opp['annualized_roi']:.1f}% | delta={opp['delta']:.2f} | iv={opp['iv']:.0f}%"
                f" | rsi={opp['rsi']:.1f} | otm={opp['distance']:.1f}% | tier={opp.get('tier', 3)}"
                f" | sr={opp.get('sr_risk_flag', '?')} | fast_score={opp.get('grok_trade_score', 0)}"
                f"{extras_str}{warn_str}"
            )
        mid_content = "\n".join(mid_lines)
        try:
            from grok_utils import call_grok, MODEL_MID as _MODEL_MID
            mid_response = call_grok(
                [
                    {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": mid_content},
                ],
                model=_MODEL_MID,
                max_tokens=2000,
                json_mode=True,
            ) or ""
            mid_json_opps = json.loads(mid_response).get("opportunities", [])
            mid_by_idx = {item.get("idx", j + 1): item for j, item in enumerate(mid_json_opps)}
            for j, opp in enumerate(mid_candidates):
                item = mid_by_idx.get(j + 1, {})
                if item:
                    new_score = int(item.get("score", opp['grok_trade_score']))
                    new_rec = str(item.get("recommendation", opp['grok_recommendation'])).strip()
                    new_reason = str(item.get("reason", opp['grok_reason'])).strip()
                    new_badge = "⚠️"
                    if new_score >= 90: new_badge = "🔥"
                    elif new_score >= 80: new_badge = "🚀"
                    elif new_score >= 70: new_badge = "✅"
                    elif new_score >= 60: new_badge = "⚡"
                    opp.update({
                        'grok_trade_score': new_score,
                        'grok_profit_prob': f"{new_score}%",
                        'grok_one_liner': f"{new_rec} — {new_reason}",
                        'score_badge': new_badge,
                        'grok_recommendation': new_rec,
                        'grok_reason': new_reason,
                        'mid_scored': True,
                    })
            print(f"MODEL_MID re-score complete for {len(mid_candidates)} candidates.")
        except Exception as e:
            logging.warning(f"[MID re-score] Failed: {e}")

    # Final sort by Grok score
    top_opps.sort(key=lambda x: x.get('grok_trade_score', 0), reverse=True)

    # === FILTER: Only keep opportunities with Grok score >= 80 ===
    high_conviction_opps = [opp for opp in top_opps if opp.get('grok_trade_score', 0) >= 80]

    print(f"After filtering >=80: {len(high_conviction_opps)} high-conviction opportunities")

    if not high_conviction_opps:
        print("No opportunities scored 80+ — keeping top 10 for reference")
        high_conviction_opps = top_opps[:10]  # Small fallback; empty = don't trade today

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

    # Log recommendations to trade outcome tracker
    try:
        from trade_outcome_tracker import log_recommendations
        logged = log_recommendations(scanner_tiles, regime_key)
        print(f"📝 Logged {logged} recommendations to trade journal")
    except Exception as e:
        print(f"Trade journal logging failed: {e}")
    
    
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