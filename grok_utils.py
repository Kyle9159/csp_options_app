import os
import requests
import json
import hashlib
import time
import re
from pathlib import Path

# Grok API configuration
GROK_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

# Cache directory
CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)

def get_grok_analysis(symbol, context=""):
    """Get Grok analysis for a symbol"""
    if not GROK_API_KEY:
        return "Grok API key not configured"

    try:
        prompt = f"Analyze {symbol} for options trading. {context}"

        response = requests.post(
            GROK_ENDPOINT,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "grok-4-1-fast-reasoning",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            }
        )

        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            return f"Error: {response.status_code}"

    except Exception as e:
        return f"Error getting Grok analysis: {str(e)}"

def get_grok_sentiment_cached(symbol=None):
    """Get cached Grok sentiment analysis"""
    if symbol:
        # Symbol-specific sentiment
        cache_file = CACHE_DIR / f"grok_sentiment_{symbol}.json"

        # Check cache first
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    cached_data = json.load(f)
                    # Cache for 1 hour
                    if time.time() - cached_data.get('timestamp', 0) < 3600:
                        return cached_data.get('sentiment', 'NEUTRAL'), cached_data.get('analysis', 'Analysis unavailable')
            except:
                pass

        # Get fresh analysis
        analysis = get_grok_analysis(symbol, "Focus on market sentiment and price direction.")
        sentiment = "NEUTRAL"  # Simple sentiment extraction

        if "bullish" in analysis.lower() or "strong" in analysis.lower():
            sentiment = "BULLISH"
        elif "bearish" in analysis.lower() or "weak" in analysis.lower():
            sentiment = "BEARISH"
        elif "cautious" in analysis.lower():
            sentiment = "CAUTIOUS"

        # Cache result
        try:
            with open(cache_file, 'w') as f:
                json.dump({
                    'symbol': symbol,
                    'sentiment': sentiment,
                    'analysis': analysis,
                    'timestamp': time.time()
                }, f)
        except:
            pass

        return sentiment, analysis
    else:
        # General market sentiment
        cache_file = CACHE_DIR / "grok_market_sentiment.json"

        # Check cache first
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    cached_data = json.load(f)
                    # Cache for 1 hour
                    if time.time() - cached_data.get('timestamp', 0) < 3600:
                        return cached_data.get('sentiment', 'NEUTRAL'), cached_data.get('analysis', 'Market analysis unavailable')
            except:
                pass

        # Get fresh market analysis
        analysis = get_grok_analysis("SPY", "Analyze overall market sentiment, direction, and key drivers.")
        sentiment = "NEUTRAL"

        if "bullish" in analysis.lower() or "strong" in analysis.lower():
            sentiment = "BULLISH"
        elif "bearish" in analysis.lower() or "weak" in analysis.lower():
            sentiment = "BEARISH"
        elif "cautious" in analysis.lower():
            sentiment = "CAUTIOUS"

        # Cache result
        try:
            with open(cache_file, 'w') as f:
                json.dump({
                    'sentiment': sentiment,
                    'analysis': analysis,
                    'timestamp': time.time()
                }, f)
        except:
            pass

        return sentiment, analysis

def get_grok_opportunity_analysis(symbol, price, strike, dte, premium, delta, iv=30, rsi=50, vol_surge=1.0, in_uptrend=True):
    """
    Get Grok analysis for a specific options opportunity.

    Returns:
        tuple: (probability_score, analysis_oneliner)
    """
    if not GROK_API_KEY:
        return 0.5, "Grok API not configured"

    try:
        direction = "bullish" if in_uptrend else "bearish"
        prompt = f"""Analyze this options trade opportunity:
Symbol: {symbol}
Current Price: ${price:.2f}
Strike: ${strike:.2f}
Days to Expiration: {dte}
Premium: ${premium:.2f}
Delta: {delta:.2f}
IV: {iv:.1f}%
RSI: {rsi:.1f}
Volume Surge: {vol_surge:.1f}x
Trend: {direction}

Give me a probability score (0-100) of this trade being profitable, and a one-line analysis."""

        response = requests.post(
            GROK_ENDPOINT,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "grok-4-1-fast-reasoning",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150
            }
        )

        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']

            # Extract probability and analysis
            import re
            prob_match = re.search(r'(\d+)%', content)
            prob = int(prob_match.group(1)) / 100.0 if prob_match else 0.5

            # Remove probability from content for oneliner
            oneliner = re.sub(r'\d+%', '', content).strip()
            if not oneliner:
                oneliner = content[:100] + "..." if len(content) > 100 else content

            return prob, oneliner
        else:
            return 0.5, f"API Error: {response.status_code}"

    except Exception as e:
        return 0.5, f"Analysis failed: {str(e)}"

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
        # Split response by common delimiters
        blocks = []

        # Try to split by numbered items (1., 2., etc.)
        if re.search(r'\d+\.', response):
            parts = re.split(r'\d+\.\s*', response)
            blocks = [part.strip() for part in parts if part.strip()]
        else:
            # Split by double newlines or other delimiters
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

