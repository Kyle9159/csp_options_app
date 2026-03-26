import os
import requests
import json
import hashlib
import logging
import time
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# Grok API configuration
GROK_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

# Cache directory
CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)

# Model tiers — update these when xAI releases new models
# FAST: bulk filtering, sentiment, background scanner — cheapest
# MID: user-facing prose generation (detailed analysis tiles) — mid-cost, no reasoning overhead
# REASONING: final trade picks, deep quantitative analysis on demand — most expensive
MODEL_FAST = "grok-4-1-fast-reasoning"
MODEL_MID = "grok-4.20-0309-non-reasoning"
MODEL_REASONING = "grok-4.20-0309-reasoning"

# Daily token usage tracking (in-memory; resets on server restart)
_token_usage: dict = {"fast_in": 0, "fast_out": 0, "mid_in": 0, "mid_out": 0, "reasoning_in": 0, "reasoning_out": 0}


def call_grok(messages, model=MODEL_FAST, max_tokens=400, json_mode=False):
    """Public wrapper for _call_grok — use when constructing custom messages directly."""
    return _call_grok(messages, model=model, max_tokens=max_tokens, json_mode=json_mode)

# Cost per 1M tokens (USD) — update when pricing changes
_COST = {
    MODEL_FAST: {"in": 0.20, "out": 0.20},
    MODEL_MID: {"in": 2.00, "out": 2.00},
    MODEL_REASONING: {"in": 2.00, "out": 2.00},
}


def _tier_key(model: str) -> str:
    """Map model name to token tracking bucket."""
    if model == MODEL_FAST:
        return "fast"
    if model == MODEL_MID:
        return "mid"
    return "reasoning"


def _log_usage(model: str, usage: dict) -> None:
    """Track token consumption and log estimated cost."""
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    tier = _tier_key(model)
    _token_usage[f"{tier}_in"] += prompt_tokens
    _token_usage[f"{tier}_out"] += completion_tokens
    cost = (
        prompt_tokens / 1_000_000 * _COST.get(model, {}).get("in", 0)
        + completion_tokens / 1_000_000 * _COST.get(model, {}).get("out", 0)
    )
    logger.info(f"[GROK cost] model={model} in={prompt_tokens} out={completion_tokens} call_cost=${cost:.5f}")


def get_daily_token_cost() -> dict:
    """Return accumulated token usage and estimated daily cost."""
    def _tier_cost(tier: str, model: str) -> float:
        return (
            _token_usage[f"{tier}_in"] / 1_000_000 * _COST[model]["in"]
            + _token_usage[f"{tier}_out"] / 1_000_000 * _COST[model]["out"]
        )

    fast_cost = _tier_cost("fast", MODEL_FAST)
    mid_cost = _tier_cost("mid", MODEL_MID)
    reasoning_cost = _tier_cost("reasoning", MODEL_REASONING)
    return {
        "fast_tokens": _token_usage["fast_in"] + _token_usage["fast_out"],
        "mid_tokens": _token_usage["mid_in"] + _token_usage["mid_out"],
        "reasoning_tokens": _token_usage["reasoning_in"] + _token_usage["reasoning_out"],
        "fast_cost_usd": round(fast_cost, 4),
        "mid_cost_usd": round(mid_cost, 4),
        "reasoning_cost_usd": round(reasoning_cost, 4),
        "total_cost_usd": round(fast_cost + mid_cost + reasoning_cost, 4),
    }


def _call_grok(messages: list, model: str = MODEL_FAST, max_tokens: int = 300, json_mode: bool = False) -> str | None:
    """
    Central HTTP call with usage logging.
    Returns parsed text content or None on error.
    """
    if not GROK_API_KEY:
        return None
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    # Reasoning model may need more time to think
    timeout = 60 if model == MODEL_REASONING else 30

    try:
        response = requests.post(
            GROK_ENDPOINT,
            headers={"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if response.status_code == 200:
            data = response.json()
            if "usage" in data:
                _log_usage(model, data["usage"])
            return data["choices"][0]["message"]["content"]
        else:
            logger.error(f"[GROK error] status={response.status_code} body={response.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"[GROK error] {e}")
        return None


def get_grok_analysis(symbol, context="", use_reasoning=False):
    """
    Get Grok analysis for a symbol.
    use_reasoning=True triggers the expensive model (call only on user-facing requests).
    """
    if not GROK_API_KEY:
        return "Grok API key not configured"

    model = MODEL_REASONING if use_reasoning else MODEL_MID
    system = "You are a professional options trader specializing in the wheel strategy. Be concise and actionable."
    prompt = f"Analyze {symbol} for options trading." + (f" {context}" if context else "")
    result = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        model=model,
        max_tokens=400,
    )
    return result or f"Analysis unavailable for {symbol}"

def _read_cache(cache_file: Path, ttl_seconds: int = 3600) -> dict | None:
    """Read JSON cache file if it exists and is within TTL."""
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
        if time.time() - data.get("timestamp", 0) < ttl_seconds:
            return data
    except Exception:
        pass
    return None


def _write_cache(cache_file: Path, data: dict) -> None:
    try:
        record = {**data, "timestamp": time.time()}
        with open(cache_file, "w") as f:
            json.dump(record, f)
    except Exception:
        pass


def _extract_sentiment(text: str) -> str:
    t = text.lower()
    if "bullish" in t or "strong" in t:
        return "BULLISH"
    if "bearish" in t or "weak" in t:
        return "BEARISH"
    if "cautious" in t:
        return "CAUTIOUS"
    return "NEUTRAL"


def get_grok_sentiment_cached(symbol=None):
    """
    Get cached Grok sentiment analysis.
    Uses fast model (cheap) — sentiment is a bulk, repeated call.
    Cache TTL: 1 hour.
    """
    if symbol:
        cache_file = CACHE_DIR / f"grok_sentiment_{symbol}.json"
        cached = _read_cache(cache_file, ttl_seconds=3600)
        if cached:
            return cached.get("sentiment", "NEUTRAL"), cached.get("analysis", "Analysis unavailable")

        analysis = get_grok_analysis(symbol, "Focus on market sentiment and price direction.")
        sentiment = _extract_sentiment(analysis)
        _write_cache(cache_file, {"symbol": symbol, "sentiment": sentiment, "analysis": analysis})
        return sentiment, analysis
    else:
        cache_file = CACHE_DIR / "grok_market_sentiment.json"
        cached = _read_cache(cache_file, ttl_seconds=3600)
        if cached:
            return cached.get("sentiment", "NEUTRAL"), cached.get("analysis", "Market analysis unavailable")

        # Compressed prompt — fast model only
        system = "You are a market analyst. Reply in 3-4 sentences maximum."
        user = (
            "Analyze overall SPY market sentiment for cash-secured put opportunities today. "
            "Cover: market direction + VIX, CSP entry conditions, sectors with rebound potential, macro risks to avoid."
        )
        analysis = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=MODEL_FAST,
            max_tokens=200,
        ) or "Market analysis unavailable"
        sentiment = _extract_sentiment(analysis)
        _write_cache(cache_file, {"sentiment": sentiment, "analysis": analysis})
        return sentiment, analysis

def get_grok_opportunity_analysis(symbol, price, strike, dte, premium, delta, iv=30, rsi=50, vol_surge=1.0, in_uptrend=True, use_reasoning=False):
    """
    Get Grok analysis for a specific options opportunity.

    use_reasoning=False (default): fast cheap model — used during bulk scanner pass.
    use_reasoning=True: expensive reasoning model — call only for top picks shown to user.

    Returns:
        tuple: (probability_score 0-1, analysis_oneliner str)

    Cache: disk-backed, keyed by all inputs + date (good for the full trading day).
    """
    if not GROK_API_KEY:
        return 0.5, "Grok API not configured"

    # Build a deterministic cache key from all trade inputs + model tier + today's date
    model = MODEL_REASONING if use_reasoning else MODEL_FAST
    cache_key = hashlib.md5(
        f"{symbol}:{price:.2f}:{strike:.2f}:{dte}:{premium:.2f}:{delta:.2f}:{iv:.1f}:{rsi:.1f}:{vol_surge:.1f}:{in_uptrend}:{model}:{date.today()}".encode()
    ).hexdigest()
    cache_file = CACHE_DIR / f"grok_opp_{cache_key}.json"

    cached = _read_cache(cache_file, ttl_seconds=86400)  # full trading day
    if cached:
        return cached.get("prob", 0.5), cached.get("oneliner", "Cached analysis")

    # Compute derived metrics — give the model pre-calculated values so it reasons on facts, not vibes
    breakeven = round(strike - premium, 2)
    otm_pct = round(((price - strike) / price) * 100, 1) if price > 0 else 0
    ann_return = round((premium / strike) * (365 / max(dte, 1)) * 100, 1) if strike > 0 else 0
    cushion_pct = round(((price - breakeven) / price) * 100, 1) if price > 0 else 0

    system = (
        "You are a quantitative CSP (cash-secured put) analyst. "
        "Score the probability that this put expires worthless (seller keeps full premium). "
        "Use this rubric:\n"
        "- Delta magnitude: |delta| < 0.20 = very safe, 0.20-0.30 = standard, > 0.35 = aggressive\n"
        "- IV context: high IV (>40%) inflates premiums but signals risk; check RSI for oversold bounce\n"
        "- DTE: 21-45 days = optimal theta decay; < 14 = gamma risk; > 60 = capital drag\n"
        "- Trend: uptrend + RSI > 40 favors put sellers; downtrend + RSI < 30 = possible reversal entry\n"
        "- Cushion: breakeven distance > 10% = strong safety; < 5% = thin margin\n"
        "Return ONLY valid JSON, no markdown.\n"
        'Schema: {"prob_otm": <int 0-100>, "ann_return_pct": <float>, '
        '"risk_flag": <"LOW"|"MEDIUM"|"HIGH">, '
        '"thesis": <string max 25 words>, "action": <"SELL"|"SKIP">}'
    )
    trade = {
        "sym": symbol, "price": round(price, 2), "strike": round(strike, 2),
        "dte": dte, "premium": round(premium, 2), "delta": round(delta, 3),
        "iv_pct": round(iv, 1), "rsi": round(rsi, 1),
        "vol_surge_x": round(vol_surge, 1), "trend": "up" if in_uptrend else "down",
        "breakeven": breakeven, "otm_pct": otm_pct,
        "ann_return_pct": ann_return, "cushion_pct": cushion_pct,
    }
    user = f"Evaluate this CSP: {json.dumps(trade)}"

    content = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        max_tokens=150,
        json_mode=True,
    )

    try:
        parsed = json.loads(content or "{}")
        prob = max(0, min(100, int(parsed.get("prob_otm", parsed.get("prob_profit", 50))))) / 100.0
        thesis = str(parsed.get("thesis", "")).strip() or f"Score: {int(prob*100)}%"
        action = parsed.get("action", "")
        risk = parsed.get("risk_flag", "")
        # Build informative oneliner: [SELL|HIGH] Thesis text
        badges = []
        if action:
            badges.append(action)
        if risk:
            badges.append(risk)
        prefix = f"[{'|'.join(badges)}] " if badges else ""
        oneliner = f"{prefix}{thesis}"
    except Exception:
        prob_match = re.search(r'"prob_otm"\s*:\s*(\d+)', content or "")
        prob = int(prob_match.group(1)) / 100.0 if prob_match else 0.5
        oneliner = "Analysis parsing failed"

    _write_cache(cache_file, {"prob": prob, "oneliner": oneliner})
    return prob, oneliner


def get_grok_0dte_recommendation(symbol, underlying_price, short_put, short_call, put_credit, call_credit, use_reasoning=False):
    """
    Get Grok's recommendation for which side of a 0DTE iron condor to favor.

    use_reasoning=True: expensive model for high-conviction final picks.
    Default: fast model (0DTE is time-sensitive; cost matters more here).

    Cache: keyed by inputs + date (0DTE refreshes daily at open).

    Returns:
        dict: {'recommendation': 'SELL_PUT'|'SELL_CALL'|'NEUTRAL', 'confidence': 1-5, 'reasoning': str}
    """
    _neutral = {"recommendation": "NEUTRAL", "confidence": 1, "reasoning": "Grok API not configured"}
    if not GROK_API_KEY:
        return _neutral

    model = MODEL_REASONING if use_reasoning else MODEL_FAST
    cache_key = hashlib.md5(
        f"0dte:{symbol}:{underlying_price:.2f}:{short_put}:{short_call}:{put_credit:.2f}:{call_credit:.2f}:{model}:{date.today()}".encode()
    ).hexdigest()
    cache_file = CACHE_DIR / f"grok_0dte_{cache_key}.json"

    # 0DTE is intraday-sensitive — 30 min cache, not full day
    cached = _read_cache(cache_file, ttl_seconds=1800)
    if cached:
        return {
            "recommendation": cached.get("recommendation", "NEUTRAL"),
            "confidence": cached.get("confidence", 1),
            "reasoning": cached.get("reasoning", ""),
        }

    system = (
        "You are an intraday options analyst. Return ONLY valid JSON, no markdown. "
        'Schema: {"recommendation": "SELL_PUT"|"SELL_CALL"|"NEUTRAL", "confidence": <integer 1-5>, "reasoning": <string max 20 words>}'
    )
    trade = {
        "sym": symbol,
        "price": round(underlying_price, 2),
        "short_put": short_put,
        "short_call": short_call,
        "put_credit": round(put_credit, 2),
        "call_credit": round(call_credit, 2),
    }
    user = (
        f"0DTE iron condor: {json.dumps(trade)}. "
        "Recommend the safer side to sell based on current market sentiment, intraday momentum, and skew."
    )

    content = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        max_tokens=120,
        json_mode=True,
    )

    try:
        parsed = json.loads(content or "{}")
        rec = parsed.get("recommendation", "NEUTRAL").upper()
        if rec not in ("SELL_PUT", "SELL_CALL", "NEUTRAL"):
            rec = "NEUTRAL"
        conf = max(1, min(5, int(parsed.get("confidence", 3))))
        reasoning = str(parsed.get("reasoning", ""))[:150]
    except Exception:
        # Fallback regex on raw content
        rec_match = re.search(r'"recommendation"\s*:\s*"(SELL_PUT|SELL_CALL|NEUTRAL)"', content or "", re.IGNORECASE)
        rec = rec_match.group(1).upper() if rec_match else "NEUTRAL"
        conf_match = re.search(r'"confidence"\s*:\s*(\d)', content or "")
        conf = int(conf_match.group(1)) if conf_match else 3
        reason_match = re.search(r'"reasoning"\s*:\s*"([^"]+)"', content or "")
        reasoning = reason_match.group(1)[:150] if reason_match else "Parse error"

    result = {"recommendation": rec, "confidence": conf, "reasoning": reasoning}
    _write_cache(cache_file, result)
    return result

