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

        # Get fresh market analysis with focus on CSP opportunities
        analysis = get_grok_analysis("SPY", """Analyze overall market sentiment for cash-secured put opportunities.

In your 3-4 sentence analysis, discuss:
1. Current market direction and VIX/volatility levels
2. Whether this creates good CSP entry opportunities (oversold bounces, high IV premiums)
3. Sectors or stock types showing rebound potential (technical oversold, recent pullbacks)
4. Any earnings season or macro risks to avoid
5. Liquidity and execution considerations

Focus on actionable insights for selling puts on quality stocks at attractive prices.""")
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


def get_grok_0dte_recommendation(symbol, underlying_price, short_put, short_call, put_credit, call_credit):
    """
    Get Grok's recommendation for which side of a 0DTE iron condor to favor.

    Analyzes market sentiment, ticker-specific factors, and trade setup to recommend
    either 'SELL_PUT' (bullish bias) or 'SELL_CALL' (bearish bias).

    Returns:
        dict: {
            'recommendation': 'SELL_PUT' or 'SELL_CALL',
            'confidence': 1-5 (1=low, 5=high),
            'reasoning': brief explanation
        }
    """
    if not GROK_API_KEY:
        return {
            'recommendation': 'NEUTRAL',
            'confidence': 1,
            'reasoning': 'Grok API not configured'
        }

    try:
        prompt = f"""You are analyzing a 0DTE iron condor opportunity for {symbol}.

Current Setup:
- Underlying Price: ${underlying_price:.2f}
- Put Side: Sell ${short_put} put (credit: ~${put_credit:.2f})
- Call Side: Sell ${short_call} call (credit: ~${call_credit:.2f})

Analyze the following factors RIGHT NOW to determine which side is SAFER to sell:
1. Current market sentiment (SPY/QQQ direction today)
2. {symbol} specific news, earnings, or events
3. Intraday price momentum and trend
4. Relative strength vs market
5. Options flow and positioning (put/call skew)
6. Key support/resistance levels relative to strikes

RESPOND IN EXACTLY THIS FORMAT:
RECOMMENDATION: [SELL_PUT or SELL_CALL]
CONFIDENCE: [1-5]
REASONING: [One sentence, max 30 words explaining why this side is safer based on current conditions]

If market is neutral or unclear, pick the side with better risk/reward based on the credit received."""

        response = requests.post(
            GROK_ENDPOINT,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "grok-4-1-fast-reasoning",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200
            }
        )

        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']

            # Parse the structured response
            recommendation = 'NEUTRAL'
            confidence = 3
            reasoning = 'Analysis unavailable'

            # Extract recommendation
            rec_match = re.search(r'RECOMMENDATION:\s*(SELL_PUT|SELL_CALL|NEUTRAL)', content, re.IGNORECASE)
            if rec_match:
                recommendation = rec_match.group(1).upper()
            elif 'sell put' in content.lower() or 'bullish' in content.lower():
                recommendation = 'SELL_PUT'
            elif 'sell call' in content.lower() or 'bearish' in content.lower():
                recommendation = 'SELL_CALL'

            # Extract confidence
            conf_match = re.search(r'CONFIDENCE:\s*(\d)', content)
            if conf_match:
                confidence = int(conf_match.group(1))
                confidence = max(1, min(5, confidence))  # Clamp to 1-5

            # Extract reasoning
            reason_match = re.search(r'REASONING:\s*(.+?)(?:\n|$)', content, re.IGNORECASE)
            if reason_match:
                reasoning = reason_match.group(1).strip()
            else:
                # Fallback: use last sentence or portion
                sentences = content.split('.')
                if sentences:
                    reasoning = sentences[-2].strip() if len(sentences) > 1 else sentences[0].strip()

            return {
                'recommendation': recommendation,
                'confidence': confidence,
                'reasoning': reasoning[:150]  # Cap length
            }
        else:
            return {
                'recommendation': 'NEUTRAL',
                'confidence': 1,
                'reasoning': f'API Error: {response.status_code}'
            }

    except Exception as e:
        return {
            'recommendation': 'NEUTRAL',
            'confidence': 1,
            'reasoning': f'Analysis failed: {str(e)}'
        }

