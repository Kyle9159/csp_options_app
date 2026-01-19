# dividend_tracker_bot.py — Dividend Income & Total Return Tracker (Dec 2025)

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
from schwab.client import Client
from schwab import auth
from telegram import Bot as telegram_bot
import yfinance as yf
import requests
from dotenv import load_dotenv
from pathlib import Path

from grok_utils import get_grok_sentiment_cached
from schwab_utils import get_client

load_dotenv()

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

CACHE_DIR = Path("cache_files")
CACHE_DIR.mkdir(exist_ok=True)

# ==================== CONFIG ====================
API_KEY = os.getenv('SCHWAB_API_KEY')
APP_SECRET = os.getenv('SCHWAB_APP_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')
PAPER_TRADING = os.getenv('PAPER_TRADING', 'True').lower() == 'true'
PAPER_ACCOUNT_ID = os.getenv('PAPER_ACCOUNT_ID')
LIVE_ACCOUNT_ID = os.getenv('LIVE_ACCOUNT_ID')
ACCOUNT_ID = PAPER_ACCOUNT_ID if PAPER_TRADING else LIVE_ACCOUNT_ID
# TOKEN_PATH = CACHE_DIR / 'schwab_token.json'

TELEGRAM_TOKEN = os.getenv('DIVIDEND_TRACKER_TELEGRAM_TOKEN')  # Optional separate token, or reuse existing
CHAT_ID = int(os.getenv('CHAT_ID', '7972059629'))  # Reuse your existing chat ID
bot = telegram_bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

ET_TZ = pytz.timezone('US/Eastern')

XAI_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
SENTIMENT_CACHE_FILE = CACHE_DIR / 'grok_sentiment_cache.json'  # Shared with other bots

# ==================== SCHWAB CLIENT ====================
c = get_client()

# ==================== TELEGRAM ALERT ====================
async def send_alert(message):
    if not bot:
        print("Telegram disabled")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, disable_web_page_preview=True, parse_mode='HTML')
        print("Dividend report sent.")
    except Exception as e:
        print(f"Telegram error: {e}")

# ==================== MAIN TRACKER ====================
async def generate_dividend_report():
    print("\n" + "="*70)
    print("STARTING Dividend Tracker")
    print("="*70)

    client = get_client()

    # === FETCH POSITIONS ===
    try:
        acct_resp = client.get_account(ACCOUNT_ID, fields=Client.Account.Fields.POSITIONS)
        logging.info(f"Account API status: {acct_resp.status_code}")
        
        if acct_resp.status_code != 200:
            logging.error(f"Account API error: {acct_resp.status_code} {acct_resp.text}")
            await send_alert(f"❌ Failed to fetch positions: HTTP {acct_resp.status_code}")
            return
        
        acct_json = acct_resp.json()
        acct_data = acct_json.get('securitiesAccount', {})
        positions = acct_data.get('positions', [])
        logging.info(f"Found {len(positions)} positions")
        
        if len(positions) == 0:
            await send_alert("⚠️ No positions found in account")
            return
            
    except Exception as e:
        logging.error(f"Failed to fetch positions: {e}")
        await send_alert(f"❌ Position fetch exception: {str(e)}")
        return

    # === FETCH TRANSACTIONS (year-by-year) ===
    current_year = datetime.now().year
    start_year = 2025  # Adjust to your actual start year
    
    all_transactions = []
    for year in range(start_year, current_year + 1):
        year_start = datetime(year, 1, 1)
        year_end = datetime(year + 1, 1, 1) - timedelta(days=1)
        if year_end > datetime.now():
            year_end = datetime.now()
        
        logging.info(f"Fetching transactions for {year}")
        try:
            tx_resp = client.get_transactions(ACCOUNT_ID, start_date=year_start, end_date=year_end)
            if tx_resp.status_code != 200:
                logging.warning(f"Year {year} error: {tx_resp.status_code}")
                continue
            
            raw_data = tx_resp.json()
            year_txs = raw_data if isinstance(raw_data, list) else raw_data.get('transactions', [])
            all_transactions.extend(year_txs)
            logging.info(f"   → {len(year_txs)} transactions")
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.warning(f"Failed {year}: {e}")

    # === SYMBOL MAPPING & DIVIDEND PROCESSING ===
    symbol_to_name = {}
    position_values = {}
    total_portfolio_value = 0.0
    
    for pos in positions:
        instr = pos['instrument']
        sym = instr['symbol']
        qty = pos['longQuantity']
        if qty < 1: continue
        mv = pos['marketValue']
        position_values[sym] = mv
        total_portfolio_value += mv
        symbol_to_name[sym] = instr.get('description', sym).upper()

    dividend_totals = {}
    ytd_dividends = {}
    ytd_start = datetime(current_year, 1, 1)
    unattributed_total = 0.0

    for tx in all_transactions:
        tx_type = (tx.get('type') or tx.get('transactionType') or tx.get('description', '')).upper()
        if 'DIVIDEND' not in tx_type and 'INTEREST' not in tx_type:
            continue

        amount = float(tx.get('netAmount') or tx.get('amount') or 0)
        if amount <= 0: continue

        tx_date_str = tx.get('tradeDate') or tx.get('transactionDate') or tx.get('settlementDate') or tx.get('date', '')
        try:
            tx_date = datetime.strptime(tx_date_str.split('T')[0], '%Y-%m-%d')
        except:
            continue

        symbol = None
        instr = tx.get('instrument', {})
        if isinstance(instr, dict):
            symbol = instr.get('symbol')

        if not symbol and tx.get('transferItems'):
            for item in tx.get('transferItems', []):
                item_instr = item.get('instrument', {})
                if item_instr.get('symbol') not in ['CURRENCY_USD', None]:
                    symbol = item_instr.get('symbol')
                    break

        if not symbol:
            desc = tx.get('description', '').upper()
            if desc:
                for sym, full_name in symbol_to_name.items():
                    if sym in desc or full_name in desc or desc.startswith(full_name):
                        symbol = sym
                        break

        if symbol and symbol in position_values:
            dividend_totals[symbol] = dividend_totals.get(symbol, 0) + amount
            if tx_date >= ytd_start:
                ytd_dividends[symbol] = ytd_dividends.get(symbol, 0) + amount
        else:
            if total_portfolio_value > 0:
                unattributed_total += amount
                for sym, mv in position_values.items():
                    allocated = amount * (mv / total_portfolio_value)
                    dividend_totals[sym] = dividend_totals.get(sym, 0) + allocated
                    if tx_date >= ytd_start:
                        ytd_dividends[sym] = ytd_dividends.get(sym, 0) + allocated

    if unattributed_total > 0:
        logging.info(f"Allocated ${unattributed_total:.2f} unattributed dividends proportionally")

    # === HEADER ===
    grok_sentiment, grok_summary = get_grok_sentiment_cached()
    sentiment_emoji = "🚀" if "STRONG" in grok_sentiment else "📈" if "BULL" in grok_sentiment else "😐" if "NEUTRAL" in grok_sentiment else "⚠️" if "CAUTIOUS" in grok_sentiment else "🔴"

    header = (
        f"<b>{sentiment_emoji} INCOME & TOTAL RETURN REPORT — {datetime.now(ET_TZ).strftime('%b %d, %Y')}</b>\n"
        f"{grok_summary}\n\n"
    )
    await send_alert(header)

    # === PROCESS POSITIONS ===
    equity_positions = []
    for pos in positions:
        instr = pos['instrument']
        symbol = instr['symbol']
        qty = pos['longQuantity']
        if qty < 1: continue

        avg_price = pos.get('averagePrice', 0)
        cost_basis = qty * avg_price
        market_value = pos['marketValue']
        unrealized_pl = pos.get('gainLoss', market_value - cost_basis)
        unrealized_pl_pct = pos.get('gainLossPercentage', 0)

        total_div = dividend_totals.get(symbol, 0)
        ytd_div = ytd_dividends.get(symbol, 0)

        # === NEW: TOTAL ADJUSTED P/L (incl dividends) ===
        adjusted_pl = unrealized_pl + total_div
        adjusted_pl_pct = (adjusted_pl / cost_basis * 100) if cost_basis > 0 else 0

        yoc = (total_div / cost_basis * 100) if cost_basis > 0 else 0

        # Forward estimates
        try:
            ticker = yf.Ticker(symbol)
            current_price = (
                ticker.info.get('regularMarketPrice') or 
                ticker.info.get('previousClose') or 
                ticker.info.get('currentPrice') or 
                (market_value / qty if qty else 0)
            )
            if current_price <= 0:
                raise ValueError("No valid price")

            # Primary: Yahoo's trailing rate
            trailing_rate = ticker.info.get('trailingAnnualDividendRate') or ticker.info.get('trailingAnnualDividendYield', 0) or 0
            if isinstance(trailing_rate, dict):  # Safety
                trailing_rate = 0
            if trailing_rate > 0:
                trailing_rate = float(trailing_rate)

            # Fallback: Calculate from dividend history
            if trailing_rate < 0.01:  # Only fallback if primary missing/low
                div_history = ticker.dividends
                if not div_history.empty and len(div_history) > 0:
                    # Handle tz-aware index
                    if div_history.index.tz is not None:
                        # Localize cutoff to same tz
                        cutoff = datetime.now(div_history.index.tz) - timedelta(days=365)
                    else:
                        # Naive
                        cutoff = datetime.now() - timedelta(days=365)

                    recent_divs = div_history[div_history.index >= cutoff]

                    if not recent_divs.empty and len(recent_divs) > 1:
                        total_recent = recent_divs.sum()
                        days_covered = (recent_divs.index[-1] - recent_divs.index[0]).days + 1  # Inclusive
                        if days_covered > 0:
                            annual_from_history = total_recent * (365 / days_covered)
                            trailing_rate = max(trailing_rate, annual_from_history)
                    elif len(div_history) > 1:
                        # Use full history if <1 year
                        total_all = div_history.sum()
                        days_total = (div_history.index[-1] - div_history.index[0]).days + 1
                        if days_total > 0:
                            trailing_rate = total_all * (365 / days_total)

            forward_yield = (trailing_rate / current_price * 100) if current_price > 0 else 0
            est_annual = trailing_rate * qty
            est_monthly = est_annual / 12 if est_annual > 0 else 0

        except Exception as e:
            logging.warning(f"yfinance forward calc failed for {symbol}: {e}")
            forward_yield = est_annual = est_monthly = 0

        roc_warning = "\n⚠️ <b>HIGH ROC RISK</b>: Distributions often ~100% Return of Capital" if symbol.endswith('W') else ""

        equity_positions.append({
            'symbol': symbol, 'qty': qty, 'avg_price': avg_price, 'cost_basis': cost_basis,
            'market_value': market_value, 'unrealized_pl': unrealized_pl, 'unrealized_pl_pct': unrealized_pl_pct,
            'total_div': total_div, 'ytd_div': ytd_div, 'yoc': yoc,
            'adjusted_pl': adjusted_pl, 'adjusted_pl_pct': adjusted_pl_pct,
            'forward_yield': forward_yield, 'est_annual_income': est_annual, 'est_monthly_income': est_monthly,
            'roc_warning': roc_warning
        })

    # Sort by estimated annual income
    equity_positions.sort(key=lambda x: x['est_annual_income'], reverse=True)

    # === SEND PER-POSITION REPORTS ===
    for pos in equity_positions:
        msg = (
            f"<b>{pos['symbol']}</b> — {pos['qty']:.0f} shares{pos['roc_warning']}\n"
            f"💵 Avg Cost: ${pos['avg_price']:.2f} | Cost Basis: ${pos['cost_basis']:,.0f}\n"
            f"📊 Value: ${pos['market_value']:,.0f}\n"
            f"📉 Unrealized P/L: ${pos['unrealized_pl']:,.0f} ({pos['unrealized_pl_pct']:+.1f}%)\n\n"
            f"💰 <b>Historical Income</b>\n"
            f"   Lifetime: ${pos['total_div']:,.2f} | YTD: ${pos['ytd_div']:,.2f}\n"
            f"   Yield on Cost: {pos['yoc']:.2f}%\n\n"
            f"🌟 <b>Total Gain/Loss (incl Dividends)</b>\n"
            f"   Adjusted P/L: <b>${pos['adjusted_pl']:,.0f}</b> ({pos['adjusted_pl_pct']:+.2f}%)\n\n"
            f"🚀 <b>Forward Estimates</b>\n"
            f"   Yield: <b>{pos['forward_yield']:.1f}%</b>\n"
            f"   Est. Annual: <b>${pos['est_annual_income']:,.0f}</b>\n"
            f"   Est. Monthly: <b>${pos['est_monthly_income']:,.0f}</b>"
        )
        await send_alert(msg)
        await asyncio.sleep(1)

    # === PORTFOLIO TOTALS ===
    if equity_positions:
        total_value = sum(p['market_value'] for p in equity_positions)
        total_cost = sum(p['cost_basis'] for p in equity_positions)
        total_unrealized = sum(p['unrealized_pl'] for p in equity_positions)
        total_hist_div = sum(p['total_div'] for p in equity_positions)
        total_adjusted_pl = total_unrealized + total_hist_div
        total_adjusted_pct = (total_adjusted_pl / total_cost * 100) if total_cost else 0
        total_est_annual = sum(p['est_annual_income'] for p in equity_positions)

        summary = (
            f"<b>📊 PORTFOLIO TOTALS</b>\n"
            f"Market Value: <b>${total_value:,.0f}</b>\n"
            f"Cost Basis: ${total_cost:,.0f}\n"
            f"Unrealized P/L: ${total_unrealized:,.0f}\n"
            f"Lifetime Dividends: <b>${total_hist_div:,.0f}</b>\n"
            f"🌟 <b>Total Adjusted P/L (incl Divs)</b>: <b>${total_adjusted_pl:,.0f}</b> ({total_adjusted_pct:+.2f}%)\n\n"
            f"Est. Forward Annual Income: <b>${total_est_annual:,.0f}</b>\n"
            f"Est. Monthly Income: <b>${total_est_annual / 12:,.0f}</b>"
        )
        await send_alert(summary)
    return equity_positions

# ==================== RUN REPORT ====================
async def main():
    logging.info("Starting Dividend Tracker Report")
    positions = await generate_dividend_report()
    logging.info("Dividend report complete")
    return positions

if __name__ == "__main__":
    asyncio.run(main())