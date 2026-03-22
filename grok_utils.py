import os
import requests
import json
import hashlib
import time
import re
from datetime import date
from pathlib import Path

# Grok API configuration
GROK_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

# Cache directory
CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)

# Model tiers
# FAST: bulk filtering, sentiment, background scanner — $0.20/1M tokens
# REASONING: final trade picks, on-demand deep analysis — $2.00/1M tokens (10x)
MODEL_FAST = "grok-3-mini-fast"
MODEL_REASONING = "grok-3"  # upgrade to grok-4.2 when available on xAI API

# Daily token usage tracking (in-memory; resets on server restart)
_token_usage: dict = {"fast_in": 0, "fast_out": 0, "reasoning_in": 0, "reasoning_out": 0}

# Cost per 1M tokens (USD)
_COST = {MODEL_FAST: {"in": 0.20, "out": 0.20}, MODEL_REASONING: {"in": 2.00, "out": 2.00}}


def _log_usage(model: str, usage: dict) -> None:
    """Track token consumption and log estimated cost."""
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if model == MODEL_FAST:
        _token_usage["fast_in"] += prompt_tokens
        _token_usage["fast_out"] += completion_tokens
    else:
        _token_usage["reasoning_in"] += prompt_tokens
        _token_usage["reasoning_out"] += completion_tokens
    cost = (
        prompt_tokens / 1_000_000 * _COST.get(model, {}).get("in", 0)
        + completion_tokens / 1_000_000 * _COST.get(model, {}).get("out", 0)
    )
    print(f"[GROK cost] model={model} in={prompt_tokens} out={completion_tokens} call_cost=${cost:.5f}")


def get_daily_token_cost() -> dict:
    """Return accumulated token usage and estimated daily cost."""
    fast_cost = (
        _token_usage["fast_in"] / 1_000_000 * _COST[MODEL_FAST]["in"]
        + _token_usage["fast_out"] / 1_000_000 * _COST[MODEL_FAST]["out"]
    )
    reasoning_cost = (
        _token_usage["reasoning_in"] / 1_000_000 * _COST[MODEL_REASONING]["in"]
        + _token_usage["reasoning_out"] / 1_000_000 * _COST[MODEL_REASONING]["out"]
    )
    return {
        "fast_tokens": _token_usage["fast_in"] + _token_usage["fast_out"],
        "reasoning_tokens": _token_usage["reasoning_in"] + _token_usage["reasoning_out"],
        "fast_cost_usd": round(fast_cost, 4),
        "reasoning_cost_usd": round(reasoning_cost, 4),
        "total_cost_usd": round(fast_cost + reasoning_cost, 4),
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

    try:
        response = requests.post(
            GROK_ENDPOINT,
            headers={"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            if "usage" in data:
                _log_usage(model, data["usage"])
            return data["choices"][0]["message"]["content"]
        else:
            print(f"[GROK error] status={response.status_code} body={response.text[:200]}")
            return None
    except Exception as e:
        print(f"[GROK error] {e}")
        return None


def get_grok_analysis(symbol, context="", use_reasoning=False):
    """
    Get Grok analysis for a symbol.
    use_reasoning=True triggers the expensive model (call only on user-facing requests).
    """
    if not GROK_API_KEY:
        return "Grok API key not configured"

    model = MODEL_REASONING if use_reasoning else MODEL_FAST
    prompt = f"Analyze {symbol} for options trading. {context}"
    result = _call_grok([{"role": "user", "content": prompt}], model=model, max_tokens=400)
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
        data["timestamp"] = time.time()
        with open(cache_file, "w") as f:
            json.dump(data, f)
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

    # Compressed JSON input prompt — both models use same prompt; reasoning model just reasons deeper
    system = (
        "You are a quantitative options analyst. Return ONLY valid JSON, no markdown. "
        "Schema: {\"prob_profit\": <integer 0-100>, \"thesis\": <string max 15 words>, \"action\": <\"SELL\" or \"SKIP\">}"
    )
    trade = {
        "sym": symbol, "price": round(price, 2), "strike": round(strike, 2),
        "dte": dte, "premium": round(premium, 2), "delta": round(delta, 3),
        "iv_pct": round(iv, 1), "rsi": round(rsi, 1),
        "vol_surge_x": round(vol_surge, 1), "trend": "up" if in_uptrend else "down",
    }
    user = f"Evaluate this CSP trade: {json.dumps(trade)}"

    content = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        max_tokens=120,
        json_mode=True,
    )

    try:
        parsed = json.loads(content or "{}")
        prob = max(0, min(100, int(parsed.get("prob_profit", 50)))) / 100.0
        oneliner = str(parsed.get("thesis", "")).strip() or f"Score: {int(prob*100)}%"
        # Prepend action badge
        action = parsed.get("action", "")
        if action:
            oneliner = f"[{action}] {oneliner}"
    except Exception:
        # Fallback: try regex on raw content
        prob_match = re.search(r'"prob_profit"\s*:\s*(\d+)', content or "")
        prob = int(prob_match.group(1)) / 100.0 if prob_match else 0.5
        oneliner = "Analysis parsing failed"

    _write_cache(cache_file, {"prob": prob, "oneliner": oneliner})
    return prob, oneliner

def parse_grok_batch_response(response, batch_size):
    """
    Parse a batch response from Grok API into individual analysis blocks.

    Args:
        response: Raw response string from Grok
        batch_size: Number of opportunities in the batch

    Returns:
        list: List of analysis blocks, one per opportunity
    """
    if not response or not isinstance(response, str):
        return []

    try:
        blocks = []

        # First, try to split by "--- OPPORTUNITY N ---" markers (our prompt format)
        opp_pattern = r'---\s*OPPORTUNITY\s*\d+\s*---'
        if re.search(opp_pattern, response, re.IGNORECASE):
            # Split by OPPORTUNITY markers
            parts = re.split(opp_pattern, response, flags=re.IGNORECASE)
            # Filter out empty parts and strip whitespace
            blocks = [part.strip() for part in parts if part.strip()]

        # If that didn't work, try "--- END ---" markers
        elif "--- END ---" in response or "---END---" in response:
            # Split by END markers
            parts = re.split(r'---\s*END\s*---', response, flags=re.IGNORECASE)
            blocks = [part.strip() for part in parts if part.strip()]

        # Try splitting by numbered items (1., 2., etc.) with SCORE/RECOMMENDATION
        elif re.search(r'(?:^|\n)\s*\d+[\.\)]\s*(?:SCORE|RECOMMENDATION)', response, re.IGNORECASE | re.MULTILINE):
            parts = re.split(r'(?:^|\n)\s*\d+[\.\)]\s*', response, flags=re.MULTILINE)
            blocks = [part.strip() for part in parts if part.strip()]

        # Try splitting by "SCORE:" markers (each opportunity starts with SCORE:)
        elif response.upper().count("SCORE:") >= batch_size:
            parts = re.split(r'(?=SCORE:)', response, flags=re.IGNORECASE)
            blocks = [part.strip() for part in parts if part.strip()]

        # Fallback: split by double newlines or paragraph breaks
        else:
            parts = re.split(r'\n\s*\n', response)
            blocks = [part.strip() for part in parts if part.strip()]

        # Ensure we have the right number of blocks
        if len(blocks) < batch_size:
            # Pad with empty blocks if needed
            blocks.extend(["Analysis unavailable"] * (batch_size - len(blocks)))
        elif len(blocks) > batch_size:
            # Truncate if too many
            blocks = blocks[:batch_size]

        return blocks

    except Exception as e:
        print(f"Error parsing batch response: {e}")
        return ["Analysis failed"] * batch_size


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

    cached = _read_cache(cache_file, ttl_seconds=86400)
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

