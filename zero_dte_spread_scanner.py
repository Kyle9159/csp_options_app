# zero_dte_spread_scanner.py — 0DTE Credit Spread / Iron Condor Scanner (Jan 2026)
# Updated: Removed all Telegram alerts
# Expanded to up to 10 underlyings with daily 0DTE options (as of Jan 2026)
# Focus remains on neutral iron condors with strong risk management

import os
import asyncio
import json
import logging
from datetime import datetime, date
import pytz
import yfinance as yf
from schwab.client import Client
from grok_utils import get_grok_analysis, get_grok_0dte_recommendation
from schwab_utils import get_client
from helper_functions import safe_float

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
ACCOUNT_CAPITAL = float(os.getenv('ACCOUNT_CAPITAL', 100000))  # Your total capital
MAX_RISK_PCT = 0.02  # 2% max risk per trade
PROFIT_TARGET_PCT = 0.60  # Close at 60% of credit
MIN_CREDIT_RATIO = 0.15  # Lowered min credit ratio for more opportunities

# Expanded underlyings with daily (Mon-Fri) 0DTE options as of Jan 2026
UNDERLYINGS = ['SPX', 'SPY', 'QQQ', 'QQQM', 'IWM', 'TSLA', 'NVDA', 'AAPL', 
               'META', 'MSFT', 'AMZN', 'GOOGL', 'NFLX', 'AMD', 'DIA', 'VXX', 'JPM', 'BAC', 'DIS',
               'PYPL', 'CRM', 'ADBE', 'INTC', 'XLE', 'TQQQ', 'SOXL', 'SPXL']

SHORT_DELTA_TARGET = (0.05, 0.25)  # Relaxed short delta per side for more opportunities
MIN_PREMIUM_PER_SIDE = 0.25  # Lowered minimum premium
MIN_WING_WIDTH = 3  # Reduced minimum wing width

CACHE_DIR = "cache_files"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_FILE = os.path.join(CACHE_DIR, '0dte_spreads_cache.json')
CACHE_HOURS = 1

ET_TZ = pytz.timezone('US/Eastern')

# Schwab client
client = get_client()

# ==================== HELPERS ====================
# def is_market_day_and_0dte_available():
#     now = datetime.now(ET_TZ)
#     return now.weekday() < 5 and 9 <= now.hour < 16

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
            # Simple stale check
            if (datetime.now() - datetime.fromisoformat(data['timestamp'])).total_seconds() < CACHE_HOURS * 3600:
                return data['opportunities']
        except:
            pass
    return None

def save_cache(opportunities):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'opportunities': opportunities
            }, f)
        logging.info("Cached 0DTE opportunities")
    except Exception as e:
        logging.warning(f"Cache save failed: {e}")

# ==================== CORE SCANNER ====================
async def scan_0dte_spreads():
    # if not is_market_day_and_0dte_available():
    #     print("Market closed or no 0DTE available today.")
    #     return []

    cached = load_cache()
    if cached:
        print(f"Loaded {len(cached)} cached opportunities")
        return cached

    opportunities = []

    today_date = date.today()

    for symbol in UNDERLYINGS:
        print(f"Scanning {symbol}...")
        try:
            resp = client.get_option_chain(
                symbol=symbol,
                contract_type=Client.Options.ContractType.ALL,
                strike_range=Client.Options.StrikeRange.ALL,
                from_date=today_date,
                to_date=today_date,
                include_underlying_quote=True
            ).json()

            if 'callExpDateMap' not in resp or 'putExpDateMap' not in resp:
                print(f"No 0DTE chain for {symbol}")
                continue

            underlying_price = resp.get('underlyingPrice', 0)
            if underlying_price <= 0:
                continue

            # Find today's expiration key
            exp_keys = [k for k in resp['callExpDateMap'] if k.startswith(today_date.strftime('%Y-%m-%d'))]
            if not exp_keys:
                print(f"No 0DTE expiration found for {symbol}")
                continue
            exp_key = exp_keys[0]

            calls = resp['callExpDateMap'][exp_key]
            puts = resp['putExpDateMap'][exp_key]

            # Build balanced iron condors
            put_strikes = sorted([float(s) for s in puts.keys()])
            call_strikes = sorted([float(s) for s in calls.keys()])

            for short_put_strike in put_strikes:
                put_opt = puts[str(short_put_strike)][0]
                short_put_delta = abs(put_opt.get('delta', 0))
                if not (SHORT_DELTA_TARGET[0] <= short_put_delta <= SHORT_DELTA_TARGET[1]):
                    continue

                # Find long put (lower strike)
                long_put_candidates = [s for s in put_strikes if s < short_put_strike - MIN_WING_WIDTH]
                if not long_put_candidates:
                    continue
                long_put_strike = max(long_put_candidates)  # Widest reasonable wing
                long_put_opt = puts[str(long_put_strike)][0]

                put_credit = put_opt['bid'] - long_put_opt['ask']
                if put_credit < MIN_PREMIUM_PER_SIDE:
                    continue

                # Match call side with similar delta
                target_call_delta = short_put_delta
                best_call_strike = None
                best_call_delta_diff = float('inf')

                for short_call_strike in call_strikes:
                    if short_call_strike <= underlying_price:
                        continue  # Only OTM calls
                    call_opt = calls[str(short_call_strike)][0]
                    call_delta = abs(call_opt.get('delta', 0))
                    diff = abs(call_delta - target_call_delta)
                    if diff < best_call_delta_diff:
                        best_call_delta_diff = diff
                        best_call_strike = short_call_strike

                if not best_call_strike or best_call_delta_diff > 0.10:
                    continue

                call_opt = calls[str(best_call_strike)][0]
                long_call_candidates = [s for s in call_strikes if s > best_call_strike + MIN_WING_WIDTH]
                if not long_call_candidates:
                    continue
                long_call_strike = min(long_call_candidates)
                long_call_opt = calls[str(long_call_strike)][0]

                call_credit = call_opt['bid'] - long_call_opt['ask']
                if call_credit < MIN_PREMIUM_PER_SIDE:
                    continue

                total_credit = round(put_credit + call_credit, 2)
                put_width = short_put_strike - long_put_strike
                call_width = long_call_strike - best_call_strike
                avg_width = (put_width + call_width) / 2
                max_risk_per_contract = avg_width * 100 - total_credit * 100
                max_risk_dollars = max_risk_per_contract  # For 1 contract

                if max_risk_dollars > ACCOUNT_CAPITAL * MAX_RISK_PCT:
                    continue

                if total_credit / avg_width < MIN_CREDIT_RATIO:
                    continue

                prob_approx = round(100 - (short_put_delta + abs(call_opt['delta'])) * 100, 1)
                score = round((total_credit / avg_width) * 100 * prob_approx / 100, 1)

                opp = {
                    'symbol': symbol,
                    'underlying_price': round(underlying_price, 2),
                    'short_put': short_put_strike,
                    'long_put': long_put_strike,
                    'short_call': best_call_strike,
                    'long_call': long_call_strike,
                    'total_credit': total_credit,
                    'max_risk_per_contract': round(max_risk_per_contract / 100, 2),
                    'risk_pct_capital': round((max_risk_dollars / ACCOUNT_CAPITAL) * 100, 2),
                    'profit_target': round(total_credit * PROFIT_TARGET_PCT, 2),
                    'prob_approx': prob_approx,
                    'score': score
                }
                opportunities.append(opp)

        except Exception as e:
            logging.warning(f"Failed scanning {symbol}: {e}")

    # Sort by score descending
    opportunities.sort(key=lambda x: x['score'], reverse=True)

    # Grok analysis on top 10
    for opp in opportunities[:10]:
        prompt = f"""
        Analyze this 0DTE {opp['symbol']} iron condor today:
        Underlying ~${opp['underlying_price']}.
        Legs: {opp['short_put']}/{opp['long_put']} put spread + {opp['short_call']}/{opp['long_call']} call spread.
        Credit ${opp['total_credit']}, max risk ${opp['max_risk_per_contract']}.
        Approx prob {opp['prob_approx']}%.
        Good entry? Management tips? Risk highlights? Keep under 70 words.
        """
        opp['grok_analysis'] = get_grok_analysis(prompt)

        # Get Grok's recommendation for which side to favor
        put_credit = opp['total_credit'] * 0.4  # Approximate split
        call_credit = opp['total_credit'] * 0.6
        grok_rec = get_grok_0dte_recommendation(
            symbol=opp['symbol'],
            underlying_price=opp['underlying_price'],
            short_put=opp['short_put'],
            short_call=opp['short_call'],
            put_credit=put_credit,
            call_credit=call_credit
        )
        opp['grok_recommendation'] = grok_rec

    save_cache(opportunities)
    return opportunities

# ==================== MAIN ====================
async def main(force_refresh=False):
    # Force refresh: clear cache if requested
    if force_refresh:
        import os
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            print("🔄 Force refresh: Zero DTE cache cleared")
            logger.info("Zero DTE cache cleared (force refresh)")

    print("🚀 Starting 0DTE Iron Condor Scanner (up to 15 underlyings)")
    opportunities = await scan_0dte_spreads()

    if not opportunities:
        print("No qualifying 0DTE iron condors found across all underlyings.")
        return

    print(f"\nFound {len(opportunities)} opportunities. Top 15 shown:\n")
    for i, opp in enumerate(opportunities[:15], 1):
        print(f"#{i} {opp['symbol']} | Score: {opp['score']}")
        print(f"   Price: ${opp['underlying_price']}")
        print(f"   Iron Condor: {opp['short_put']}-{opp['long_put']}P / {opp['short_call']}-{opp['long_call']}C")
        print(f"   Credit: ${opp['total_credit']} | Max Risk: ${opp['max_risk_per_contract']} ({opp['risk_pct_capital']:.1f}% capital)")
        print(f"   Target Close: ${opp['profit_target']} | Approx Prob: {opp['prob_approx']}%")
        print(f"   🤖 Grok: {opp.get('grok_analysis', 'N/A')}\n")

    print(f"Full results cached to {CACHE_FILE} for dashboard use.")

if __name__ == "__main__":
    asyncio.run(main())