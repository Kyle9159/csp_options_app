# covered_call_bot.py — Dedicated Covered Call Alerts (Dec 2025)
# Runs independently, finds best calls to sell on assigned shares

import os
import json
import asyncio
import logging
from datetime import datetime
import pytz
from schwab.client import Client
from schwab import auth
from telegram import Bot as telegram_bot
import yfinance as yf
import requests
import dotenv
from pathlib import Path

from grok_utils import get_grok_sentiment_cached
from schwab_utils import get_client

dotenv.load_dotenv()

# Config
API_KEY = os.getenv('SCHWAB_API_KEY')
APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')
PAPER_TRADING = os.getenv('PAPER_TRADING', 'True').lower() == 'true'
PAPER_ACCOUNT_ID = os.getenv('PAPER_ACCOUNT_ID')
LIVE_ACCOUNT_ID = os.getenv('LIVE_ACCOUNT_ID')
ACCOUNT_ID = PAPER_ACCOUNT_ID if PAPER_TRADING else LIVE_ACCOUNT_ID
TOKEN_PATH = 'schwab_token.json'
ASSIGNMENT_FILE = 'assigned_history.json'  # Shared with wheel_bot

TELEGRAM_TOKEN = os.getenv('COVERED_CALL_TELEGRAM_TOKEN')
CHAT_ID = 7972059629
bot = telegram_bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

ET_TZ = pytz.timezone('US/Eastern')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

XAI_API_KEY = os.getenv('XAI_API_KEY')  # Your Grok key
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

CACHE_DIR = Path("cache_files")
SENTIMENT_CACHE_FILE = CACHE_DIR / 'grok_sentiment_cache.json'

# Schwab client
c = get_client()
    
async def send_alert(message):
    if not bot:
        print("Telegram disabled")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, disable_web_page_preview=True)
        print("CC Alert sent.")
    except Exception as e:
        print(f"Telegram error: {e}")

def get_current_positions():
    try:
        acct = get_client().get_account(ACCOUNT_ID, fields=Client.Account.Fields.POSITIONS)
        positions = acct.json()['securitiesAccount']['positions']
        pos_dict = {}
        for p in positions:
            qty = p['longQuantity']
            if qty > 0:
                sym = p['instrument']['symbol']
                avg_price = p.get('averagePrice', 0)
                pos_dict[sym] = {'quantity': qty, 'averagePrice': avg_price}
        return pos_dict
    except Exception as e:
        logging.warning(f"Positions fetch failed: {e}")
        return {}

async def find_covered_calls(symbol, position_data):
    shares = position_data['quantity']
    avg_price = position_data.get('averagePrice', 0)
    
    if shares < 1:
        return None

    try:
        resp = await asyncio.to_thread(get_client().get_option_chain, symbol)
        chain = resp.json()
        if 'callExpDateMap' not in chain or not chain['callExpDateMap']:
            return None

        price = yf.Ticker(symbol).info.get('regularMarketPrice') or chain['underlying'].get('last', 0)
        if price == 0:
            return None

        # Get Grok sentiment to adjust DTE range
        grok_sentiment, _ = get_grok_sentiment_cached()
        is_bullish = "BULL" in grok_sentiment or "STRONG" in grok_sentiment

        if is_bullish:
            # Bullish: Slightly more aggressive but still safe
            delta_max = 0.25   # Was 0.60 → now very conservative
            delta_min = 0.08
            dte_min = 21
            dte_max = 45
        else:
            # Neutral / Bearish: Ultra conservative
            delta_max = 0.20   # Even lower delta = deeper OTM
            delta_min = 0.05
            dte_min = 30
            dte_max = 60

        # Add minimum premium filtegrok_sentiment, _ = get_grok_sentiment_cached()
        is_bullish = "BULL" in grok_sentiment or "STRONG" in grok_sentiment

        # === CONSERVATIVE COVERED CALL MODE ===
        # Goal: Very low chance of assignment (>80–95% keep probability)
        # Focus on deep-ish OTM calls with decent premium

        if is_bullish:
            delta_min = 0.08      # Allow slightly higher premium
            delta_max = 0.25      # Max ~75% keep probability — still safe
            dte_min = 21
            dte_max = 45
        else:
            delta_min = 0.05      # Ultra conservative in neutral/bear
            delta_max = 0.20      # ~80–95% keep probability
            dte_min = 30
            dte_max = 60

        # Minimum quality filters
        min_bid = 0.30            # Don't sell tiny premiums
        min_oi = 100              # Better liquidity/fills

        candidates = []
        for exp, strikes in chain['callExpDateMap'].items():
            try:
                exp_date_str = exp.split(':')[0]
                exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d")
                dte = (exp_date.date() - datetime.now().date()).days
            except:
                continue

            # DTE filter
            if not (dte_min <= dte <= dte_max):
                continue

            for strike_str, contracts_list in strikes.items():
                if not contracts_list:
                    continue
                opt = contracts_list[0]

                bid = opt.get('bid', 0)
                if bid < min_bid:
                    continue

                delta = abs(opt.get('delta', 0) or 0)
                if not (delta_min <= delta <= delta_max):
                    continue

                oi = opt.get('openInterest', 0) or 0
                if oi < min_oi:
                    continue

                strike = float(strike_str)
                contracts_possible = shares // 100
                if contracts_possible == 0:
                    continue

                # Income calculations
                income_full = bid * 100 * contracts_possible
                total_income = income_full + (bid * (shares % 100))  # partial share

                distance_pct = ((strike - price) / price) * 100
                prob_keep = (1 - delta) * 100
                annualized = (bid / price) * 100 * (365 / dte) if dte > 0 else 0
                daily_income = bid / dte if dte > 0 else 0

                # Badges
                if distance_pct > 12:
                    distance_badge = "Very Deep OTM 🟢🟢"
                elif distance_pct > 8:
                    distance_badge = "Deep OTM 🟢"
                elif distance_pct > 4:
                    distance_badge = "Safe OTM 🟡"
                else:
                    distance_badge = "Near ATM 🟠"

                candidates.append({
                    'strike': strike,
                    'dte': dte,
                    'bid': bid,
                    'delta': delta,
                    'annualized': annualized,
                    'daily_income': daily_income,
                    'prob_keep': prob_keep,
                    'total_income': total_income,
                    'contracts_possible': contracts_possible,
                    'distance_pct': distance_pct,
                    'distance_badge': distance_badge,
                    'avg_price': avg_price,
                    'contract': opt.get('symbol', '')
                })

        # Sort by daily income first (steady cash flow), then total income
        candidates.sort(key=lambda x: (x['daily_income'], x['total_income']), reverse=True)

        return candidates[:5]  # Top 5 conservative suggestions

    except Exception as e:
        logging.warning(f"CC error {symbol}: {e}")
        return None
    
async def main():
    print("\n" + "="*70)
    print("STARTING Covered Calls SCANNER")
    print("="*70)
    logging.info("Starting Grok-Enhanced Covered Call Bot")

    while True:
        grok_sentiment, grok_summary = get_grok_sentiment_cached()
        sentiment_emoji = "🚀" if "STRONG" in grok_sentiment else "📈" if "BULL" in grok_sentiment else "😐" if "NEUTRAL" in grok_sentiment else "⚠️" if "CAUTIOUS" in grok_sentiment else "🔴"

        positions = get_current_positions()
        if not positions:
            logging.info("No assigned positions")
            await asyncio.sleep(3600)
            continue

        header = (
            f"{sentiment_emoji} GROK MARKET SENTIMENT: {grok_sentiment.replace('_', ' ')}\n"
            f"{grok_summary}\n\n"
            f"💸 COVERED CALL SUGGESTIONS ({datetime.now(ET_TZ).strftime('%b %d')})\n"
        )
        await send_alert(header)

        for sym, qty in positions.items():
            calls = await find_covered_calls(sym, int(qty))
            if calls:
                price = yf.Ticker(sym).info.get('regularMarketPrice', 0)
                msg = (
                    f"📊 {sym} — {qty} shares @ ~${price:.2f}\n"
                    f"Top calls to sell (limit near bid):\n"
                )
                for i, c in enumerate(calls, 1):
                    msg += (
                        f"#{i} ${c['strike']:.2f}C ({c['dte']} DTE)\n"
                        f"  Bid: ${c['bid']:.2f} → ${c['premium_income']:,.0f} income\n"
                        f"  Monthly: {c['monthly_yield']:.1f}% | Annualized: {c['annualized']:.1f}%\n"
                        f"  ~{c['prob_profit']:.0f}% chance keep shares\n"
                        f"  If called: +{c['if_called_return']:.1f}% return\n"
                        f"  Delta: {c['delta']:.2f}\n\n"
                    )
                msg += "Tip: Use limit orders 5–10¢ above bid for better fills!"
                await send_alert(msg)

        await asyncio.sleep(3600)  # Hourly checks

if __name__ == "__main__":
    asyncio.run(main())