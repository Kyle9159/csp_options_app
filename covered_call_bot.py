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
import yfinance as yf
from telegram_utils import send_alert as _tg_send
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

ET_TZ = pytz.timezone('US/Eastern')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

XAI_API_KEY = os.getenv('XAI_API_KEY')  # Your Grok key
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"

CACHE_DIR = Path("cache_files")
SENTIMENT_CACHE_FILE = CACHE_DIR / 'grok_sentiment_cache.json'

# Schwab client — lazy loaded on first use so server starts even if token is expired
c = None
def _get_schwab_client():
    global c
    if c is None:
        c = get_client()
    return c
    
async def send_alert(message):
    await _tg_send("cc_bot", message)

def get_current_positions():
    try:
        client = get_client()
        acct_numbers = client.get_account_numbers().json()
        account_hash = next(
            (a['hashValue'] for a in acct_numbers if a['accountNumber'] == ACCOUNT_ID),
            None
        )
        if not account_hash:
            logging.warning(f"No hash found for account {ACCOUNT_ID}")
            return {}
        acct = client.get_account(account_hash, fields=Client.Account.Fields.POSITIONS)
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

        # 5-regime CC settings (OTM calls — lower delta = safer from assignment)
        _CC_REGIME = {
            "STRONG_BULL":      {"delta_min": 0.10, "delta_max": 0.30, "dte_min": 21, "dte_max": 35},
            "MILD_BULL":        {"delta_min": 0.08, "delta_max": 0.25, "dte_min": 25, "dte_max": 40},
            "NEUTRAL_OR_WEAK":  {"delta_min": 0.05, "delta_max": 0.20, "dte_min": 30, "dte_max": 45},
            "CAUTIOUS":         {"delta_min": 0.05, "delta_max": 0.15, "dte_min": 35, "dte_max": 50},
            "BEARISH_HIGH_VOL": {"delta_min": 0.03, "delta_max": 0.10, "dte_min": 45, "dte_max": 60},
        }
        grok_sentiment, _ = get_grok_sentiment_cached()
        regime_key = grok_sentiment if grok_sentiment in _CC_REGIME else "MILD_BULL"
        cc_settings = _CC_REGIME[regime_key]
        delta_min = cc_settings["delta_min"]
        delta_max = cc_settings["delta_max"]
        dte_min   = cc_settings["dte_min"]
        dte_max   = cc_settings["dte_max"]

        # Minimum quality filters
        min_bid = 0.30            # Don't sell tiny premiums
        min_oi = 100              # Adequate liquidity

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

        for sym, pos_data in positions.items():
            calls = await find_covered_calls(sym, pos_data)
            if calls:
                shares = pos_data['quantity']
                avg_price = pos_data.get('averagePrice', 0)
                price = yf.Ticker(sym).info.get('regularMarketPrice', 0)
                msg = (
                    f"📊 {sym} — {shares} shares @ ~${price:.2f}\n"
                    f"Top calls to sell (limit near bid):\n"
                )
                for i, c in enumerate(calls, 1):
                    if_called = ((c['strike'] - c['avg_price']) / c['avg_price'] * 100) if c['avg_price'] else 0
                    monthly = c['annualized'] / 12
                    msg += (
                        f"#{i} ${c['strike']:.2f}C ({c['dte']} DTE) {c.get('distance_badge', '')}\n"
                        f"  Bid: ${c['bid']:.2f} → ${c['total_income']:,.0f} income\n"
                        f"  Monthly: {monthly:.1f}% | Annualized: {c['annualized']:.1f}%\n"
                        f"  ~{c['prob_keep']:.0f}% chance keep shares\n"
                        f"  If called: +{if_called:.1f}% return\n"
                        f"  Delta: {c['delta']:.2f}\n\n"
                    )
                msg += "Tip: Use limit orders 5–10¢ above bid for better fills!"
                await send_alert(msg)

        await asyncio.sleep(3600)  # Hourly checks

if __name__ == "__main__":
    asyncio.run(main())