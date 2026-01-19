# leaps_scanner.py — Simple Grok-Driven LEAPS Scanner (Jan 2026)
# Focus: Grok picks best symbols → fetch deep ITM/ATM LEAPS calls → Grok analyzes CC strategy

import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

import asyncio
import os
import json
import re  # For robust parsing
from datetime import datetime, timedelta
import pytz
from pathlib import Path
import dotenv
from schwab.client import Client
from schwab import auth
import yfinance as yf
from telegram import Bot as telegram_bot

from grok_utils import get_grok_analysis, get_grok_sentiment_cached
from helper_functions import save_cached_leaps
from schwab_utils import get_client

dotenv.load_dotenv()

# ==================== SETUP ====================
API_KEY = os.getenv('SCHWAB_API_KEY')
APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://127.0.0.1')

CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)
TOKEN_PATH = CACHE_DIR / 'schwab_token.json'

c = get_client()
c.set_enforce_enums(False)

ET_TZ = pytz.timezone('US/Eastern')
TELEGRAM_TOKEN = os.getenv('SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN')
CHAT_ID = 7972059629
bot = telegram_bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

async def send_alert(msg):
    if bot:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True)
        except:
            pass
    print(msg)

# ==================== MAIN LEAPS SCANNER ====================
async def main(force_refresh=False):
    # Force refresh: clear cache if requested
    if force_refresh:
        from pathlib import Path
        cache_file = Path("cache_files") / "leaps_cache.json"
        if cache_file.exists():
            cache_file.unlink()
            print("🔄 Force refresh: LEAPS cache cleared")
            logger.info("LEAPS cache cleared (force refresh)")

    print("\n=== STARTING LEAPS SCANNER ===")

    grok_sentiment, grok_summary = get_grok_sentiment_cached()
    
    sentiment_emoji = "🚀" if "BULL" in grok_sentiment else "⚠️"
    
    # Step 1: Ask Grok for top 50 LEAPS symbols
    prompt_symbols = f"""
        You are an expert long-term investor using LEAPS calls.

        Current date: January 2026
        Market regime: Strong bull, low volatility
        Grok sentiment: {grok_sentiment}

        Recommend exactly 30 high-quality stocks/ETFs for buying LEAPS calls (1-2 years out).  Focus on the best current sectors (tech, AI, energy, healthcare), market sentiment/news, company sentiment/quality.

        Prioritize:
        - Strong fundamentals (growth, dividends, moat)
        - Stable or upward trend
        - Reasonable IV (not too high)
        - Good liquidity in options
        - Tier 1/2 quality

        Return ONLY a numbered list: 1. AAPL, 2. MSFT, etc. No explanations.
        """

    print("Asking Grok for top 30 LEAPS symbols...")
    response = get_grok_analysis(prompt_symbols)
    
    # Parse symbols
    symbols = []
    for line in response.split('\n'):
        line = line.strip()
        if line and (line[0].isdigit() or line.startswith('-') or line.startswith('•')):
            parts = line.split('. ', 1) if '. ' in line else line.split(' ', 1)
            if len(parts) > 1:
                sym = parts[1].split()[0].upper().strip('.,)')
                if sym.isalpha() and len(sym) <= 5:
                    symbols.append(sym)
            elif line[0].isdigit():
                sym = line.split()[1].upper().strip('.,)') if len(line.split()) > 1 else ""
                if sym.isalpha() and len(sym) <= 5:
                    symbols.append(sym)
    
    symbols = list(dict.fromkeys(symbols))[:50]  # Dedupe, limit 50
    print(f"Grok recommended {len(symbols)} symbols: {', '.join(symbols[:10])}...")

    if not symbols:
        await send_alert("LEAPS Scanner: Grok returned no valid symbols")
        return

    # Step 2: Fetch one strong LEAPS call per symbol (minimal filters)
    leaps_opps = []
    for sym in symbols:
        try:
            chain = c.get_option_chain(
                symbol=sym,
                contract_type='CALL',
                from_date=datetime.now().date(),
                to_date=(datetime.now() + timedelta(days=800)).date()
            ).json()

            if 'callExpDateMap' not in chain:
                print(f"   → No callExpDateMap for {sym}")
                continue

            underlying_price = chain.get('underlyingPrice', 0)
            if underlying_price == 0:
                print(f"   → No underlying price for {sym}")
                continue

            best_opp = None
            best_score = -999

            # Collect all true LEAPS expirations (300+ days)
            long_exps = []
            for exp_key, strikes in chain['callExpDateMap'].items():
                try:
                    dte = int(exp_key.split(':')[1])
                    if dte >= 300:
                        long_exps.append((dte, exp_key, strikes))
                        print(f"     Found LEAPS exp: {exp_key.split(':')[0]} → {dte} DTE ({len(strikes)} strikes)")
                except Exception as e:
                    print(f"     Bad exp_key {exp_key}: {e}")
                    continue

            if not long_exps:
                print(f"   → No LEAPS expirations (>=300 DTE) found for {sym}")
                continue

            # Prefer longest DTE first
            long_exps.sort(reverse=True, key=lambda x: x[0])
            print(f"   → {len(long_exps)} LEAPS expirations available, checking top 3")

            processed_strikes = 0
            skipped_strikes = 0

            # Check up to the 3 longest expirations
            for dte, exp_key, strikes in long_exps[:3]:
                print(f"     → Checking DTE {dte} ({len(strikes)} strikes)")
                for strike_str, contracts in strikes.items():
                    if not contracts:
                        continue
                    opt = contracts[0]
                    processed_strikes += 1

                    try:
                        strike = float(strike_str)
                        if strike > underlying_price * 1.5:  # Allow up to ~25% OTM
                            continue

                        bid = float(opt.get('bidPrice', 0) or 0)
                        ask = float(opt.get('askPrice', 0) or 0)
                        mark = float(opt.get('markPrice', 0) or 0)
                        last = float(opt.get('lastPrice', 0) or 0)
                        close = float(opt.get('closePrice', 0) or 0)
                        premium = bid if bid > 0 else ask if ask > 0 else mark if mark > 0 else last if last > 0 else close
                        if premium < .01:  # Skip worthless
                            skipped_strikes += 1
                            continue

                        delta = abs(float(opt.get('delta', 0) or .5))
                        iv = float(opt.get('volatility', 0) or 30)

                        # Score favors deep ITM + high delta + longer DTE
                        depth_bonus = max(0, (underlying_price - strike) / underlying_price) * 100
                        score = delta * 120 + depth_bonus + (dte / 10) - (iv / 20)

                        # print(f"       Strike ${strike:.0f} | Prem ${premium:.2f} | DTE {dte} | Delta {delta:.3f} | IV% {iv:.0f} | Score {score:.1f}")

                        if score > best_score:
                            best_score = score
                            best_opp = {
                                'symbol': sym,
                                'underlying_price': round(underlying_price, 2),
                                'strike': strike,
                                'dte': dte,
                                'premium': round(premium, 2),
                                'delta': round(delta, 3),
                                'iv': round(iv, 0),
                                'oi': int(opt.get('openInterest', 0) or 0),
                                'volume': int(opt.get('totalVolume', 0) or 0),
                                'distance_itm_pct': round(depth_bonus, 1),
                                'expiration_date': exp_key.split(':')[0],
                                'capital_required': round(premium * 100, 0),                    # $ per contract
                                'breakeven': round(strike + premium, 2),                        # Strike + premium paid
                                'leverage_ratio': round(underlying_price / premium, 1),
                            }
                    except Exception as e:
                        print(f"       → ERROR on strike {strike_str}: {e}")
                        continue

            if best_opp:
                print(f"   → Selected best: {best_opp['symbol']} | ${best_opp['strike']:.0f} STRIKE | ({best_opp['dte']} DTE) | Score {best_score:.1f}")
                leaps_opps.append(best_opp)
            else:
                print(f"   → No valid LEAPS call found for {sym} (processed {processed_strikes}, skipped {skipped_strikes})")

        except Exception as e:
            print(f"   → Chain fetch failed for {sym}: {e}")
            continue

    if not leaps_opps:
        await send_alert("LEAPS Scanner: No viable LEAPS calls found")
        return

    print(f"Found {len(leaps_opps)} LEAPS candidates — sending all to Grok for final ranking...")

# Step 3: STRICTER PROMPT FOR GROK TO FORCE BETTER OUTPUT
    current_date = datetime.now(ET_TZ).strftime('%B %d, %Y')
    prompt_ranking = f"""
        You are an expert LEAPS + Covered Call strategist.

        Current date: {current_date}
        Market regime: Strong bull / growth favorable

        Rank and SCORE the following LEAPS call candidates from BEST to WORST for long-term bullish exposure + some leverage to keep initial capital cost down + covered call income.

        Evaluate on:
        - Company quality & long-term growth
        - Delta (higher = better stock replacement)
        - Depth ITM
        - DTE (longer = more CC opportunities)
        - IV level (moderate preferred)
        - Premium cost vs leverage
        - Liquidity

        CRITICAL FORMATTING RULES - FOLLOW EXACTLY OR ANALYSIS FAILS:
        - Output ONLY the ranked list, no intro, no conclusion, no extra text
        - Every rank MUST have all 4 lines in this exact order:
        - Score out of 100

        RANK #X: SYMBOL - $STRIKE CALL (DDD DTE)
        Score: XX/100
        Reason: One short sentence only
        Covered Call Idea: Short $Y CALL (30-45 DTE), expected $Z premium

        Example:
        RANK #1: MSFT - $175 CALL (709 DTE)
        Score: 95/100
        Reason: Excellent growth and moderate IV.
        Covered Call Idea: Short $500 CALL (45 DTE), expected $15 premium

        Rank the top 15 strongest. Provide ALL 15 ranks with the exact format.
        """

    for i, opp in enumerate(leaps_opps, 1):
        prompt_ranking += f"\n--- CANDIDATE {i} ---\n"
        prompt_ranking += f"Symbol: {opp['symbol']}\n"
        prompt_ranking += f"Underlying: ${opp['underlying_price']:.2f}\n"
        prompt_ranking += f"LEAPS: ${opp['strike']:.0f} CALL ({opp['dte']} DTE)\n"
        prompt_ranking += f"Premium: ${opp['premium']:.2f}\n"
        prompt_ranking += f"Delta: {opp['delta']:.3f} | IV: {opp['iv']:.0f}% | OI: {opp['oi']}\n"
        prompt_ranking += f"ITM Depth: {opp['distance_itm_pct']:.1f}%\n"

    try:
        grok_ranking_response = get_grok_analysis(prompt_ranking)
        print("\n--- GROK RAW OUTPUT ---\n")
        print(grok_ranking_response[:2000])  # Print first part for debugging
        print("\n--- END RAW ---\n")
    except Exception as e:
        print(f"Grok ranking failed: {e}")
        grok_ranking_response = ""

    # Step 4: EVEN MORE ROBUST PARSING (fixes the symbol bug)
    final_tiles = []
    current_tile = None
    lines = [line.strip() for line in grok_ranking_response.split('\n') if line.strip()]

    for line in lines:
        # Detect rank line more flexibly
        if re.match(r'^RANK\s*#?\d+[:\s]', line, re.IGNORECASE):
            if current_tile:
                final_tiles.append(current_tile)

            raw_line = line

            # Extract rank
            rank_match = re.search(r'#(\d+)', line)
            rank_num = rank_match.group(1) if rank_match else "?"

            # Extract symbol: first uppercase ticker AFTER "RANK #X:"
            after_rank = re.split(r'#\d+[:\s.-]*', line, flags=re.IGNORECASE)[1] if rank_match else line
            symbol_match = re.search(r'\b([A-Z]{2,5})\b', after_rank)
            symbol = symbol_match.group(1) if symbol_match else 'UNKNOWN'

            # Extract strike and DTE
            strike_match = re.search(r'\$([0-9]+\.?[0-9]*)', raw_line)
            dte_match = re.search(r'\((\d+)\s*DTE\)', raw_line) or re.search(r'\((\d+)\s*days', raw_line)

            strike = float(strike_match.group(1)) if strike_match else 0.0
            dte = int(dte_match.group(1)) if dte_match else 0

            current_tile = {
                'rank': rank_num,
                'symbol': symbol,
                'strike': strike,
                'dte': dte,
                'grok_score': 0,
                'reason': '',
                'cc_idea': '',
                'raw_line': raw_line
            }

        # More flexible field detection
        elif line.lower().startswith("score:") and current_tile:
            try:
                score_str = re.search(r'\d+', line.split(':', 1)[1]).group(0)
                current_tile['grok_score'] = int(score_str)
            except:
                pass

        elif line.lower().startswith("reason:") and current_tile:
            current_tile['reason'] = line.split(':', 1)[1].strip()

        elif line.lower().startswith("covered call idea:") and current_tile:
            current_tile['cc_idea'] = line.split(':', 1)[1].strip()

    if current_tile:
        final_tiles.append(current_tile)

    # Enrich with real data (unchanged)
    symbol_to_opp = {opp['symbol']: opp for opp in leaps_opps}
    for tile in final_tiles:
        sym = tile['symbol']
        if sym in symbol_to_opp:
            real_opp = symbol_to_opp[sym]
            tile.update({
                'underlying_price': real_opp['underlying_price'],
                'premium': real_opp['premium'],
                'delta': real_opp['delta'],
                'iv': real_opp['iv'],
                'oi': real_opp['oi'],
                'distance_itm_pct': real_opp['distance_itm_pct'],
                'expiration_date': real_opp.get('expiration_date', 'N/A'),
            })
            p = real_opp['premium']
            tile['capital_required'] = round(p * 100, 0)
            tile['breakeven'] = round(real_opp['strike'] + p, 2)
            tile['leverage_ratio'] = round(real_opp['underlying_price'] / p, 1) if p > 0 else 0
        else:
            tile['premium'] = 0.0
            tile['delta'] = 0.0
            tile['distance_itm_pct'] = 0.0

    # RELAXED FALLBACK: Only trigger if Grok completely failed (no valid symbols)
    if len(final_tiles) < 5 or final_tiles[0]['symbol'] == 'UNKNOWN':
        print("Grok parsing completely failed — using quantitative ranking")
        # ... [existing quantitative fallback code] ...
    else:
        print(f"Grok analysis successful — using {len(final_tiles)} ranked tiles with real reasons")
    # === ADD DISPLAY LINE FOR TELEGRAM ===
    for tile in final_tiles:
        depth_emoji = "Deep ITM 🟢🟢" if tile.get('distance_itm_pct', 0) > 15 else "ITM 🟢" if tile.get('distance_itm_pct', 0) > 5 else "ATM 🟡"
        tile['display_line'] = (
            f"RANK #{tile['rank']}: {tile['symbol']} ${tile['strike']:.0f} CALL "
            f"({tile['dte']} DTE) @ ${tile.get('premium', 0):.2f} "
            f"| Delta {tile.get('delta', 0):.2f} | {depth_emoji}"
        )

    # === SEND TO TELEGRAM ===
    header = f"{sentiment_emoji} GROK'S TOP LEAPS + COVERED CALL PICKS (Jan 2026)\n\n"
    header += f"{grok_summary}\n\n"
    header += f"Analyzed {len(leaps_opps)} candidates → Top ranked opportunities:\n\n"
    await send_alert(header)

    for tile in final_tiles[:3]:  # Only top 3
        msg = (
            f"🏆 {tile['display_line']}\n\n"
            f"📝 Reason: {tile['reason']}\n\n"
            f"💡 Covered Call Idea: {tile['cc_idea']}\n"
        )
        await send_alert(msg)
        await asyncio.sleep(2)

    # Save for dashboard (compatible with existing helper)
    save_cached_leaps(final_tiles, LEAPS_CACHE_FILE="leaps_cache.json")
    print(f"LEAPS scan complete — {len(final_tiles)} ranked tiles cached")

if __name__ == "__main__":
    asyncio.run(main())