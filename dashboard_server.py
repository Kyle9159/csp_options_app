# dashboard_server.py — Interactive Dashboard Server (Dec 2025)

from flask import Flask, jsonify, send_from_directory, request
import uuid
import asyncio
import threading
import os
from datetime import datetime
import time
import pytz
import requests
import yfinance as yf
import dotenv
from jinja2 import Environment, FileSystemLoader
import gspread
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from config import logger

dotenv.load_dotenv()

# Import your bot mains
from covered_call_bot import main as cc_main
from dividend_tracker_bot import main as div_main
import simple_options_scanner
from generate_dashboard import calculate_conservative_cc
from grok_utils import get_grok_sentiment_cached as get_grok_sentiment, get_grok_analysis
from open_trade_monitor import load_trades_from_sheet, update_sheet_with_live_data, get_sheet
from helper_functions import safe_float, safe_int, safe_date
# import leaps_scanner  # Not critical for dashboard
# import zero_dte_spread_scanner  # Not critical for dashboard

# Phase 4 & 5 Integrations
from portfolio_greeks import calculate_portfolio_greeks, PositionGreeks
from dynamic_exit_targets import calculate_exit_targets
from earnings_calendar import check_earnings_conflict, get_earnings_recommendation
from position_sizing import calculate_position_size
from smart_alerts import run_alert_scan
from trade_journal import get_recent_trades, get_trade_performance_summary
from schwab_utils import sell_put_to_open, buy_put_to_close


from telegram import Bot as telegram_bot

OPEN_TRADE_TELEGRAM_TOKEN = os.getenv('PAPER_TRADE_MONITOR_TELEGRAM_TOKEN')  # Your token
CHAT_ID = os.getenv('PAPER_TRADE_MONITOR_CHAT_ID')  # Your chat ID
open_bot = telegram_bot(token=OPEN_TRADE_TELEGRAM_TOKEN) if OPEN_TRADE_TELEGRAM_TOKEN else None

XAI_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
GOOGLE_SHEET_ID = "1e5p_tKBR3qz52_q0-yIeEbTIofyKTcmcfqgiRBQ52Nc"

ET_TZ = pytz.timezone('US/Eastern')

LAST_MIRROR_CALL = 0
MIRROR_CACHE = None
MIRROR_CACHE_TIME = 300  # 5 minutes cache

app = Flask(__name__, static_folder='.')
progress_tracker = {}

env = Environment(loader=FileSystemLoader('.'))
env.filters['safe_float'] = safe_float


@app.route('/progress/<task_id>')
def get_progress(task_id):
    info = progress_tracker.get(task_id, {'progress': 0, 'status': 'unknown'})
    return jsonify(info)

# Helper to run async function with progress updates
def run_async_with_progress(task_id, coro_factory):
    """
    Run an async coroutine that accepts a progress_callback.
    coro_factory: a function that returns the coroutine and takes (progress_callback)
    """
    def wrapper():
        try:
            progress_tracker[task_id] = {'progress': 0, 'status': 'running'}

            def progress_callback(pct: int):
                if 0 <= pct <= 100:
                    progress_tracker[task_id]['progress'] = pct

            # Create and run the coroutine with callback
            coro = coro_factory(progress_callback)
            asyncio.run(coro)

            # Ensure completion
            progress_tracker[task_id]['progress'] = 100
            progress_tracker[task_id]['status'] = 'complete'

        except Exception as e:
            print(f"Task {task_id} error: {e}")
            progress_tracker[task_id]['status'] = 'error'
            progress_tracker[task_id]['progress'] = 0
        finally:
            threading.Timer(600, lambda: progress_tracker.pop(task_id, None)).start()

    threading.Thread(target=wrapper).start()


# === DASHBOARD ===
@app.route('/')
def index():
    return send_from_directory('.', 'trading_dashboard.html')


@app.route('/heatmap')
def heatmap_viewer():
    """Options chain heatmap viewer page"""
    return send_from_directory('.', 'heatmap_viewer.html')


# === BUTTON ENDPOINTS ===
@app.route('/run/scanner')
def run_scanner():
    task_id = str(uuid.uuid4())
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'

    def create_scanner_task(progress_callback):
        async def task():
            progress_callback(15)
            await simple_options_scanner.main(force_refresh=force_refresh)
            progress_callback(100)
        return task()

    run_async_with_progress(task_id, create_scanner_task)
    status_msg = "Simple Scanner running (force refresh)!" if force_refresh else "Simple Scanner running — top opportunities sent!"
    return jsonify({"status": status_msg, "task_id": task_id})

@app.route('/run/leaps')
def run_leaps():
    task_id = str(uuid.uuid4())
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'

    def create_leaps_task(progress_callback):
        async def task():
            progress_callback(15)
            await leaps_scanner.main(force_refresh=force_refresh)  # Runs the full scan
            progress_callback(100)
        return task()

    run_async_with_progress(task_id, create_leaps_task)
    status_msg = "LEAPS Scanner running (force refresh)!" if force_refresh else "LEAPS Scanner running — top opportunities sent!"
    return jsonify({"status": status_msg, "task_id": task_id})


@app.route('/run/zero_dte')
def run_zero_dte():
    task_id = str(uuid.uuid4())
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'

    def create_zero_dte_task(progress_callback):
        async def task():
            progress_callback(15)
            await zero_dte_spread_scanner.main(force_refresh=force_refresh)
            progress_callback(100)
        return task()

    run_async_with_progress(task_id, create_zero_dte_task)
    status_msg = "Zero DTE Scanner running (force refresh)!" if force_refresh else "Zero DTE Scanner running — scanning for iron condors!"
    return jsonify({"status": status_msg, "task_id": task_id})


@app.route('/run/covered_calls')
def run_covered_calls():
    task_id = str(uuid.uuid4())

    def create_cc_task(progress_callback):
        async def task():
            progress_callback(15)
            await cc_main()  # Your existing main already has good structure
            # If cc_main doesn't report progress, add hooks inside it
            progress_callback(100)
        return task()

    run_async_with_progress(task_id, create_cc_task)
    return jsonify({"status": "Covered Call bot running!", "task_id": task_id})


@app.route('/run/dividends')
def run_dividends():
    task_id = str(uuid.uuid4())

    def create_div_task(progress_callback):
        async def task():
            progress_callback(20)  # Starting fetch
            # You can enhance div_main to accept callback if needed
            await div_main()
            progress_callback(100)
        return task()

    run_async_with_progress(task_id, create_div_task)
    return jsonify({"status": "Dividend report generating!", "task_id": task_id})


@app.route('/api/update_greeks')
def update_greeks_api():
    """
    Update Greeks from Schwab API for all positions in Google Sheets.

    Returns progress and result summary.
    """
    try:
        from update_greeks_from_schwab import update_greeks_in_sheet

        logger.info("Starting Greeks update from Schwab API...")
        updated_count = update_greeks_in_sheet()

        if updated_count > 0:
            return jsonify({
                'success': True,
                'updated': updated_count,
                'message': f'Updated Greeks for {updated_count} position(s)'
            })
        else:
            return jsonify({
                'success': False,
                'updated': 0,
                'message': 'No positions were updated - check logs for errors'
            })

    except Exception as e:
        logger.error(f"Greeks update failed: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'message': f'Error: {str(e)}'
        }), 500


@app.route('/run/all_scanners')
def run_all_scanners():
    """
    Run all 3 scanners in parallel for maximum efficiency.

    Phase 3 Performance Optimization:
    - Runs Wheel, LEAPS, and 0DTE scanners simultaneously
    - Uses ThreadPoolExecutor for parallel execution
    - Returns combined task_id for progress tracking
    """
    task_id = str(uuid.uuid4())
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'

    def run_scanners_parallel():
        """Execute all scanners in parallel threads"""
        try:
            progress_tracker[task_id] = {'progress': 0, 'status': 'running', 'scanners': {}}
            logger.info(f"Starting parallel scanner execution (Wheel + LEAPS + 0DTE) - Force refresh: {force_refresh}")

            start_time = time.time()

            # Create thread pool with 3 workers (one per scanner)
            with ThreadPoolExecutor(max_workers=3) as executor:
                # Submit all scanner tasks
                futures = {
                    'wheel': executor.submit(asyncio.run, simple_options_scanner.main(force_refresh=force_refresh)),
                    'leaps': executor.submit(asyncio.run, leaps_scanner.main(force_refresh=force_refresh)),
                    'zero_dte': executor.submit(asyncio.run, zero_dte_spread_scanner.main(force_refresh=force_refresh)),
                }

                # Update progress as scanners complete
                completed = 0
                for scanner_name, future in futures.items():
                    try:
                        future.result()  # Wait for completion
                        completed += 1
                        progress = int((completed / len(futures)) * 100)
                        progress_tracker[task_id]['progress'] = progress
                        progress_tracker[task_id]['scanners'][scanner_name] = 'completed'
                        logger.info(f"{scanner_name.upper()} scanner completed ({completed}/{len(futures)})")
                    except Exception as e:
                        logger.error(f"{scanner_name.upper()} scanner failed: {e}")
                        progress_tracker[task_id]['scanners'][scanner_name] = f'failed: {e}'

            elapsed = time.time() - start_time
            progress_tracker[task_id]['progress'] = 100
            progress_tracker[task_id]['status'] = 'complete'
            progress_tracker[task_id]['elapsed_time'] = f"{elapsed:.1f}s"

            logger.info(f"All scanners completed in {elapsed:.1f}s")

        except Exception as e:
            logger.error(f"Parallel scanner execution failed: {e}")
            progress_tracker[task_id]['status'] = 'error'
            progress_tracker[task_id]['error'] = str(e)
        finally:
            # Clean up after 10 minutes
            threading.Timer(600, lambda: progress_tracker.pop(task_id, None)).start()

    # Start parallel execution in background thread
    threading.Thread(target=run_scanners_parallel).start()

    status_msg = "Running all scanners in parallel (Wheel + LEAPS + 0DTE) - Force refresh!" if force_refresh else "Running all scanners in parallel (Wheel + LEAPS + 0DTE)..."
    return jsonify({
        "status": status_msg,
        "task_id": task_id,
        "info": "Check /progress/{task_id} for status"
    })

@app.route('/live/open_trades')
def live_open_trades():
    trades_result = load_trades_from_sheet()
    if trades_result is None:
        return jsonify([])
    
    df, _ = trades_result  # Unpack — we only need df here
    if df is None or df.empty:
        return jsonify([])
    
    updated_trades = []
    for _, row in df.iterrows():
        symbol = row['Symbol']
        strike = row['Strike']
        exp = row['Exp Date']  # Assume format 'MM/DD/YYYY'
        entry = row['Entry Premium']
        
        try:
            # Parse expiration for yfinance (YYYY-MM-DD)
            exp_date = datetime.strptime(exp, '%m/%d/%Y').strftime('%Y-%m-%d')
            
            tk = yf.Ticker(symbol)
            chain = tk.option_chain(exp_date)
            puts = chain.puts
            put_row = puts[puts['strike'] == strike]
            
            if not put_row.empty:
                bid = put_row['bid'].iloc[0]
                ask = put_row['ask'].iloc[0]
                mark = (bid + ask) / 2 if bid > 0 and ask > 0 else bid or ask or 0
                underlying = tk.info.get('regularMarketPrice') or tk.info.get('previousClose', 0)
                delta = put_row['delta'].iloc[0] if 'delta' in put_row.columns else row.get('Delta', 0)
                iv = put_row['impliedVolatility'].iloc[0] * 100 if 'impliedVolatility' in put_row.columns else row.get('IV', 0)
            else:
                mark = row.get('Current Mark', 0)
                underlying = row.get('Underlying Price', 0)
                delta = row.get('Delta', 0)
                iv = row.get('IV', 0)
        except:
            # Fallback to sheet values if fail
            mark = row.get('Current Mark', 0)
            underlying = row.get('Underlying Price', 0)
            delta = row.get('Delta', 0)
            iv = row.get('IV', 0)
        
        progress = ((entry - mark) / entry * 100) if entry > 0 else 0
        pl_dollar = (entry - mark) * 100  # per contract
        pl_pct = progress
        
        trade_dict = row.to_dict()
        trade_dict.update({
            'Current Mark': round(mark, 2),
            'Underlying Price': round(underlying, 2),
            'Progress to Target': round(progress, 1),
            'Current P/L $': round(pl_dollar, 2),
            'Current P/L %': round(pl_pct, 1),
            'Delta': round(delta, 2),
            'IV': round(iv, 1),
            'Bid': round(bid if 'bid' in locals() else 0, 2),
            'Ask': round(ask if 'ask' in locals() else 0, 2)
        })
        updated_trades.append(trade_dict)
    
    return jsonify(updated_trades)

@app.route('/alert/milestone/<symbol>/<float:progress>')
def milestone_alert(symbol, progress):
    if open_bot:
        msg = f"🎯 {symbol} put hit {progress:.0f}% profit target!\nCheck dashboard for details."
        asyncio.run(open_bot.send_message(chat_id=CHAT_ID, text=msg))  # Use your CHAT_ID
    return jsonify({"status": "Alert sent"})

@app.route('/run/open_trades_refresh')
def run_open_trades_refresh():
    task_id = str(uuid.uuid4())

    def create_refresh_task(progress_callback):
        async def task():
            progress_callback(10)  # Starting...

            try:
                # Run the core logic from open_trade_monitor
                df, live_ws = load_trades_from_sheet()  # Reuse your function
                if df is not None and not df.empty:
                    await update_sheet_with_live_data(df, live_ws)
                    progress_callback(80)
                    await asyncio.sleep(2)  # Let sheet settle
                    progress_callback(100)
                else:
                    progress_callback(100)  # Nothing to do
            except Exception as e:
                print(f"Open trades refresh failed: {e}")
                progress_callback(100)

        return task()

    run_async_with_progress(task_id, create_refresh_task)
    return jsonify({"status": "Refreshing Open CSPs with live data — sheet updating!", "task_id": task_id})

@app.route('/grok/analyze/<symbol>')
def grok_analyze(symbol):
    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not set"})

    # Get current price/IV for context
    try:
        tk = yf.Ticker(symbol)
        info = tk.info
        price = info.get('regularMarketPrice') or info.get('previousClose', 'N/A')
        # Rough IV estimate from first option chain date
        options = tk.options
        iv = 'N/A'
        if options:
            chain = tk.option_chain(options[0])
            iv = round(chain.calls['impliedVolatility'].mean() * 100, 1) if not chain.calls.empty else 'N/A'
    except Exception:
        price = 'N/A'
        iv = 'N/A'

    current_date = datetime.now().strftime('%B %d, %Y')

    prompt = f"""
        Current date: {current_date}
        Symbol: {symbol}
        Current price: ${price}
        Current IV: {iv}%
        Context: User runs wheel strategy on stocks and LEAPS calls. They want to know if {symbol} is a good candidate for selling cash-secured puts (CSPs) as part of a wheel strategy, and also if there is a strong bullish case for buying LEAPS calls. 
                 Review the stock and options data and provide a detailed analysis as if you were a best in the business professional options trader.
        Provide analysis on:
        - Wheel suitability/LEAPS suitability (keep concise)
        - Market sentiment, news, recent earnings, events, Key risks (keep concise)
        - Technical Levels:
            - Support
            - Resistance
            - Moving Averages (9, 50, 200)
            - RSI
            - Trend analysis
        - Optimal CSP suggestion based on current analysis. Format as bullet points. 
            Suggest:
            - Current underlying price
            - Strike
            - Premium
            - DTE
            - Bid-Ask spread
            - IV %
            - Delta
            - Theta
            - Capital needed per contract

        - Recommend possible LEAPS call options if you think there is a strong bullish case. Strategy would be to sell covered calls on a LEAPS option for more profit potential. 
            Include: For Bullish or Bearish case (only show based off market conditions) suggest (format as bullet points):
                     - Current underlying price
                     - Strike
                     - Premium
                     - DTE
                     - Capital needed per contract
                     - Dividends? Yes or No.  If Yes, include yield %.
                     - Strategy on selling the covered calls for this stock (%OTM, DTE, etc).  You can go more in depth here if you want to explain the strategy and how to implement it.  Try not to use too many acronyms.
        Format the response as:
        Format as bullet points.
        Keep under 600 words if needed for strategy of LEAPS).
        """

    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "grok-4-1-fast-reasoning",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 750
    }

    try:
        resp = requests.post(GROK_ENDPOINT, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        return jsonify({"analysis": content})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/grok/analyze_option', methods=['POST'])
def grok_analyze_option():
    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not configured"})

    data = request.get_json()

    symbol = data.get('symbol', '').upper()
    opt_type = data.get('type', 'Put')          # Put or Call
    direction = data.get('direction', 'Sell')   # Buy or Sell
    strategy = data.get('strategy', '')         # CSP, LEAPS, CC, etc.
    strike = data.get('strike')
    premium = data.get('premium')
    dte = data.get('dte')
    delta = data.get('delta')
    theta = data.get('theta')
    vega = data.get('vega')
    iv = data.get('iv')

    # Fetch current underlying price for context
    try:
        tk = yf.Ticker(symbol)
        info = tk.info
        current_price = info.get('regularMarketPrice') or info.get('previousClose') or 'N/A'
    except:
        current_price = 'N/A'

    # Calculate useful derived metrics for the prompt
    extra_context = ""
    if current_price != 'N/A' and strike:
        if opt_type == 'Put':
            distance_pct = ((current_price - strike) / current_price) * 100 if current_price > strike else 0
            extra_context += f"OTM distance: {distance_pct:.1f}%\n"
        else:  # Call
            distance_pct = ((strike - current_price) / current_price) * 100 if strike > current_price else 0
            extra_context += f"OTM distance: {distance_pct:.1f}%\n"

        if direction == 'Sell' and opt_type == 'Put':  # CSP
            capital = strike * 100
            annualized = (premium / strike) * (365 / dte) * 100 if dte > 0 else 0
            extra_context += f"Capital required per contract: ${capital:,.0f}\n"
            extra_context += f"Rough annualized return if not assigned: {annualized:.1f}%\n"

    current_date = datetime.now().strftime('%B %d, %Y')

    prompt = f"""
        Current date: {current_date}
        Underlying: {symbol} @ ${current_price}
        Trade: {direction} {opt_type} | Strike ${strike} | Premium ${premium} | DTE {dte}
        {extra_context}
        """

    if delta is not None: prompt += f"Delta: {delta:.3f}\n"
    if theta is not None: prompt += f"Theta: {theta:.3f}\n"
    if vega is not None: prompt += f"Vega: {vega:.3f}\n"
    if iv is not None: prompt += f"Implied Volatility: {iv:.1f}%\n"

    # Strategy-specific instructions
    if strategy == 'CSP':
        prompt += "\nThis is a Cash-Secured Put (wheel strategy). Focus on:\n"
        prompt += "- Probability of profit / probability of assignment\n- Downside breakeven and protection\n- Annualized return\n- Risk of early assignment\n- Comparison to simply buying the stock\n"
    elif strategy == 'LEAPS':
        prompt += "\nThis is a LEAPS long call (stock replacement). Focus on:\n"
        prompt += "- Effective leverage vs buying shares outright\n- Delta exposure and cost basis\n- Time decay risk over long horizon\n- Breakeven and max loss\n"
    elif strategy == 'CC':
        prompt += "\nThis is a Covered Call. Focus on:\n"
        prompt += "- Income vs upside cap\n- Probability of being called away\n- If-called vs if-not-called scenarios\n"
    else:
        prompt += f"\nGeneral analysis of {direction.lower()}ing a {opt_type.lower()} option.\n"

    prompt += """
        Provide a concise, actionable analysis in bullet points:
        • Trade summary
        • Estimated probability of profit 
        • Breakeven point(s)
        • Expected return per contract / annualized (if applicable)
        • Key risks
        • Comparison to alternatives
        • Final recommendation: Strong Yes, Yes, Neutral, Caution, No
        Keep total response under 400 words.
        """

    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "grok-4-1-fast-reasoning",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 500
    }

    try:
        resp = requests.post(GROK_ENDPOINT, headers=headers, json=payload, timeout=40)
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content'].strip()
        return jsonify({"analysis": content})
    except Exception as e:
        return jsonify({"error": f"Grok API error: {str(e)}"})
    
@app.route('/grok/trade_analysis', methods=['POST'])
def grok_trade_analysis():
    try:
        # Load closed trades
        gc = gspread.service_account(filename="google-credentials.json")
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        history_ws = sh.worksheet("Trade_History")
        records = history_ws.get_all_records()
        closed_trades = [r for r in records if r.get('Exit Date') and str(r.get('Exit Date', '')).strip()]

        if not closed_trades:
            return {"analysis": "No closed trades found for analysis."}

        # Build rich trade data
        trades_text = ""
        for i, t in enumerate(closed_trades, 1):
            symbol = t.get('Symbol', 'N/A')
            strike = safe_float(t.get('Strike', 0))
            exp_str = t.get('Exp Date', 'N/A')
            entry_prem = safe_float(t.get('Entry Premium', 0))
            exit_prem = safe_float(t.get('Exit Premium', 0)) or 0
            pl = safe_float(t.get('Net Profit $', 0))
            days_held = safe_int(t.get('Days Held', 0))
            iv_entry = t.get('IV at Entry', 'N/A')
            rsi_entry = t.get('RSI at Entry', 'N/A')

            trades_text += f"""
                Trade #{i}: {symbol} ${strike:.2f}P exp {exp_str}
                Entry: ${entry_prem:.2f} | Exit: ${exit_prem:.2f} | Held: {days_held} days
                P/L: ${pl:+,.0f} | Result: {'WIN' if pl > 0 else 'LOSS' if pl < 0 else 'BE'}
                IV: {iv_entry} | RSI: {rsi_entry}
                """

        prompt = f"""
            You are an elite wheel strategy analyst reviewing my closed CSP trades.

            Here are my last {len(closed_trades)} closed trades:

            {trades_text}

            Analyze and report in clean Markdown with proper tables:

            1. Overall Performance
            - Win rate, total P/L, avg P/L, best/worst trade

            2. Patterns by Category
            - DTE at entry ranges (0-10, 11-21, 22-45, 45+)
            - IV at entry (high >50%, medium 30-50%, low <30%)
            - RSI at entry (low <40, mid 40-60, high >60)
            - Sector/ticker groups

            3. Best Setups
            - Highest win rate combinations
            - Highest ROI patterns
            - Most consistent performers

            4. Future Recommendations
            - My personal "winning DNA"
            - Specific setups to target
            - Risks/patterns to avoid

            Use **proper Markdown tables** (with | separators and header row).
            Be direct, data-driven, and actionable.
            Keep under 600 words.
            """

        analysis = get_grok_analysis(prompt)

        # Clean up any malformed table lines from Grok
        lines = analysis.split('\n')
        cleaned = []
        in_table = False
        for line in lines:
            line = line.rstrip('\\')  # Remove trailing \
            if '|' in line and line.count('|') >= 2:
                in_table = True
                cleaned.append(line)
            elif in_table and line.strip() == '':
                in_table = False
                cleaned.append('')
            else:
                cleaned.append(line)

        analysis = '\n'.join(cleaned)

        return {"analysis": analysis}

    except Exception as e:
        return {"analysis": f"Analysis failed: {str(e)}"}

@app.route('/calculate/cc', methods=['POST'])
def calculate_cc_live():
    data = request.json
    symbol = data.get('Symbol', '').upper().strip()
    shares = int(data.get('shares', 100))
    safety = data.get('safety', 'safe')
    dte_pref = data.get('dte', '21-60')

    if not symbol:
        return jsonify({"error": "Symbol required"})

    result = calculate_conservative_cc(symbol, shares, safety, dte_pref)
    
    return jsonify(result)

@app.post("/csp/suggest_roll")
def suggest_roll():
    # data = asyncio.run(request.json())
    # symbol = data.get('Symbol', '').upper().strip()
    # strike = data.get('Strike')
    # exp_date = data.get('Exp_Date')
    try:
        # Load closed trades
        trades_result = load_trades_from_sheet()
        if trades_result is None:
            return jsonify([])
        
        df, _ = trades_result  # Unpack — we only need df here
        if df is None or df.empty:
            return jsonify([])
    
        for _, row in df.iterrows():
            symbol = row['Symbol']
            strike = row['Strike']
            exp_date = row['Exp Date']  # Assume format 'MM/DD/YYYY'

        prompt = f"Suggest optimal roll for current {symbol} CSP position based on performance of current CSP option in {symbol} at {strike} strike price, and {exp_date} expiration date. Include new strike, DTE, expected credit."
        suggestion = get_grok_analysis(prompt)
        result = {"suggestion": suggestion}
        return result
    except Exception as e:
        return {"result": f"Rollover Analysis Failed: {str(e)}"}
    
@app.post("/csp/mark_closed")
def mark_trade_closed():
    try:
        data = request.json
        symbol = data.get('symbol')
        strike = data.get('strike')
        exp_date = data.get('exp_date')

        if not symbol or not strike:
            return {"status": "Error: Missing symbol or strike"}, 400

        # Load Live_Trades
        gc = gspread.service_account(filename="google-credentials.json")
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        live_ws = sh.worksheet("Live_Trades")
        history_ws = sh.worksheet("Trade_History")

        live_records = live_ws.get_all_records()
        row_to_move = None
        row_index = None

        for i, row in enumerate(live_records, start=2):  # +2 for header + 1-based
            if (row.get('Symbol', '').strip() == symbol.strip() and
                safe_float(row.get('Strike', 0)) == safe_float(strike) and
                row.get('Exp Date', '').strip() == exp_date.strip()):
                row_to_move = row
                row_index = i
                break

        if not row_to_move:
            return {"status": "Trade not found in Live_Trades"}, 404

        # Prepare row for History
        today = datetime.now(ET_TZ).strftime('%m/%d/%Y')
        history_row = row_to_move.copy()
        history_row['Exit Date'] = today
        # Optional: clear or set defaults
        history_row['Exit Premium'] = ''
        history_row['Net Profit $'] = ''
        history_row['Notes'] = ''

        # Append to Trade_History
        history_ws.append_row(list(history_row.values()))

        # Delete from Live_Trades
        live_ws.delete_rows(row_index)

        return {"status": f"{symbol} marked as closed and moved to Trade History!"}

    except Exception as e:
        logging.error(f"Mark closed failed: {e}")
        return {"status": "Server error — check logs"}, 500

@app.get("/grok/dna")
def get_trading_dna():
    try:
        # Load closed trades
        gc = gspread.service_account(filename="google-credentials.json")
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        history_ws = sh.worksheet("Trade_History")
        records = history_ws.get_all_records()
        closed_trades = [r for r in records if r.get('Exit Date') and str(r.get('Exit Date', '')).strip()]

        if len(closed_trades) < 5:
            return {"dna": "🔍 <strong>Not enough closed trades for DNA analysis</strong><br><br>"
                           "Need at least 5 completed trades to identify patterns.<br>"
                           "Keep building your history!"}

        # Build concise trade list (limit to avoid token overflow)
        trades_summary = ""
        for i, t in enumerate(closed_trades[:20], 1):
            symbol = t.get('Symbol', 'N/A')
            strike = safe_float(t.get('Strike', 0))
            pl = safe_float(t.get('Net Profit $', 0))
            days = safe_int(t.get('Days Held', 0))
            result = "WIN" if pl > 0 else "LOSS" if pl < 0 else "BE"
            trades_summary += f"{i}. {symbol} ${strike:.0f}P | Held {days}d | P/L ${pl:+,.0f} | {result}\n"

        prompt = f"""
            You are an elite wheel trading coach analyzing my closed trades to discover my personal "Trading DNA".

            My last {len(closed_trades)} closed trades:

            {trades_summary}

            Identify my winning patterns:
            • Highest win rate DTE ranges
            • Best delta/IV/RSI conditions
            • Top performing sectors/tickers
            • Most profitable setup combinations
            • Specific recommendations for future trades

            Be direct, actionable, and specific.
            Use bullets.
            Keep under 300 words.
            """

        analysis = get_grok_analysis(prompt)

        # Final fallback
        if not analysis or "unavailable" in analysis.lower() or len(analysis.strip()) < 10:
            analysis = ("• Focus on 21-45 DTE puts with delta 0.20-0.35\n"
                        "• Prefer high IV (>40%) and low RSI (<50)\n"
                        "• Tech and energy sectors showing strength\n"
                        "• Avoid very short DTE (<7 days) unless high conviction\n"
                        "• Target 50-70% profit take for consistency")

        # Fix Python f-string syntax (cannot include backslash in expression)
        analysis_html = analysis.replace('\n', '<br>')
        return {"dna": f"<strong style='color:#34d399; font-size:1.4rem;'>🧬 Your Personal Trading DNA</strong><br><br>"
                       f"{analysis_html}"}

    except Exception as e:
        logging.error(f"DNA analysis error: {e}")
        return {"dna": "<strong style='color:#fb923c;'>⚠️ Analysis failed</strong><br><br>"
                       "Check server logs or try again later."}

@app.route('/grok/update_csp')
def update_csp_insight():
    symbol = request.args.get('symbol')
    try:
        strike = float(request.args.get('strike'))
    except:
        return jsonify({"error": "Invalid strike"})
    exp = request.args.get('exp')  # MM/DD/YYYY
    
    if not symbol or not exp:
        return jsonify({"error": "Missing parameters"})
    
    try:
        # Load the specific trade from sheet for entry data
        ws = get_sheet()
        if not ws:
            return jsonify({"error": "Sheet connection failed"})
        
        records = ws.get_all_records()

        # More flexible date matching - try multiple formats
        def dates_match(sheet_date, target_date):
            if not sheet_date or not target_date:
                return False
            # Try exact match first
            if str(sheet_date).strip() == str(target_date).strip():
                return True
            # Try parsing both dates to compare
            try:
                formats = ['%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y', '%Y/%m/%d']
                sheet_dt = None
                target_dt = None

                for fmt in formats:
                    try:
                        sheet_dt = datetime.strptime(str(sheet_date).strip(), fmt).date()
                        break
                    except:
                        continue

                for fmt in formats:
                    try:
                        target_dt = datetime.strptime(str(target_date).strip(), fmt).date()
                        break
                    except:
                        continue

                if sheet_dt and target_dt:
                    return sheet_dt == target_dt
            except:
                pass
            return False

        # Add debug logging
        logger.info(f"Looking for trade: Symbol={symbol.upper()}, Strike={strike}, Exp={exp}")
        logger.info(f"Found {len(records)} records in sheet")

        # Try to find the trade
        trade = next((r for r in records if
                      r.get('Symbol', '').strip().upper() == symbol.upper() and
                      abs(safe_float(r.get('Strike')) - strike) < 0.01 and  # Float comparison tolerance
                      dates_match(r.get('Exp Date', ''), exp)), None)

        if not trade:
            # Log what we found to help debug
            matching_symbols = [r for r in records if r.get('Symbol', '').strip().upper() == symbol.upper()]
            logger.warning(f"Trade not found. Found {len(matching_symbols)} records with symbol {symbol}")
            if matching_symbols:
                logger.warning(f"Sample: {matching_symbols[0]}")
            return jsonify({"error": f"Trade not found in sheet: {symbol} ${strike} exp {exp}. Check that symbol, strike, and expiration date match exactly."})
        
        entry_premium = safe_float(trade.get('Entry Premium'))
        contracts = safe_int(trade.get('Contracts Qty', 1))
        entry_date = safe_date(trade.get('Entry Date'))
        
        # Convert exp to YYYY-MM-DD for yfinance
        exp_dt = datetime.strptime(exp, '%m/%d/%Y')
        exp_date = exp_dt.strftime('%Y-%m-%d')
        dte = max((exp_dt.date() - datetime.now().date()).days, 0)
        days_open = max((datetime.now().date() - entry_date).days, 1) if entry_date else 1
        
        # Fetch latest quote via yfinance
        tk = yf.Ticker(symbol)
        try:
            chain = tk.option_chain(exp_date)
            puts = chain.puts
            put_row = puts[puts['strike'] == strike]
            
            if put_row.empty:
                return jsonify({"error": "Option data not found"})
            
            bid = put_row['bid'].iloc[0]
            ask = put_row['ask'].iloc[0]
            mark = put_row['lastPrice'].iloc[0] or (bid + ask) / 2 if (bid + ask) > 0 else 0
            iv = put_row['impliedVolatility'].iloc[0] * 100 if 'impliedVolatility' in put_row.columns else 0
            delta = abs(put_row['delta'].iloc[0]) if 'delta' in put_row.columns else 0.3  # Fallback approx
            
            underlying = tk.info.get('regularMarketPrice') or tk.info.get('previousClose', 0)
        except Exception as fetch_e:
            return jsonify({"error": f"Quote fetch failed: {str(fetch_e)}"})
        
        # Calculate metrics
        profit_captured = max(entry_premium - mark, 0)
        pl_dollars = profit_captured * 100 * contracts
        progress_pct = (profit_captured / entry_premium * 100) if entry_premium > 0 else 0
        
        # Build updated prompt focused on revisions/updates
        grok_prompt = f"""
        Update strategy for open Short {symbol} ${strike:.2f} PUT exp {exp}
        
        Entry credit: ${entry_premium:.2f}, Current mark: ${mark:.2f} ({progress_pct:.1f}% profit captured)
        
        DTE: {dte}, Days open: {days_open}
        
        Underlying: ${underlying:.2f}, Delta: {delta:.2f}, IV: {iv:.1f}%
        
        P/L: ${pl_dollars:,.0f}
        
        Provide revised advice: Any updates to original plan based on latest data? 
        Should I close early, hold to expiration, roll out/up/down? New risks or opportunities?
        
        Keep concise, under 80 words. Be actionable.
        """
        
        analysis = get_grok_analysis(grok_prompt)
        timestamp = datetime.now(ET_TZ).strftime("%I:%M %p ET")
        analysis_with_time = f"[{timestamp}] {analysis}"
        
        return jsonify({"analysis": analysis_with_time})
    
    except Exception as e:
        logging.error(f"Update CSP insight failed: {e}", exc_info=True)
        return jsonify({"error": "Failed to get updated insight — try again later"})
    
@app.route('/refresh_levels/<symbol>')
def refresh_levels(symbol):
    cache_file = Path('simple_options_scanner.py').parent / 'scanner_caches' / f"{symbol.upper()}_levels_cache.json"
    if cache_file.exists():
        cache_file.unlink()
        return jsonify({"status": f"Cache cleared for {symbol} - next scan will recalculate."})
    return jsonify({"status": f"No cache found for {symbol}"})

    
@app.get("/grok/market_pulse")
def get_market_pulse():
    current_date = datetime.now(ET_TZ).strftime('%B %d, %Y')
    try:
        # Load your watchlist symbols for recommendations
        from simple_options_scanner import SIMPLE_WATCHLIST

        # Build prompt for market pulse - make it work with get_grok_analysis prepend
        context_prompt = f"""
        Current date: {current_date}
        Market context: Provide overall market pulse and sentiment analysis.

        Available watchlist symbols: {', '.join(sorted(set(SIMPLE_WATCHLIST)))}

        Please provide a concise market pulse covering:
        - Overall market sentiment (bullish/bearish/neutral)
        - Strongest sectors right now
        - Top 5 wheel strategy picks from the watchlist above (focus on strong sectors)
        - Quick actionable advice/recommendations

        Format your response with these headers:
        **Overall Sentiment**
        **Strongest Sectors**
        **Best Wheel Picks**
        **Quick Advice**

        Keep under 350 words. Be actionable and specific.
        """

        # Call Grok analysis with SPY as the symbol and market pulse context
        analysis = get_grok_analysis(symbol="SPY", context=context_prompt)

        if not analysis:
            analysis = "Market pulse unavailable — try again soon."

        return {"pulse": analysis}

    except Exception as e:
        return {"pulse": "Pulse failed — check server"}
    
@app.post("/refresh_dashboard")
def refresh_dashboard():
    try:
        # Run the full dashboard generation in a background thread
        threading.Thread(target=lambda: asyncio.run(run_all_bots_and_generate()), daemon=True).start()
        return {"status": "Dashboard refresh started! New data in ~2 minutes."}
    except Exception as e:
        return {"status": f"Refresh failed: {str(e)}"}, 500

# Helper to run the async generation
async def run_all_bots_and_generate():
    from generate_dashboard import run_all_bots, generate_html
    await run_all_bots()  # your existing function
    generate_html()       # your existing function

# One-off wheel scan (since wheel_alert_loop is infinite)
# async def one_wheel_scan():
#     best_opportunities, regime, grok_sentiment, grok_summary = await find_best_puts_async()
    
#     sentiment_emoji = "🚀" if "STRONG" in grok_sentiment else "📈" if "BULL" in grok_sentiment else "😐" if "NEUTRAL" in grok_sentiment else "⚠️" if "CAUTIOUS" in grok_sentiment else "🔴"

#     if best_opportunities:
#         header = (
#             f"{sentiment_emoji} WHEEL SCAN (Manual Trigger)\n"
#             f"{grok_summary}\n\n"
#             f"🎯 {regime['name']} | Found {len(best_opportunities)} opportunities"
#         )
#         await wheel_send(header)
#         for opp in best_opportunities:
#             # Reuse your existing alert formatting logic here or simplify
#             msg = f"{opp['tier_label']} {opp['symbol']} — Top put: ${opp['candidates'][0]['strike']:.0f}P @ ${opp['candidates'][0]['premium']:.2f} ({opp['candidates'][0]['annualized']:.1f}% ann)"
#             await wheel_send(msg)
#             await asyncio.sleep(1)

# ==================== PHASE 4 & 5 ENDPOINTS ====================

@app.route('/api/portfolio_greeks')
def get_portfolio_greeks():
    """
    Calculate and return portfolio-wide Greeks.

    Returns aggregated delta, theta, vega, gamma with risk alerts.
    """
    try:
        # Load current trades from Google Sheets
        trades_result = load_trades_from_sheet()

        if trades_result is None:
            return jsonify({
                'net_delta': 0,
                'net_theta': 0,
                'net_vega': 0,
                'net_gamma': 0,
                'alerts': [],
                'positions_count': 0
            })

        # Unpack DataFrame and worksheet
        df, _ = trades_result

        if df is None or df.empty:
            return jsonify({
                'net_delta': 0,
                'net_theta': 0,
                'net_vega': 0,
                'net_gamma': 0,
                'alerts': [],
                'positions_count': 0
            })

        # Convert trades to PositionGreeks objects with live Greeks from Schwab API
        from schwab_utils import get_client
        client = get_client()

        positions = []
        for _, row in df.iterrows():
            try:
                symbol = str(row.get('Symbol', ''))
                strike = safe_float(row.get('Strike', 0))
                exp_date = str(row.get('Exp Date', ''))
                quantity = int(row.get('Quantity', 0))

                # STRATEGY: Try sheet values first, then live API as enhancement
                # This ensures we always have Greeks even if API fails

                # Get Greeks from Google Sheets first (primary source)
                delta_sheet = safe_float(row.get('Delta', 0))
                gamma_sheet = safe_float(row.get('Gamma', 0))
                theta_sheet = safe_float(row.get('Theta', 0))
                vega_sheet = safe_float(row.get('Vega', 0))

                # Start with sheet values
                delta = delta_sheet
                gamma = gamma_sheet
                theta = theta_sheet
                vega = vega_sheet

                logger.debug(f"Sheet Greeks for {symbol}: delta={delta:.3f}, theta={theta:.3f}, "
                           f"gamma={gamma:.3f}, vega={vega:.3f}")

                # Try to enhance with live API Greeks if sheet values are zero or missing
                if delta == 0 and theta == 0:
                    logger.info(f"Sheet Greeks are zero for {symbol}, attempting live API fetch...")
                    try:
                        # Parse expiration date from various formats
                        from datetime import datetime as dt
                        if '/' in exp_date:
                            exp_dt = dt.strptime(exp_date, '%m/%d/%Y')
                        else:
                            exp_dt = dt.strptime(exp_date, '%Y-%m-%d')

                        exp_formatted = exp_dt.strftime('%m%d%y')
                        strike_formatted = f"{int(strike)}" if strike == int(strike) else f"{strike:.1f}".replace('.', '')
                        occ_symbol = f"{symbol}_{exp_formatted}P{strike_formatted}"

                        logger.debug(f"Fetching live Greeks for {occ_symbol}")
                        response = client.get_quote(occ_symbol)

                        if response.status_code == 200:
                            quote_data = response.json()

                            if occ_symbol in quote_data:
                                quote = quote_data[occ_symbol]
                                api_delta = safe_float(quote.get('delta', 0))
                                api_gamma = safe_float(quote.get('gamma', 0))
                                api_theta = safe_float(quote.get('theta', 0))
                                api_vega = safe_float(quote.get('vega', 0))

                                if api_delta != 0 or api_theta != 0:
                                    # Use API values if they're non-zero
                                    delta = api_delta
                                    gamma = api_gamma
                                    theta = api_theta
                                    vega = api_vega
                                    logger.info(f"✅ Using live API Greeks for {symbol}: delta={delta:.3f}, theta={theta:.3f}")
                                else:
                                    logger.warning(f"API returned zero Greeks for {occ_symbol}")
                            else:
                                logger.warning(f"Option {occ_symbol} not found in API response")
                        else:
                            logger.warning(f"Schwab API returned {response.status_code} for {occ_symbol}")

                    except Exception as e:
                        logger.warning(f"Live API fetch failed for {symbol}: {e}")
                else:
                    logger.debug(f"Using sheet Greeks for {symbol} (non-zero values)")

                # Get underlying price from sheet or use 0
                underlying_price = safe_float(row.get('Current Price', 0))

                pos = PositionGreeks(
                    symbol=symbol,
                    quantity=quantity,
                    delta=delta,
                    gamma=gamma,
                    theta=theta,
                    vega=vega,
                    strike=strike,
                    expiration=exp_date,
                    underlying_price=underlying_price,
                    position_type='put'
                )
                positions.append(pos)

                logger.debug(f"Added position: {symbol} {quantity}x delta={delta:.3f}, theta={theta:.3f}, "
                           f"gamma={gamma:.3f}, vega={vega:.3f}")

            except Exception as e:
                logger.error(f"Error creating PositionGreeks for {row.get('Symbol', 'Unknown')}: {e}")
                continue

        # Calculate portfolio Greeks
        portfolio = calculate_portfolio_greeks(positions)

        # Get risk alerts
        alerts = portfolio.get_alerts()

        logger.info(f"Portfolio Greeks calculated: Delta={portfolio.net_delta:.2f}, Theta={portfolio.net_theta:.2f}, "
                   f"Vega={portfolio.net_vega:.2f}, Gamma={portfolio.net_gamma:.3f} from {len(positions)} positions")

        if portfolio.net_delta == 0 and portfolio.net_theta == 0:
            logger.warning("⚠️ All Greeks are zero - check if positions have valid Greek values or if API calls failed")

        return jsonify({
            'net_delta': round(portfolio.net_delta, 2),
            'net_theta': round(portfolio.net_theta, 2),
            'net_vega': round(portfolio.net_vega, 2),
            'net_gamma': round(portfolio.net_gamma, 3),
            'alerts': alerts,
            'positions_count': len(positions),
            'symbols': list(set([p.symbol for p in positions]))
        })

    except Exception as e:
        logger.error(f"Portfolio Greeks calculation failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/exit_targets')
def get_exit_targets():
    """
    Calculate dynamic exit targets for all open positions.

    Returns profit target and stop loss levels for each trade.
    """
    try:
        # Load current trades
        trades_result = load_trades_from_sheet()

        if trades_result is None:
            return jsonify({'positions': []})

        # Unpack DataFrame and worksheet
        df, _ = trades_result

        if df is None or df.empty:
            return jsonify({'positions': []})

        exit_data = []

        for _, row in df.iterrows():
            try:
                symbol = str(row.get('Symbol', ''))
                strike = safe_float(row.get('Strike', 0))
                entry_premium = safe_float(row.get('Entry Premium', 0))
                expiration = str(row.get('Exp Date', ''))

                # Convert expiration to YYYY-MM-DD format for API call
                from datetime import datetime as dt
                try:
                    if '/' in expiration:
                        exp_dt = dt.strptime(expiration, '%m/%d/%Y')
                    else:
                        exp_dt = dt.strptime(expiration, '%Y-%m-%d')
                    expiration_formatted = exp_dt.strftime('%Y-%m-%d')
                except:
                    expiration_formatted = expiration

                # Fetch LIVE quote for accurate P&L calculation
                from order_execution import get_option_bid_ask
                live_quote = get_option_bid_ask(symbol, strike, expiration_formatted)

                if live_quote and live_quote.get('ask', 0) > 0:
                    # Use live ask price (what we'd pay to close)
                    current_premium = live_quote['ask']
                    logger.debug(f"Using live ask for {symbol} ${strike}P: ${current_premium:.2f}")
                else:
                    # Fallback to sheet value if API fails
                    current_premium = safe_float(row.get('Current Premium', 0))
                    logger.warning(f"Using sheet fallback for {symbol} ${strike}P: ${current_premium:.2f}")

                # Use Current Price from sheet (already fetched by open_trade_monitor)
                current_price = safe_float(row.get('Current Price', 0))
                current_iv = safe_float(row.get('IV', 0.30))

                # Calculate exit targets
                target = calculate_exit_targets(
                    entry_premium=entry_premium,
                    current_premium=current_premium,
                    strike=strike,
                    underlying_price=current_price,
                    expiration_date=expiration,
                    current_iv=current_iv
                )

                exit_data.append({
                    'symbol': symbol,
                    'strike': strike,
                    'recommendation': target.recommendation,
                    'profit_target_pct': target.profit_target_pct,
                    'stop_loss_pct': target.stop_loss_pct,
                    'current_pnl_pct': target.current_pnl_pct,
                    'reasoning': target.reasoning,
                    'should_roll': target.should_roll,
                    'context': target.context
                })

            except Exception as e:
                logger.error(f"Exit target calculation failed for {row.get('Symbol', 'Unknown')}: {e}")
                continue

        return jsonify({'positions': exit_data})

    except Exception as e:
        logger.error(f"Exit targets failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/trade_performance')
def get_trade_performance():
    """
    Get trade performance summary from Google Sheets Trade History.

    Returns win rate, profit factor, expectancy, and trade statistics.
    """
    try:
        # Load Trade History from Google Sheets
        gc = gspread.service_account(filename='google-credentials.json')
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        history_ws = sh.worksheet("Trade_History")
        trades = history_ws.get_all_records()

        # Validate required columns exist
        required_columns = ['Exit Date', 'Net Profit $', 'DTE', 'Days Held']
        if trades:
            first_trade = trades[0]
            missing_columns = [col for col in required_columns if col not in first_trade]

            # Try common alternative column names
            column_mapping = {
                'Net Profit $': ['Net Profit $', 'Net_Profit', 'Profit', 'Net Profit'],
                'DTE': ['DTE', 'Days_To_Expiry', 'Days To Expiry'],
                'Days Held': ['Days Held', 'Days_Held', 'Hold Duration']
            }

            if missing_columns:
                logger.warning(f"Missing columns in Trade_History: {missing_columns}")
                logger.info(f"Available columns: {list(first_trade.keys())}")

                # Try to map alternative column names
                for required_col in missing_columns:
                    alternatives = column_mapping.get(required_col, [])
                    found_col = next((alt for alt in alternatives if alt in first_trade), None)
                    if found_col and found_col != required_col:
                        logger.info(f"Mapping '{found_col}' to '{required_col}'")
                        for trade in trades:
                            trade[required_col] = trade.get(found_col, 0)

        # Filter closed trades (those with Exit Date)
        closed_trades = [t for t in trades if t.get('Exit Date')]

        logger.info(f"Loaded {len(trades)} total trades, {len(closed_trades)} closed trades from Trade_History")

        if not closed_trades:
            logger.warning("No closed trades found in Trade_History (trades need Exit Date populated)")
            return jsonify({
                'total_trades': 0,
                'win_rate': 0.0,
                'profit_factor': 0.0,
                'total_pnl': 0.0,
                'expectancy': 0.0
            })

        # Calculate statistics
        total_trades = len(closed_trades)

        # Separate winners and losers
        winners = [t for t in closed_trades if safe_float(t.get('Net Profit $', 0)) > 0]
        losers = [t for t in closed_trades if safe_float(t.get('Net Profit $', 0)) <= 0]

        # Win Rate
        win_rate = (len(winners) / total_trades * 100) if total_trades > 0 else 0.0

        # Total P&L
        total_pnl = sum(safe_float(t.get('Net Profit $', 0)) for t in closed_trades)

        # Total wins and losses
        total_wins = sum(safe_float(t.get('Net Profit $', 0)) for t in winners)
        total_losses = sum(abs(safe_float(t.get('Net Profit $', 0))) for t in losers)

        # Profit Factor (cap at 999.99 to avoid infinity)
        if total_losses > 0:
            profit_factor = total_wins / total_losses
        elif total_wins > 0:
            profit_factor = 999.99  # Cap instead of infinity
        else:
            profit_factor = 0.0

        # Average winner and loser
        avg_winner = (total_wins / len(winners)) if winners else 0.0
        avg_loser = (total_losses / len(losers)) if losers else 0.0

        # Expectancy
        win_rate_decimal = win_rate / 100
        loss_rate = 1 - win_rate_decimal
        expectancy = (avg_winner * win_rate_decimal) - (avg_loser * loss_rate)

        # Calculate average DTE and days held
        avg_dte = sum(safe_float(t.get('DTE', 0)) for t in closed_trades) / total_trades if total_trades > 0 else 0
        avg_days_held = sum(safe_float(t.get('Days Held', 0)) for t in closed_trades) / total_trades if total_trades > 0 else 0

        logger.info(f"Performance calculated from {total_trades} trades: {len(winners)} wins, {len(losers)} losses")

        return jsonify({
            'total_trades': total_trades,
            'wins': len(winners),
            'losses': len(losers),
            'win_rate': round(win_rate, 1),
            'profit_factor': round(profit_factor, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_winner, 2),
            'avg_loss': round(avg_loser, 2),
            'expectancy': round(expectancy, 2),
            'avg_dte': round(avg_dte, 1),
            'avg_days_held': round(avg_days_held, 1)
        })

    except Exception as e:
        logger.error(f"Trade performance failed: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/recent_trades')
def get_api_recent_trades():
    """
    Get recent trades from Google Sheets (live data).

    Query params:
    - limit: Number of trades to return (default 10)
    - status: Filter by status ('open', 'closed', 'all')
    """
    try:
        limit = int(request.args.get('limit', 10))

        # Load from Google Sheets instead of database
        trades_result = load_trades_from_sheet()

        if trades_result is None:
            return jsonify({'trades': []})

        df, _ = trades_result

        if df is None or df.empty:
            return jsonify({'trades': []})

        # Convert DataFrame to list of dicts for API response
        trades = []
        for _, row in df.iterrows():
            symbol = str(row.get('Symbol', ''))
            strike = safe_float(row.get('Strike', 0))
            entry_premium = safe_float(row.get('Entry Premium', 0))
            expiration = str(row.get('Exp Date', ''))
            quantity = int(row.get('Quantity', 0))

            # Try to get live current premium for accurate P&L
            try:
                # Convert expiration format
                from datetime import datetime as dt
                if '/' in expiration:
                    exp_dt = dt.strptime(expiration, '%m/%d/%Y')
                else:
                    exp_dt = dt.strptime(expiration, '%Y-%m-%d')
                expiration_formatted = exp_dt.strftime('%Y-%m-%d')

                # Fetch live quote
                from order_execution import get_option_bid_ask
                live_quote = get_option_bid_ask(symbol, strike, expiration_formatted)

                if live_quote and live_quote.get('ask', 0) > 0:
                    current_premium = live_quote['ask']
                else:
                    # Fallback to sheet
                    current_premium = safe_float(row.get('Current Premium', 0))
            except:
                # Fallback to sheet value on any error
                current_premium = safe_float(row.get('Current Premium', 0))

            # For puts: profit when premium decreases
            pnl = (entry_premium - current_premium) * quantity * 100

            trades.append({
                'symbol': symbol,
                'strike': strike,
                'status': 'open',  # All sheet trades are open
                'entry_date': str(row.get('Entry Date', '')),
                'entry_premium': entry_premium,
                'quantity': quantity,
                'profit_loss': pnl,
                'current_premium': current_premium
            })

        # Sort by entry date, most recent first
        trades.sort(key=lambda x: x['entry_date'], reverse=True)

        return jsonify({'trades': trades[:limit]})

    except Exception as e:
        logger.error(f"Recent trades failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/position_size', methods=['POST'])
def calculate_api_position_size():
    """
    Calculate recommended position size for a trade.

    POST body:
    {
        "account_value": 50000,
        "strike_price": 175.0,
        "grok_score": 8,
        "win_rate": 65.0
    }
    """
    try:
        data = request.get_json()

        account_value = data.get('account_value', 50000)
        strike_price = data.get('strike_price', 100)
        grok_score = data.get('grok_score')
        win_rate = data.get('win_rate')

        sizing = calculate_position_size(
            account_value=account_value,
            strike_price=strike_price,
            grok_score=grok_score,
            win_rate=win_rate
        )

        return jsonify({
            'recommended_contracts': sizing.recommended_contracts,
            'capital_required': sizing.capital_required,
            'risk_amount': sizing.risk_amount,
            'confidence_multiplier': sizing.confidence_multiplier,
            'warnings': sizing.warnings
        })

    except Exception as e:
        logger.error(f"Position sizing failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/earnings_check/<symbol>')
def check_earnings(symbol):
    """
    Check for earnings conflicts for a symbol.

    Query params:
    - expiration: Option expiration date (YYYY-MM-DD)
    - strike: Strike price
    - underlying_price: Current stock price
    """
    try:
        from datetime import datetime

        expiration_str = request.args.get('expiration')
        strike = safe_float(request.args.get('strike', 0))
        underlying_price = safe_float(request.args.get('underlying_price', 0))

        if not expiration_str:
            return jsonify({'error': 'Missing expiration parameter'}), 400

        expiration = datetime.strptime(expiration_str, '%Y-%m-%d').date()

        recommendation = get_earnings_recommendation(
            symbol=symbol,
            expiration_date=expiration,
            strike=strike,
            underlying_price=underlying_price,
            is_open_position=False
        )

        return jsonify(recommendation)

    except Exception as e:
        logger.error(f"Earnings check failed for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/smart_alerts/run')
def run_smart_alerts():
    """
    Run smart alerts scan on all open positions.

    Checks for:
    - Exit targets hit
    - Earnings conflicts
    - Portfolio Greeks alerts
    - Performance degradation
    """
    task_id = str(uuid.uuid4())

    def create_alerts_task(progress_callback):
        async def task():
            try:
                progress_callback(20)

                # Load current trades
                trades_result = load_trades_from_sheet()

                if trades_result is None:
                    return {'status': 'error', 'message': 'Could not load trades'}

                # Unpack DataFrame
                df, _ = trades_result

                if df is None or df.empty:
                    return {'status': 'success', 'message': 'No trades to scan'}

                # Convert to format expected by smart_alerts
                trades = []
                for _, row in df.iterrows():
                    trades.append({
                        'symbol': str(row.get('Symbol', '')),
                        'strike': safe_float(row.get('Strike', 0)),
                        'entry_premium': safe_float(row.get('Entry Premium', 0)),
                        'current_premium': safe_float(row.get('Current Premium', 0)),
                        'underlying_price': safe_float(row.get('Current Price', 0)),
                        'expiration_date': str(row.get('Exp Date', '')),
                        'current_iv': safe_float(row.get('IV', 0.30))
                    })

                progress_callback(50)

                # Run alert scan
                alerts = await run_alert_scan(trades)

                progress_callback(100)

                logger.info(f"Smart alerts scan complete: {len(alerts)} alerts generated")

            except Exception as e:
                logger.error(f"Smart alerts failed: {e}")

        return task()

    run_async_with_progress(task_id, create_alerts_task)
    return jsonify({"status": "Smart alerts scan running!", "task_id": task_id})


# ==================== PHASE 2: ORDER EXECUTION ENDPOINTS ====================
# NOTE: Disabled - order_execution module not implemented yet
# The entire order execution section (lines 1551-1766) has been removed
# because the order_execution module doesn't exist
# Uncomment when ready to enable order execution features


# ==================== PHASE 4: OPTIONS CHAIN HEATMAP API ====================
def execute_sell_put():
    """
    Execute a sell-to-open put order.

    POST body:
    {
        "symbol": "AAPL",
        "strike": 175.0,
        "expiration": "2026-02-20",
        "quantity": 1,
        "limit_price": 2.50
    }

    Returns:
        JSON with success status, order ID, and message
    """
    try:
        data = request.json

        # Validation
        required_fields = ['symbol', 'strike', 'expiration', 'quantity', 'limit_price']
        for field in required_fields:
            if field not in data:
                return jsonify({'success': False, 'message': f'Missing field: {field}'}), 400

        # Safety checks
        if data['quantity'] > 10:
            return jsonify({'success': False, 'message': 'Max 10 contracts per order (safety limit)'}), 400

        if data['limit_price'] <= 0 or data['limit_price'] > 50:
            return jsonify({'success': False, 'message': 'Invalid limit price (must be 0-50)'}), 400

        # Execute order
        result = place_sell_put_order(
            symbol=data['symbol'],
            strike=float(data['strike']),
            expiration=data['expiration'],
            quantity=int(data['quantity']),
            limit_price=float(data['limit_price'])
        )

        if result['success']:
            return jsonify(result), 201
        else:
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Sell put API failed: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/order/close_put', methods=['POST'])
def execute_close_put():
    """
    Execute a buy-to-close order for existing put position.

    POST body:
    {
        "symbol": "AAPL",
        "strike": 175.0,
        "expiration": "2026-02-20",
        "quantity": 1,
        "limit_price": 1.25
    }

    Returns:
        JSON with success status, order ID, and message
    """
    try:
        data = request.json

        # Validation
        required_fields = ['symbol', 'strike', 'expiration', 'quantity', 'limit_price']
        for field in required_fields:
            if field not in data:
                return jsonify({'success': False, 'message': f'Missing field: {field}'}), 400

        # Safety checks
        if data['quantity'] > 10:
            return jsonify({'success': False, 'message': 'Max 10 contracts per order (safety limit)'}), 400

        if data['limit_price'] <= 0 or data['limit_price'] > 50:
            return jsonify({'success': False, 'message': 'Invalid limit price (must be 0-50)'}), 400

        # Execute order
        result = close_put_position(
            symbol=data['symbol'],
            strike=float(data['strike']),
            expiration=data['expiration'],
            quantity=int(data['quantity']),
            limit_price=float(data['limit_price'])
        )

        if result['success']:
            return jsonify(result), 201
        else:
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Close put API failed: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/option/quote', methods=['GET'])
def get_option_quote_api():
    """
    Get bid/ask for an option.

    Query params:
        symbol: Underlying symbol (e.g., 'AAPL')
        strike: Strike price (e.g., 175.0)
        expiration: Expiration date in YYYY-MM-DD format

    Returns:
        JSON with bid, ask, mark, last, volume, open_interest
    """
    try:
        symbol = request.args.get('symbol')
        strike = float(request.args.get('strike'))
        expiration = request.args.get('expiration')  # YYYY-MM-DD format

        if not symbol or not strike or not expiration:
            return jsonify({'error': 'Missing required parameters'}), 400

        quote = get_option_bid_ask(symbol, strike, expiration)

        return jsonify(quote)

    except Exception as e:
        logger.error(f"Option quote API failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync_positions', methods=['POST'])
def sync_positions_api():
    """
    Trigger position sync from Schwab account to Google Sheets.

    Fetches all open option positions from Schwab and adds new positions
    to the tracking sheet.

    Returns:
        JSON with success status, sync count, and details
    """
    try:
        from account_sync import sync_positions_from_schwab

        logger.info("API: Starting position sync from Schwab...")
        result = sync_positions_from_schwab()

        if result['success']:
            logger.info(f"API: Sync complete - {result['synced_count']} new positions")
            return jsonify(result), 200
        else:
            logger.error(f"API: Sync failed - {result.get('error')}")
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"Sync positions API failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/open_csps')
def get_open_csps():
    """Get updated open CSPs data for dashboard refresh"""
    try:
        from open_trade_monitor import load_trades_from_sheet, enrich_trade_with_live_data

        # Load trades
        df, _ = load_trades_from_sheet()
        if df is None or df.empty:
            return jsonify({'csps': [], 'summary': {}})

        # For now, use empty quotes (sheet data only)
        quotes = {}

        # Enrich data
        enriched_rows = []
        for _, row in df.iterrows():
            enriched = enrich_trade_with_live_data(dict(row), quotes)
            enriched_rows.append(enriched)

        # Calculate summary
        total_credit = sum(float(r.get('_total_credit', 0)) for r in enriched_rows)
        total_pl = sum(float(r.get('_pl_dollars', 0)) for r in enriched_rows)
        avg_progress = sum(float(r.get('_progress_pct', 0)) for r in enriched_rows) / len(enriched_rows) if enriched_rows else 0
        avg_dte = sum(float(r.get('_dte', 0)) for r in enriched_rows) / len(enriched_rows) if enriched_rows else 0
        total_realized_theta = sum(float(r.get('_daily_theta_decay_dollars', 0)) for r in enriched_rows)
        avg_theta_per_pos = total_realized_theta / len(enriched_rows) if enriched_rows else 0
        total_expected_decay = sum(float(r.get('_forward_theta_daily', 0)) for r in enriched_rows)
        projected_remaining = sum(float(r.get('_projected_decay', 0)) for r in enriched_rows)

        summary = {
            'positions_count': len(enriched_rows),
            'total_credit': total_credit,
            'total_pl': total_pl,
            'avg_progress': avg_progress,
            'avg_dte': avg_dte,
            'total_realized_theta': total_realized_theta,
            'avg_theta_per_pos': avg_theta_per_pos,
            'total_expected_decay': total_expected_decay,
            'projected_remaining': projected_remaining
        }

        return jsonify({'csps': enriched_rows, 'summary': summary})

    except Exception as e:
        logger.error(f"Failed to get open CSPs: {e}")
        return jsonify({'error': str(e)}), 500


# ==================== PHASE 4: OPTIONS CHAIN HEATMAP API ====================

@app.route('/api/chain_heatmap/<symbol>')
def get_chain_heatmap(symbol):
    """
    Generate options chain heatmap for a symbol.

    Query Parameters:
        contract_type: 'PUT' or 'CALL' (default: 'PUT')
        visualization: 'open_interest', 'volume', 'liquidity', 'iv_surface',
                       'delta', 'gamma', 'theta', 'vega', 'dashboard' (default: 'open_interest')
        days_out: Days from now to fetch (default: 60)

    Returns:
        JSON with heatmap HTML and DataFrame as JSON
    """
    try:
        from chain_visualizer import fetch_option_chain_data, generate_chain_heatmap
        import json

        symbol = symbol.upper()
        contract_type = request.args.get('contract_type', 'PUT').upper()
        visualization = request.args.get('visualization', 'open_interest')
        days_out = int(request.args.get('days_out', 60))

        logger.info(f"API: Generating {visualization} heatmap for {symbol} {contract_type}s")

        # Step 1: Fetch option chain data from Schwab API
        df = fetch_option_chain_data(symbol=symbol)

        if df.empty:
            logger.error(f"No option chain data found for {symbol}")
            return jsonify({'error': f'No option chain data available for {symbol}'}), 404

        # Filter by contract type if specified
        if contract_type == 'CALL':
            # Filter to only include calls (rows where call data exists)
            df = df[df['call_open_interest'].notna()]
        elif contract_type == 'PUT':
            # Filter to only include puts (rows where put data exists)
            df = df[df['put_open_interest'].notna()]

        logger.info(f"Fetched {len(df)} options for {symbol} ({contract_type})")

        # Step 2: Generate heatmap using the DataFrame
        fig_json = generate_chain_heatmap(
            df=df,
            viz_type=visualization,
            symbol=symbol
        )

        if fig_json is None:
            return jsonify({'error': 'Failed to generate heatmap'}), 500

        # Step 3: Parse the JSON figure and convert to HTML
        import plotly.graph_objects as go
        fig = go.Figure(json.loads(fig_json))
        heatmap_html = fig.to_html(include_plotlyjs='cdn', full_html=False)

        # Convert DataFrame to JSON for API response
        df_json = df.to_dict('records')

        # Summary statistics
        summary = {
            'total_strikes': len(df),
            'expirations': df['expiration'].nunique() if 'expiration' in df.columns else 0,
            'avg_open_interest': float(df['call_open_interest'].mean() if contract_type == 'CALL' else df['put_open_interest'].mean()),
            'avg_volume': float(df['call_volume'].mean() if contract_type == 'CALL' else df['put_volume'].mean()),
            'symbol': symbol,
            'contract_type': contract_type
        }

        logger.info(f"API: Heatmap generated - {len(df)} options across {summary['expirations']} expirations")

        return jsonify({
            'success': True,
            'symbol': symbol,
            'contract_type': contract_type,
            'visualization': visualization,
            'heatmap_html': heatmap_html,
            'data': df_json,
            'summary': summary
        }), 200

    except Exception as e:
        logger.error(f"Chain heatmap API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/chain_data/<symbol>')
def get_chain_data(symbol):
    """
    Get raw option chain data as JSON (no visualization).

    Query Parameters:
        contract_type: 'PUT' or 'CALL' (default: 'PUT')
        days_out: Days from now to fetch (default: 60)
        min_oi: Minimum open interest filter (optional)
        min_volume: Minimum volume filter (optional)

    Returns:
        JSON array of option data
    """
    try:
        from chain_visualizer import fetch_option_chain_data, parse_chain_to_dataframe

        symbol = symbol.upper()
        contract_type = request.args.get('contract_type', 'PUT').upper()
        days_out = int(request.args.get('days_out', 60))
        min_oi = int(request.args.get('min_oi', 0))
        min_volume = int(request.args.get('min_volume', 0))

        logger.info(f"API: Fetching chain data for {symbol} {contract_type}s")

        # Fetch and parse chain data
        chain_data = fetch_option_chain_data(symbol, contract_type, days_out)
        if not chain_data:
            return jsonify({'error': 'Failed to fetch chain data'}), 500

        df = parse_chain_to_dataframe(chain_data)
        if df.empty:
            return jsonify({'error': 'No option data found'}), 404

        # Apply filters
        if min_oi > 0:
            df = df[df['open_interest'] >= min_oi]
        if min_volume > 0:
            df = df[df['volume'] >= min_volume]

        # Convert to JSON
        data = df.to_dict('records')

        logger.info(f"API: Returning {len(data)} options for {symbol}")

        return jsonify({
            'success': True,
            'symbol': symbol,
            'contract_type': contract_type,
            'underlying_price': chain_data['underlying_price'],
            'options_count': len(data),
            'data': data
        }), 200

    except Exception as e:
        logger.error(f"Chain data API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/liquidity_analysis/<symbol>')
def get_liquidity_analysis(symbol):
    """
    Analyze option chain liquidity and return most liquid strikes.

    Query Parameters:
        contract_type: 'PUT' or 'CALL' (default: 'PUT')
        days_out: Days from now to fetch (default: 60)
        min_oi: Minimum open interest (default: 100)
        min_volume: Minimum volume (default: 50)
        limit: Max results to return (default: 20)

    Returns:
        JSON with most liquid strikes ranked by open interest
    """
    try:
        from chain_visualizer import fetch_option_chain_data, parse_chain_to_dataframe, analyze_liquidity_zones

        symbol = symbol.upper()
        contract_type = request.args.get('contract_type', 'PUT').upper()
        days_out = int(request.args.get('days_out', 60))
        min_oi = int(request.args.get('min_oi', 100))
        min_volume = int(request.args.get('min_volume', 50))
        limit = int(request.args.get('limit', 20))

        logger.info(f"API: Analyzing liquidity for {symbol} {contract_type}s")

        # Fetch and parse chain
        chain_data = fetch_option_chain_data(symbol, contract_type, days_out)
        if not chain_data:
            return jsonify({'error': 'Failed to fetch chain data'}), 500

        df = parse_chain_to_dataframe(chain_data)
        if df.empty:
            return jsonify({'error': 'No option data found'}), 404

        # Analyze liquidity
        liquid_df = analyze_liquidity_zones(df, threshold_oi=min_oi, threshold_volume=min_volume)

        # Get top N most liquid
        top_liquid = liquid_df.head(limit)

        # Convert to JSON
        data = top_liquid[['strike', 'expiration_str', 'dte', 'open_interest', 'volume', 'bid', 'ask', 'spread_pct', 'iv']].to_dict('records')

        logger.info(f"API: Found {len(liquid_df)} liquid strikes, returning top {limit}")

        return jsonify({
            'success': True,
            'symbol': symbol,
            'contract_type': contract_type,
            'underlying_price': chain_data['underlying_price'],
            'total_liquid_strikes': len(liquid_df),
            'top_liquid_strikes': data,
            'criteria': {
                'min_open_interest': min_oi,
                'min_volume': min_volume
            }
        }), 200

    except Exception as e:
        logger.error(f"Liquidity analysis API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# PHASE 5: RISK MANAGEMENT DASHBOARD API ENDPOINTS
# =============================================================================

@app.route('/api/risk/var', methods=['GET'])
def get_portfolio_var():
    """
    Calculate portfolio Value at Risk (VaR)

    Query params:
        - confidence: VaR confidence level (default 0.95)
        - time_horizon: Days (1, 5, or 10, default 1)
        - num_simulations: Monte Carlo simulations (default 10000)

    Returns:
        JSON with VaR metrics
    """
    try:
        from risk_calculator import RiskCalculator, PositionRisk
        from open_trade_monitor import load_trades_from_sheet

        # Get parameters
        confidence = float(request.args.get('confidence', 0.95))
        time_horizon = int(request.args.get('time_horizon', 1))
        num_simulations = int(request.args.get('num_simulations', 10000))
        account_value = float(request.args.get('account_value', 100000))

        logger.info(f"API: Calculating VaR with {confidence*100}% confidence, {time_horizon}d horizon")

        # Load open positions from Google Sheets
        trades_result = load_trades_from_sheet()
        if not trades_result or trades_result[0] is None or trades_result[0].empty:
            return jsonify({
                'success': False,
                'error': 'No open positions found'
            }), 404

        df, _ = trades_result

        # Convert to PositionRisk objects
        positions = []
        for _, row in df.iterrows():
            try:
                pos = PositionRisk(
                    symbol=str(row.get('Symbol', '')),
                    quantity=safe_int(row.get('Quantity', 0)),
                    strike=safe_float(row.get('Strike', 0)),
                    expiration=str(row.get('Exp Date', '')),
                    entry_premium=safe_float(row.get('Entry Premium', 0)),
                    current_premium=safe_float(row.get('Current Premium', 0)),
                    margin_requirement=safe_float(row.get('Strike', 0)) * 100 * safe_int(row.get('Quantity', 0)),
                    current_value=safe_float(row.get('Current Premium', 0)) * safe_int(row.get('Quantity', 0)) * 100,
                    unrealized_pnl=safe_float(row.get('Current P/L', 0)),
                    delta=safe_float(row.get('Delta', -0.30)),
                    theta=safe_float(row.get('Theta', 0.15)),
                    vega=safe_float(row.get('Vega', 0.20)),
                    days_to_expiration=safe_int(row.get('DTE', 30)),
                    implied_volatility=safe_float(row.get('IV', 0.25)),
                    underlying_price=safe_float(row.get('Current Price', 0))
                )
                positions.append(pos)
            except Exception as e:
                logger.warning(f"Failed to parse position: {e}")
                continue

        if not positions:
            return jsonify({
                'success': False,
                'error': 'Failed to parse any positions'
            }), 500

        # Calculate VaR
        calc = RiskCalculator(account_value)
        var_result = calc.calculate_portfolio_var(
            positions,
            confidence=confidence,
            time_horizon=time_horizon,
            num_simulations=num_simulations
        )

        logger.info(f"API: VaR calculated - ${var_result['var_amount']:,.2f} ({var_result['var_pct']:.2f}%)")

        return jsonify({
            'success': True,
            'var_metrics': var_result,
            'num_positions': len(positions)
        }), 200

    except Exception as e:
        logger.error(f"VaR API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/risk/correlation', methods=['GET'])
def get_correlation_matrix():
    """
    Calculate correlation matrix between portfolio symbols

    Query params:
        - lookback_days: Days for correlation calculation (default 90)

    Returns:
        JSON with correlation matrix
    """
    try:
        from risk_calculator import RiskCalculator, PositionRisk
        from open_trade_monitor import load_trades_from_sheet

        lookback_days = int(request.args.get('lookback_days', 90))

        logger.info(f"API: Calculating correlation matrix with {lookback_days}d lookback")

        # Load open positions
        trades_result = load_trades_from_sheet()
        if not trades_result or trades_result[0] is None or trades_result[0].empty:
            return jsonify({
                'success': False,
                'error': 'No open positions found'
            }), 404

        df, _ = trades_result

        # Convert to PositionRisk objects
        positions = []
        for _, row in df.iterrows():
            try:
                pos = PositionRisk(
                    symbol=str(row.get('Symbol', '')),
                    quantity=safe_int(row.get('Quantity', 0)),
                    strike=safe_float(row.get('Strike', 0)),
                    expiration=str(row.get('Exp Date', '')),
                    entry_premium=safe_float(row.get('Entry Premium', 0)),
                    current_premium=safe_float(row.get('Current Premium', 0)),
                    margin_requirement=0,
                    current_value=0,
                    unrealized_pnl=0,
                    delta=0, theta=0, vega=0,
                    days_to_expiration=0,
                    implied_volatility=0,
                    underlying_price=0
                )
                positions.append(pos)
            except:
                continue

        if not positions:
            return jsonify({
                'success': False,
                'error': 'Failed to parse any positions'
            }), 500

        # Calculate correlation matrix
        calc = RiskCalculator()
        corr_matrix = calc.calculate_correlation_matrix(positions, lookback_days)

        # Convert to JSON-friendly format
        symbols = corr_matrix.index.tolist()
        correlation_data = corr_matrix.values.tolist()

        logger.info(f"API: Correlation matrix calculated for {len(symbols)} symbols")

        return jsonify({
            'success': True,
            'symbols': symbols,
            'correlation_matrix': correlation_data,
            'lookback_days': lookback_days
        }), 200

    except Exception as e:
        logger.error(f"Correlation API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/risk/margin', methods=['GET'])
def get_margin_requirements():
    """
    Calculate total margin requirements and buying power

    Returns:
        JSON with margin metrics
    """
    try:
        from risk_calculator import RiskCalculator, PositionRisk
        from open_trade_monitor import load_trades_from_sheet

        account_value = float(request.args.get('account_value', 100000))

        logger.info(f"API: Calculating margin requirements")

        # Load open positions
        trades_result = load_trades_from_sheet()
        if not trades_result or trades_result[0] is None or trades_result[0].empty:
            return jsonify({
                'success': True,
                'total_margin': 0.0,
                'available_buying_power': account_value,
                'utilization_pct': 0.0,
                'by_position': {}
            }), 200

        df, _ = trades_result

        # Convert to PositionRisk objects
        positions = []
        for _, row in df.iterrows():
            try:
                pos = PositionRisk(
                    symbol=str(row.get('Symbol', '')),
                    quantity=safe_int(row.get('Quantity', 0)),
                    strike=safe_float(row.get('Strike', 0)),
                    expiration=str(row.get('Exp Date', '')),
                    entry_premium=0, current_premium=0,
                    margin_requirement=0, current_value=0, unrealized_pnl=0,
                    delta=0, theta=0, vega=0,
                    days_to_expiration=0, implied_volatility=0, underlying_price=0
                )
                positions.append(pos)
            except:
                continue

        # Calculate margin
        calc = RiskCalculator(account_value)
        margin_result = calc.calculate_margin_requirements(positions)

        logger.info(f"API: Margin utilization: {margin_result['utilization_pct']:.1f}%")

        return jsonify({
            'success': True,
            'margin_metrics': margin_result
        }), 200

    except Exception as e:
        logger.error(f"Margin API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/risk/report', methods=['GET'])
def get_full_risk_report():
    """
    Generate comprehensive portfolio risk report

    Query params:
        - account_value: Total account value (default 100000)
        - confidence: VaR confidence level (default 0.95)

    Returns:
        JSON with full risk metrics
    """
    try:
        from risk_calculator import RiskCalculator, PositionRisk
        from open_trade_monitor import load_trades_from_sheet

        account_value = float(request.args.get('account_value', 100000))
        confidence = float(request.args.get('confidence', 0.95))

        logger.info(f"API: Generating full risk report")

        # Load open positions
        trades_result = load_trades_from_sheet()
        if not trades_result or trades_result[0] is None or trades_result[0].empty:
            return jsonify({
                'success': False,
                'error': 'No open positions found'
            }), 404

        df, _ = trades_result

        # Convert to PositionRisk objects (full conversion with all fields)
        positions = []
        for _, row in df.iterrows():
            try:
                pos = PositionRisk(
                    symbol=str(row.get('Symbol', '')),
                    quantity=safe_int(row.get('Quantity', 0)),
                    strike=safe_float(row.get('Strike', 0)),
                    expiration=str(row.get('Exp Date', '')),
                    entry_premium=safe_float(row.get('Entry Premium', 0)),
                    current_premium=safe_float(row.get('Current Premium', 0)),
                    margin_requirement=safe_float(row.get('Strike', 0)) * 100 * safe_int(row.get('Quantity', 0)),
                    current_value=safe_float(row.get('Current Premium', 0)) * safe_int(row.get('Quantity', 0)) * 100,
                    unrealized_pnl=safe_float(row.get('Current P/L', 0)),
                    delta=safe_float(row.get('Delta', -0.30)),
                    theta=safe_float(row.get('Theta', 0.15)),
                    vega=safe_float(row.get('Vega', 0.20)),
                    days_to_expiration=safe_int(row.get('DTE', 30)),
                    implied_volatility=safe_float(row.get('IV', 0.25)),
                    underlying_price=safe_float(row.get('Current Price', 0))
                )
                positions.append(pos)
            except Exception as e:
                logger.warning(f"Failed to parse position: {e}")
                continue

        if not positions:
            return jsonify({
                'success': False,
                'error': 'Failed to parse any positions'
            }), 500

        # Generate risk report
        calc = RiskCalculator(account_value)
        risk_report = calc.generate_risk_report(positions, confidence)

        # Convert dataclass to dict
        report_dict = {
            'value_at_risk_1day': risk_report.value_at_risk_1day,
            'value_at_risk_5day': risk_report.value_at_risk_5day,
            'value_at_risk_pct': risk_report.value_at_risk_pct,
            'total_margin_requirement': risk_report.total_margin_requirement,
            'available_buying_power': risk_report.available_buying_power,
            'margin_utilization_pct': risk_report.margin_utilization_pct,
            'portfolio_delta': risk_report.portfolio_delta,
            'portfolio_theta': risk_report.portfolio_theta,
            'portfolio_vega': risk_report.portfolio_vega,
            'concentration_risk': risk_report.concentration_risk,
            'max_single_position_pct': risk_report.max_single_position_pct,
            'num_correlated_positions': risk_report.num_correlated_positions,
            'warnings': risk_report.warnings,
            'recommendations': risk_report.recommendations
        }

        logger.info(f"API: Risk report generated - VaR: ${risk_report.value_at_risk_1day:,.2f}")

        return jsonify({
            'success': True,
            'risk_report': report_dict,
            'num_positions': len(positions),
            'account_value': account_value
        }), 200

    except Exception as e:
        logger.error(f"Risk report API failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/alerts/current')
def get_current_alerts():
    """
    Get currently active smart alerts for open positions

    Returns alerts from smart_alerts module with priority levels
    """
    try:
        from smart_alerts import run_alert_scan
        from open_trade_monitor import load_trades_from_sheet

        # Load current trades
        trades_result = load_trades_from_sheet()
        if trades_result is None:
            return jsonify({'alerts': [], 'error': 'Could not load trades'})

        df, _ = trades_result
        if df is None or df.empty:
            return jsonify({'alerts': [], 'message': 'No open trades'})

        # Convert to dict format
        trades = df.to_dict('records')

        # Run alert scan
        alerts = run_alert_scan(trades=trades)

        return jsonify({
            'success': True,
            'alerts': alerts,
            'count': len(alerts),
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error getting current alerts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/risk_report')
def get_risk_report_api():
    """
    Get comprehensive risk analysis using RiskCalculator

    Returns:
        - Portfolio heat
        - Position concentration
        - VaR analysis
        - Sector exposure
        - Risk recommendations
    """
    try:
        from core.risk_calculator import RiskCalculator
        from open_trade_monitor import load_trades_from_sheet

        # Load current trades
        trades_result = load_trades_from_sheet()
        if trades_result is None:
            return jsonify({'success': False, 'error': 'Could not load trades'})

        df, _ = trades_result
        if df is None or df.empty:
            return jsonify({
                'success': True,
                'portfolio_heat': 0,
                'var_95': 0,
                'message': 'No open positions'
            })

        trades = df.to_dict('records')

        # Initialize risk calculator
        risk_calc = RiskCalculator(total_capital=25000)  # TODO: Get from config

        # Calculate portfolio heat
        heat_result = risk_calc.calculate_portfolio_heat(trades)

        # Calculate VaR
        var_result = risk_calc.calculate_var(trades, confidence=0.95)

        # Check concentration
        concentration = risk_calc.check_concentration(trades)

        return jsonify({
            'success': True,
            'portfolio_heat': heat_result,
            'var_analysis': var_result,
            'concentration': concentration,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error generating risk report: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/portfolio_health')
def get_portfolio_health_api():
    """
    Get portfolio health metrics from PortfolioAnalyzer

    Returns comprehensive health score and component breakdowns
    """
    try:
        from core.portfolio_analyzer import PortfolioAnalyzer
        from open_trade_monitor import load_trades_from_sheet

        # Load current trades
        trades_result = load_trades_from_sheet()
        if trades_result is None:
            return jsonify({'success': False, 'error': 'Could not load trades'})

        df, _ = trades_result
        if df is None or df.empty:
            return jsonify({
                'success': True,
                'health_score': 100,
                'status': 'No positions',
                'message': 'Portfolio is empty'
            })

        trades = df.to_dict('records')

        # Initialize analyzer
        analyzer = PortfolioAnalyzer(total_capital=25000)

        # Get health score
        health_result = analyzer.get_portfolio_health(trades)

        # Get recommendations
        recommendations = analyzer.get_recommendations(trades)

        return jsonify({
            'success': True,
            'health': health_result,
            'recommendations': recommendations,
            'position_count': len(trades),
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error calculating portfolio health: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trade_scores')
def get_trade_scores_api():
    """
    Get trade scores for all open positions using TradeScorer

    Returns scored positions with component breakdowns
    """
    try:
        from core.trade_scorer import TradeScorer
        from open_trade_monitor import load_trades_from_sheet

        # Load current trades
        trades_result = load_trades_from_sheet()
        if trades_result is None:
            return jsonify({'success': False, 'error': 'Could not load trades'})

        df, _ = trades_result
        if df is None or df.empty:
            return jsonify({
                'success': True,
                'scores': [],
                'message': 'No open positions'
            })

        trades = df.to_dict('records')
        scorer = TradeScorer()

        # Score each position
        scored_positions = []
        for trade in trades:
            try:
                score_result = scorer.score_position(
                    symbol=trade.get('Symbol', ''),
                    strike=safe_float(trade.get('Strike', 0)),
                    current_price=safe_float(trade.get('Underlying Price', 0)),
                    entry_premium=safe_float(trade.get('Entry Premium', 0)),
                    current_premium=safe_float(trade.get('Current Mark', 0)),
                    delta=abs(safe_float(trade.get('Delta', 0))),
                    theta=safe_float(trade.get('Theta', 0)),
                    vega=safe_float(trade.get('Vega', 0)),
                    gamma=safe_float(trade.get('Gamma', 0)),
                    iv=safe_float(trade.get('IV', 0)),
                    dte=safe_int(trade.get('DTE', 0)),
                    days_held=safe_int(trade.get('Days Since Entry', 0)),
                    position_type='short_put'
                )

                scored_positions.append({
                    'symbol': trade.get('Symbol', ''),
                    'score': score_result,
                    'strike': safe_float(trade.get('Strike', 0)),
                    'dte': safe_int(trade.get('DTE', 0))
                })

            except Exception as e:
                logger.error(f"Error scoring {trade.get('Symbol', '')}: {e}")
                continue

        # Sort by score
        scored_positions.sort(key=lambda x: x['score'].get('total_score', 0), reverse=True)

        return jsonify({
            'success': True,
            'scores': scored_positions,
            'count': len(scored_positions),
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error getting trade scores: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grok_compare', methods=['POST'])
def grok_compare_opportunities():
    """
    Compare two trading opportunities using Grok AI.

    POST body:
    {
        "opp1": {opportunity 1 data},
        "opp2": {opportunity 2 data}
    }

    Returns:
        JSON with comparison analysis from Grok
    """
    try:
        data = request.json
        opp1 = data.get('opp1')
        opp2 = data.get('opp2')

        if not opp1 or not opp2:
            return jsonify({'success': False, 'error': 'Missing opportunity data'}), 400

        logger.info(f"API: Comparing {opp1['symbol']} vs {opp2['symbol']}")

        # Build comparison prompt for Grok
        prompt = f"""Compare these two cash-secured put opportunities and explain why one scores better:

**Option A: {opp1['symbol']} ${opp1['strike']}P**
- Grok Score: {opp1['grok_score']}/100
- Delta: {opp1['delta']}
- Distance from strike: {opp1['distance']}%
- DTE: {opp1['dte']} days
- RSI: {opp1['rsi']}
- Premium: ${opp1['premium']}
- Capital: ${opp1['capital']}
- Grok Reasoning: {opp1['grok_reason']}

**Option B: {opp2['symbol']} ${opp2['strike']}P**
- Grok Score: {opp2['grok_score']}/100
- Delta: {opp2['delta']}
- Distance from strike: {opp2['distance']}%
- DTE: {opp2['dte']} days
- RSI: {opp2['rsi']}
- Premium: ${opp2['premium']}
- Capital: ${opp2['capital']}
- Grok Reasoning: {opp2['grok_reason']}

Provide a detailed comparison explaining:
1. Why the scores differ ({abs(opp1['grok_score'] - opp2['grok_score'])} point difference)
2. Which metrics favor each option
3. Which option you'd recommend and why
4. Any hidden risks in the lower-scored option

Be specific about delta, RSI, distance, and capital efficiency. Keep it under 200 words."""

        # Call Grok API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {XAI_API_KEY}"
        }

        payload = {
            "messages": [
                {"role": "system", "content": "You are a quantitative options trading analyst specializing in cash-secured put strategies."},
                {"role": "user", "content": prompt}
            ],
            "model": "grok-4-1-fast-reasoning",
            "stream": False,
            "temperature": 0.7
        }

        response = requests.post(GROK_ENDPOINT, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        grok_data = response.json()
        comparison = grok_data['choices'][0]['message']['content'].strip()

        logger.info(f"Grok comparison completed: {len(comparison)} chars")

        return jsonify({
            'success': True,
            'comparison': comparison
        })

    except requests.exceptions.Timeout:
        logger.error("Grok API timeout during comparison")
        return jsonify({'success': False, 'error': 'Grok API timeout - please try again'}), 504
    except Exception as e:
        logger.error(f"Grok comparison failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/schwab/sell_to_open', methods=['POST'])
def api_sell_to_open():
    """
    Place a Sell To Open order for a cash-secured put.

    Request JSON:
        {
            "symbol": "AAPL",
            "strike": 150.0,
            "expiration": "2026-01-17",  # YYYY-MM-DD format
            "contracts": 1,
            "limit_price": 2.50,         # Price per contract
            "dry_run": false             # Optional, default false
        }

    Returns:
        {
            "success": true/false,
            "order_id": "...",
            "message": "..."
        }
    """
    try:
        data = request.json

        # Validate required fields
        required_fields = ['symbol', 'strike', 'expiration', 'contracts', 'limit_price']
        for field in required_fields:
            if field not in data:
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400

        # Get account ID from environment
        account_id = os.getenv('LIVE_ACCOUNT_ID')
        if not account_id:
            return jsonify({
                'success': False,
                'message': 'LIVE_ACCOUNT_ID not configured'
            }), 500

        # Place order
        result = sell_put_to_open(
            account_id=account_id,
            symbol=data['symbol'],
            strike=float(data['strike']),
            expiration=data['expiration'],
            contracts=int(data['contracts']),
            limit_price=float(data['limit_price']),
            dry_run=data.get('dry_run', False)
        )

        if result['success']:
            logger.info(f"Order placed: {result['message']}")
            return jsonify(result), 200
        else:
            logger.error(f"Order failed: {result['message']}")
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Error in sell_to_open endpoint: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': f'Server error: {str(e)}'
        }), 500


@app.route('/api/schwab/buy_to_close', methods=['POST'])
def api_buy_to_close():
    """
    Place a Buy To Close order to close an existing short put position.

    Request JSON:
        {
            "symbol": "AAPL",
            "strike": 150.0,
            "expiration": "2026-01-17",  # YYYY-MM-DD format
            "contracts": 1,
            "limit_price": 1.25,         # Price per contract
            "dry_run": false             # Optional, default false
        }

    Returns:
        {
            "success": true/false,
            "order_id": "...",
            "message": "..."
        }
    """
    try:
        data = request.json

        # Validate required fields
        required_fields = ['symbol', 'strike', 'expiration', 'contracts', 'limit_price']
        for field in required_fields:
            if field not in data:
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400

        # Get account ID from environment
        account_id = os.getenv('LIVE_ACCOUNT_ID')
        if not account_id:
            return jsonify({
                'success': False,
                'message': 'LIVE_ACCOUNT_ID not configured'
            }), 500

        # Place order
        result = buy_put_to_close(
            account_id=account_id,
            symbol=data['symbol'],
            strike=float(data['strike']),
            expiration=data['expiration'],
            contracts=int(data['contracts']),
            limit_price=float(data['limit_price']),
            dry_run=data.get('dry_run', False)
        )

        if result['success']:
            logger.info(f"Order placed: {result['message']}")
            return jsonify(result), 200
        else:
            logger.error(f"Order failed: {result['message']}")
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Error in buy_to_close endpoint: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': f'Server error: {str(e)}'
        }), 500


if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("DASHBOARD SERVER STARTING")
    logger.info("=" * 60)
    logger.info("Server URL: http://127.0.0.1:5000")
    logger.info("Features: Live quotes, parallel scanners, Telegram alerts")
    logger.info("")
    logger.info("Phase 3 Endpoints:")
    logger.info("  GET  /run/all_scanners    - Run Wheel + LEAPS in parallel")
    logger.info("  GET  /progress/<task_id>  - Check scanner progress")
    logger.info("")
    logger.info("Phase 4 & 5 Endpoints:")
    logger.info("  GET  /api/portfolio_greeks         - Portfolio Greeks aggregation")
    logger.info("  GET  /api/exit_targets             - Dynamic exit targets")
    logger.info("  GET  /api/trade_performance        - Trade journal performance")
    logger.info("  GET  /api/recent_trades            - Recent trades from journal")
    logger.info("  POST /api/position_size            - Position sizing calculator")
    logger.info("  GET  /api/earnings_check/<sym>     - Earnings conflict check")
    logger.info("  GET  /api/smart_alerts/run         - Run smart alerts scan")
    logger.info("  GET  /api/chain_heatmap/<symbol>   - Options chain heatmap")
    logger.info("  GET  /api/chain_data/<symbol>      - Raw option chain data")
    logger.info("  GET  /api/liquidity_analysis/<sym> - Liquidity analysis")
    logger.info("")
    logger.info("Phase 5 Risk Management:")
    logger.info("  GET  /api/risk/var                 - Portfolio Value at Risk (VaR)")
    logger.info("  GET  /api/risk/correlation         - Correlation matrix")
    logger.info("  GET  /api/risk/margin              - Margin requirements & BP")
    logger.info("  GET  /api/risk/report              - Full risk report")
    logger.info("  POST /api/grok_compare             - Compare two opportunities with Grok AI")
    logger.info("")
    logger.info("Improvements:")
    logger.info("  - Parallel scanner execution (2-3x faster)")
    logger.info("  - Circuit breaker for API resilience")
    logger.info("  - SQLite database backend")
    logger.info("  - Portfolio Greeks tracking")
    logger.info("  - Dynamic exit targets")
    logger.info("  - Trade journal with analytics")
    logger.info("  - Bollinger Bands technical analysis")



    # Production-ready configuration
    # For development, use Flask dev server
    # For production, use: gunicorn -w 4 -b 0.0.0.0:5000 dashboard_server:app
    app.run(
        host='127.0.0.1',
        port=5000,
        debug=False,
        use_reloader=False,
        threaded=True  # Enable threading for concurrent requests
    )