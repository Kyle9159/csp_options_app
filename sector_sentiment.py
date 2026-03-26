"""
sector_sentiment.py — GICS Sector Momentum Scorer for CSP Strategy

Scores each GICS sector 0-100 using SPDR ETFs as proxies.
Higher score = better environment for selling cash-secured puts in that sector.

Components:
  - Relative 5d return vs SPY (0-30 pts)  — sector is outperforming or holding up
  - RSI 45-65 sweet spot (0-25 pts)       — not overbought or oversold
  - SMA20 slope (0-25 pts)                — short-term trend positive
  - Volume conviction (0-20 pts)          — trending with above-avg volume

Cache: 4 hours to cache_files/sector_sentiment_cache.json
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

logger = logging.getLogger(__name__)

CACHE_PATH = Path("cache_files") / "sector_sentiment_cache.json"
CACHE_TTL_HOURS = 4

# SPDR Sector ETF proxies (GICS standard)
SECTOR_ETFS: dict[str, str] = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Consumer Staples": "XLP",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Consumer Discretionary": "XLY",
}

# Symbol → GICS sector mapping covering the full scanner watchlist + extras
SYMBOL_TO_SECTOR: dict[str, str] = {
    # ── Technology ─────────────────────────────────────────────────────
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD": "Technology",  "AVGO": "Technology", "TSM": "Technology",
    "QCOM": "Technology", "TXN": "Technology",  "AMAT": "Technology",
    "LRCX": "Technology", "KLAC": "Technology", "MU": "Technology",
    "ASML": "Technology", "INTC": "Technology", "ON": "Technology",
    "ADI": "Technology",  "NXPI": "Technology", "MCHP": "Technology",
    "MRVL": "Technology", "SNPS": "Technology", "CDNS": "Technology",
    "CSCO": "Technology", "IBM": "Technology",  "ORCL": "Technology",
    "CRM": "Technology",  "NOW": "Technology",  "ADBE": "Technology",
    "SHOP": "Technology", "SNOW": "Technology", "DDOG": "Technology",
    "MDB": "Technology",  "NET": "Technology",  "ZS": "Technology",
    "CRWD": "Technology", "PANW": "Technology", "FTNT": "Technology",
    "S": "Technology",    "TEAM": "Technology", "WDAY": "Technology",
    "ZI": "Technology",   "GTLB": "Technology", "PLTR": "Technology",
    "IONQ": "Technology", "ARM": "Technology",  "DOCN": "Technology",
    "U": "Technology",    "TWLO": "Technology",
    # additional quality tech names
    "INTU": "Technology", "ANSS": "Technology", "PTC": "Technology",
    "EPAM": "Technology", "AKAM": "Technology", "SPLK": "Technology",

    # ── Financials ─────────────────────────────────────────────────────
    "JPM": "Financials",  "BAC": "Financials",  "WFC": "Financials",
    "USB": "Financials",  "PNC": "Financials",  "TFC": "Financials",
    "BLK": "Financials",  "GS": "Financials",   "MS": "Financials",
    "SCHW": "Financials", "AXP": "Financials",  "V": "Financials",
    "MA": "Financials",   "CFG": "Financials",  "FITB": "Financials",
    "KEY": "Financials",  "RF": "Financials",   "ZION": "Financials",
    "HBAN": "Financials", "MTB": "Financials",  "HOOD": "Financials",
    "SOFI": "Financials", "COIN": "Financials",
    # additional quality financials
    "KKR": "Financials",  "APO": "Financials",  "BX": "Financials",
    "CB": "Financials",   "MMC": "Financials",  "AON": "Financials",
    "CME": "Financials",  "ICE": "Financials",  "NDAQ": "Financials",
    "PYPL": "Financials", "SQ": "Financials",   "FIS": "Financials",
    "FI": "Financials",   "MKTX": "Financials",

    # ── Energy ─────────────────────────────────────────────────────────
    "XOM": "Energy",  "CVX": "Energy",  "COP": "Energy",
    "PSX": "Energy",  "MPC": "Energy",  "VLO": "Energy",
    "OXY": "Energy",  "HAL": "Energy",  "SLB": "Energy",
    "BP": "Energy",   "EOG": "Energy",  "DVN": "Energy",
    "FANG": "Energy", "LEU": "Energy",  "CCJ": "Energy",
    "XLE": "Energy",
    # additional energy names
    "WMB": "Energy",  "KMI": "Energy",  "ET": "Energy",
    "LNG": "Energy",  "RRC": "Energy",  "AR": "Energy",
    "MRO": "Energy",  "APA": "Energy",

    # ── Healthcare ─────────────────────────────────────────────────────
    "JNJ": "Healthcare",  "MRK": "Healthcare",  "BMY": "Healthcare",
    "ABBV": "Healthcare", "UNH": "Healthcare",  "CVS": "Healthcare",
    "CI": "Healthcare",   "HUM": "Healthcare",  "AMGN": "Healthcare",
    "GILD": "Healthcare", "PFE": "Healthcare",  "LLY": "Healthcare",
    "NVO": "Healthcare",  "AZN": "Healthcare",  "REGN": "Healthcare",
    "VRTX": "Healthcare", "BIIB": "Healthcare", "MRNA": "Healthcare",
    "BNTX": "Healthcare", "MCK": "Healthcare",  "CAH": "Healthcare",
    "ABC": "Healthcare",  "VEEV": "Healthcare", "HIMS": "Healthcare",
    # additional quality healthcare names
    "ELV": "Healthcare",  "MOH": "Healthcare",  "ABT": "Healthcare",
    "MDT": "Healthcare",  "TMO": "Healthcare",  "DHR": "Healthcare",
    "ISRG": "Healthcare", "EW": "Healthcare",   "BDX": "Healthcare",
    "ZBH": "Healthcare",  "BSX": "Healthcare",  "SYK": "Healthcare",

    # ── Industrials ────────────────────────────────────────────────────
    "CAT": "Industrials", "DE": "Industrials",  "HON": "Industrials",
    "MMM": "Industrials", "BA": "Industrials",  "RTX": "Industrials",
    "LMT": "Industrials", "GD": "Industrials",  "NOC": "Industrials",
    "GE": "Industrials",  "JCI": "Industrials", "EMR": "Industrials",
    "ITW": "Industrials", "UPS": "Industrials", "FDX": "Industrials",
    "ODFL": "Industrials","XPO": "Industrials", "JBHT": "Industrials",
    "BWXT": "Industrials","CEG": "Industrials",
    # additional quality industrials names
    "PH": "Industrials",  "ROK": "Industrials", "FAST": "Industrials",
    "CTAS": "Industrials","RSG": "Industrials", "WM": "Industrials",
    "NSC": "Industrials", "CSX": "Industrials", "UAL": "Industrials",
    "DAL": "Industrials", "LUV": "Industrials", "EXPD": "Industrials",

    # ── Communication Services ─────────────────────────────────────────
    "META": "Communication Services",  "GOOGL": "Communication Services",
    "NFLX": "Communication Services",  "DIS": "Communication Services",
    "RDDT": "Communication Services",  "ZM": "Communication Services",
    "PINS": "Communication Services",  "RBLX": "Communication Services",
    "DKNG": "Communication Services",
    # additional quality comm svcs names
    "CMCSA": "Communication Services", "CHTR": "Communication Services",
    "T": "Communication Services",     "TMUS": "Communication Services",
    "VZ": "Communication Services",    "TTWO": "Communication Services",
    "EA": "Communication Services",    "NWSA": "Communication Services",
    "WBD": "Communication Services",   "PARA": "Communication Services",

    # ── Consumer Staples ───────────────────────────────────────────────
    "KO": "Consumer Staples",  "PG": "Consumer Staples",
    "WMT": "Consumer Staples", "PEP": "Consumer Staples",
    "CL": "Consumer Staples",  "KMB": "Consumer Staples",
    "COST": "Consumer Staples","TGT": "Consumer Staples",
    "TJX": "Consumer Staples",
    # additional quality consumer staples
    "PM": "Consumer Staples",  "MO": "Consumer Staples",
    "MDLZ": "Consumer Staples","KHC": "Consumer Staples",
    "SYY": "Consumer Staples", "CAG": "Consumer Staples",
    "HSY": "Consumer Staples", "GIS": "Consumer Staples",
    "K": "Consumer Staples",   "TSN": "Consumer Staples",
    "CHD": "Consumer Staples", "CLX": "Consumer Staples",

    # ── Real Estate ────────────────────────────────────────────────────
    "O": "Real Estate",   "STAG": "Real Estate", "SPG": "Real Estate",
    # additional quality REITs
    "PLD": "Real Estate", "AMT": "Real Estate",  "EQIX": "Real Estate",
    "CCI": "Real Estate", "WELL": "Real Estate", "DLR": "Real Estate",
    "PSA": "Real Estate", "AVB": "Real Estate",  "EQR": "Real Estate",

    # ── Materials ─────────────────────────────────────────────────────
    "FCX": "Materials",  "CLF": "Materials",  "NEM": "Materials",
    "VALE": "Materials",
    # additional quality materials names
    "AA": "Materials",   "BHP": "Materials",  "RIO": "Materials",
    "SCCO": "Materials", "MP": "Materials",   "ALB": "Materials",
    "BALL": "Materials", "PKG": "Materials",  "IP": "Materials",
    "CF": "Materials",   "MOS": "Materials",  "LIN": "Materials",
    "APD": "Materials",  "ECL": "Materials",  "SHW": "Materials",

    # ── Utilities ─────────────────────────────────────────────────────
    "NEE": "Utilities",  "DUK": "Utilities",  "SO": "Utilities",
    "D": "Utilities",    "VST": "Utilities",  "NLR": "Utilities",
    "SMR": "Utilities",  "OKLO": "Utilities", "URA": "Utilities",
    "ENPH": "Utilities", "FSLR": "Utilities",
    # additional quality utilities names
    "AEP": "Utilities",  "EXC": "Utilities",  "XEL": "Utilities",
    "AWK": "Utilities",  "WEC": "Utilities",  "ES": "Utilities",
    "PPL": "Utilities",  "CMS": "Utilities",  "ETR": "Utilities",

    # ── Consumer Discretionary ────────────────────────────────────────
    "HD": "Consumer Discretionary",   "LOW": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",  "SBUX": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",  "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    "YUM": "Consumer Discretionary",  "QSR": "Consumer Discretionary",
    "DPZ": "Consumer Discretionary",  "WING": "Consumer Discretionary",
    "CAVA": "Consumer Discretionary", "ABNB": "Consumer Discretionary",
    "UBER": "Consumer Discretionary", "DASH": "Consumer Discretionary",
    "ROKU": "Consumer Discretionary", "MAR": "Consumer Discretionary",
    "RCL": "Consumer Discretionary",  "NCLH": "Consumer Discretionary",
    "CCL": "Consumer Discretionary",  "F": "Consumer Discretionary",
    "RIVN": "Consumer Discretionary", "GM": "Consumer Discretionary",
    "EXPE": "Consumer Discretionary",
    # additional quality consumer discr names
    "LULU": "Consumer Discretionary", "DECK": "Consumer Discretionary",
    "BURL": "Consumer Discretionary", "ROST": "Consumer Discretionary",
    "EBAY": "Consumer Discretionary", "BKNG": "Consumer Discretionary",
    "HLT": "Consumer Discretionary",  "H": "Consumer Discretionary",
    "WYNN": "Consumer Discretionary", "MGM": "Consumer Discretionary",
    "LVS": "Consumer Discretionary",  "BMW": "Consumer Discretionary",
}

_LABELS = {
    (75, 101): "Strong Bull",
    (55, 75):  "Bullish",
    (35, 55):  "Neutral",
    (15, 35):  "Bearish",
    (0,  15):  "Avoid",
}

def _label_from_score(score: float) -> str:
    for (lo, hi), label in _LABELS.items():
        if lo <= score < hi:
            return label
    return "Neutral"


def _score_sector(etf_ticker: str, spy_return_5d: float) -> dict:
    """
    Score a single GICS sector 0-100 using its SPDR ETF.

    Args:
        etf_ticker: SPDR ETF ticker (e.g. 'XLK')
        spy_return_5d: SPY 5-day return for relative comparison

    Returns:
        dict with keys: score (0-100), label, etf, components
    """
    try:
        import socket
        # Set a timeout for yfinance network calls (default is no timeout)
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)
        try:
            df = yf.Ticker(etf_ticker).history(period="60d", interval="1d")
        finally:
            socket.setdefaulttimeout(old_timeout)
        if df.empty or len(df) < 25:
            return {"score": 50, "label": "Neutral", "etf": etf_ticker, "components": {}, "error": "insufficient data"}

        score = 0
        components: dict[str, float] = {}

        # ── 1. Relative 5-day return vs SPY (0-30 pts) ────────────────
        if len(df) >= 5:
            etf_ret_5d = ((df["Close"].iloc[-1] / df["Close"].iloc[-5]) - 1) * 100
            rel_ret = etf_ret_5d - spy_return_5d
            components["relative_5d_return"] = round(rel_ret, 2)
            if rel_ret >= 3:
                score += 30
            elif rel_ret >= 1.5:
                score += 22
            elif rel_ret >= 0:
                score += 15
            elif rel_ret >= -1.5:
                score += 8
            else:
                score += 0

        # ── 2. RSI sweet spot 45-65 (0-25 pts) ───────────────────────
        if len(df) >= 14:
            rsi = RSIIndicator(close=df["Close"], window=14).rsi().iloc[-1]
            components["rsi"] = round(rsi, 1)
            if 45 <= rsi <= 65:
                score += 25  # Sweet spot: strong but not overbought
            elif 35 <= rsi < 45 or 65 < rsi <= 75:
                score += 15
            elif rsi < 35:
                score += 8   # Oversold — potential bounce but also continued weakness
            else:
                score += 0   # Overbought > 75

        # ── 3. SMA20 slope (0-25 pts) ─────────────────────────────────
        if len(df) >= 25:
            sma20 = SMAIndicator(close=df["Close"], window=20).sma_indicator()
            slope_pct = ((sma20.iloc[-1] / sma20.iloc[-5]) - 1) * 100 if sma20.iloc[-5] > 0 else 0
            components["sma20_slope_pct"] = round(slope_pct, 3)
            if slope_pct >= 0.5:
                score += 25  # Strong uptrend
            elif slope_pct >= 0.1:
                score += 18  # Mild uptrend
            elif slope_pct >= -0.1:
                score += 10  # Flat
            elif slope_pct >= -0.5:
                score += 4   # Mild downtrend
            else:
                score += 0   # Strong downtrend

        # ── 4. Volume conviction (0-20 pts) ───────────────────────────
        if len(df) >= 20:
            avg_vol = df["Volume"].iloc[-20:].mean()
            recent_vol = df["Volume"].iloc[-5:].mean()
            vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
            components["volume_ratio"] = round(vol_ratio, 2)

            # Positive if recent 5d price is up; negative if down
            price_direction = df["Close"].iloc[-1] > df["Close"].iloc[-5]
            if vol_ratio >= 1.3 and price_direction:
                score += 20  # High volume upward move — conviction
            elif vol_ratio >= 1.0 and price_direction:
                score += 13
            elif vol_ratio >= 1.3 and not price_direction:
                score += 5   # High volume sell-off — caution
            else:
                score += 8

        score = max(0, min(100, int(score)))
        return {
            "score": score,
            "label": _label_from_score(score),
            "etf": etf_ticker,
            "components": components,
        }

    except Exception as e:
        logger.warning("sector score failed for %s: %s", etf_ticker, e)
        return {"score": 50, "label": "Neutral", "etf": etf_ticker, "components": {}, "error": str(e)}


def get_sector_scores(force_refresh: bool = False) -> dict[str, dict]:
    """
    Return scored dicts for all 11 GICS sectors.

    Results are cached to disk for CACHE_TTL_HOURS hours to avoid excessive
    yfinance calls on every scanner run.

    Args:
        force_refresh: Skip cache and re-score all sectors.

    Returns:
        {sector_name: {"score": int, "label": str, "etf": str, "components": dict}}
    """
    # ── Check cache ───────────────────────────────────────────────────
    if not force_refresh and CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            cached_at = datetime.fromisoformat(cached.get("_cached_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_HOURS):
                logger.debug("sector_sentiment cache hit (age %s)", datetime.now() - cached_at)
                # Defensive: ensure structure is valid
                scores = {k: v for k, v in cached.items() if not k.startswith("_")}
                if all(isinstance(v, dict) and 'score' in v for v in scores.values()):
                    return scores
                logger.warning("sector_sentiment cache invalid structure: %r", scores)
        except Exception as e:
            logger.warning("sector cache read failed: %s", e)

    # ── Fetch SPY for relative return baseline ─────────────────────────
    spy_return_5d = 0.0
    try:
        spy_df = yf.Ticker("SPY").history(period="10d", interval="1d")
        if len(spy_df) >= 5:
            spy_return_5d = ((spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-5]) - 1) * 100
    except Exception as e:
        logger.warning("SPY fetch failed for sector sentinel: %s", e)

    # ── Score all sectors ─────────────────────────────────────────────
    scores: dict[str, dict] = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            scores[sector] = _score_sector(etf, spy_return_5d)
            logger.debug("  %s (%s): %d — %s", sector, etf, scores[sector]["score"], scores[sector]["label"])
        except Exception as e:
            logger.warning("Failed to score sector %s: %s", sector, e)
            scores[sector] = {"score": 50, "label": "Neutral", "etf": etf, "components": {}, "error": str(e)}

    # ── Persist cache ─────────────────────────────────────────────────
    try:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump({**scores, "_cached_at": datetime.now().isoformat()}, f, indent=2)
    except Exception as e:
        logger.warning("sector cache write failed: %s", e)

    return scores


def get_symbol_sector(symbol: str) -> str:
    """Return the GICS sector for a symbol, or 'Unknown' / 'ETF'."""
    etf_tickers = {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "SCHD", "JEPI", "JEPQ",
                   "XLK", "XLF", "XLE", "XLV", "XLI", "XLC", "XLP", "XLRE", "XLB", "XLU", "XLY",
                   "VXX", "NLR", "URA"}
    if symbol.upper() in etf_tickers:
        return "ETF"
    return SYMBOL_TO_SECTOR.get(symbol.upper(), "Unknown")


def get_symbol_sector_score(symbol: str, sector_scores: dict[str, dict]) -> dict:
    """
    Return the sector score dict for a given symbol.

    Args:
        symbol: Ticker symbol
        sector_scores: Output from get_sector_scores()

    Returns:
        Score dict with keys: score, label, etf, components.
        Returns a neutral dict if sector is unknown or ETF.
    """
    sector = get_symbol_sector(symbol)
    if sector in ("ETF", "Unknown"):
        return {"score": 50, "label": "Neutral", "etf": "N/A", "components": {}}
    return sector_scores.get(sector, {"score": 50, "label": "Neutral", "etf": "N/A", "components": {}})
