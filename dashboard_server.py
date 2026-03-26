# dashboard_server.py — Interactive Dashboard Server (Dec 2025)

from flask import Flask, jsonify, send_from_directory, request, redirect
import uuid
import asyncio
import threading
import os
import json
from datetime import datetime
import time
import pytz
import requests
import yfinance as yf
import dotenv
from jinja2 import Environment, FileSystemLoader
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from config import logger

dotenv.load_dotenv()

# Import your bot mains
from covered_call_bot import main as cc_main
from dividend_tracker_bot import main as div_main
import simple_options_scanner
from grok_utils import (
    get_grok_sentiment_cached as get_grok_sentiment,
    get_grok_analysis,
    get_daily_token_cost,
    _call_grok,
    MODEL_FAST,
    MODEL_MID,
    MODEL_REASONING,
)
from schwab_positions import get_open_positions_as_df as _get_open_positions_compat, get_schwab_csp_positions
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
from trade_outcome_tracker import (
    get_recent_trades as _ot_get_recent_trades,
    get_trade_stats as _ot_get_trade_stats,
    log_trade_exit as _ot_log_trade_exit,
    get_open_trades as _ot_get_open_trades,
    get_db as _ot_get_db,
)
from schwab_utils import sell_put_to_open, buy_put_to_close


from telegram import Bot as telegram_bot

OPEN_TRADE_TELEGRAM_TOKEN = os.getenv('PAPER_TRADE_MONITOR_TELEGRAM_TOKEN')  # Your token
CHAT_ID = os.getenv('PAPER_TRADE_MONITOR_CHAT_ID')  # Your chat ID
open_bot = telegram_bot(token=OPEN_TRADE_TELEGRAM_TOKEN) if OPEN_TRADE_TELEGRAM_TOKEN else None

XAI_API_KEY = os.getenv('XAI_API_KEY')
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
ET_TZ = pytz.timezone('US/Eastern')

LAST_MIRROR_CALL = 0
MIRROR_CACHE = None
MIRROR_CACHE_TIME = 300  # 5 minutes cache

app = Flask(__name__, static_folder='.')
progress_tracker = {}
_refresh_process = None
_refresh_started_at = None
_refresh_log_path = '/tmp/csp-generate.log'

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
    """Return live open CSP positions from Schwab (replaces Google Sheets source)."""
    rows = get_schwab_csp_positions()
    return jsonify(rows)

@app.route('/alert/milestone/<symbol>/<float:progress>')
def milestone_alert(symbol, progress):
    if open_bot:
        msg = f"🎯 {symbol} put hit {progress:.0f}% profit target!\nCheck dashboard for details."
        asyncio.run(open_bot.send_message(chat_id=CHAT_ID, text=msg))  # Use your CHAT_ID
    return jsonify({"status": "Alert sent"})

@app.route('/run/open_trades_refresh')
def run_open_trades_refresh():
    """Positions now sourced live from Schwab — no sheet refresh required."""
    return jsonify({"status": "Positions are sourced live from Schwab on every request. No refresh needed."})

@app.route('/grok/market_pulse')
def grok_market_pulse():
    """Broad market sentiment pulse for the dashboard header tile."""
    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not configured"})
    try:
        # Try cached fast-model sentiment first
        from grok_utils import _call_grok, _read_cache, _write_cache, MODEL_MID
        from pathlib import Path
        cache_file = Path("cache_files") / "grok_market_pulse.json"
        cached = _read_cache(cache_file, ttl_seconds=3600)
        if cached:
            return jsonify({"pulse": cached["pulse"], "sentiment": cached.get("sentiment", "NEUTRAL")})

        system = (
            "You are a professional options market analyst. Be direct and concise. "
            "Use plain text only — no markdown symbols like **, ##, or bullet dashes. "
            "Use numbered lists or plain line breaks."
        )
        user = (
            "Give me a market pulse for today's cash-secured put (CSP) wheel trading. "
            "Cover these 4 points (each on its own line):\n"
            "1. Overall SPY/QQQ direction and VIX level — bullish, neutral, or bearish?\n"
            "2. Best sectors/themes for CSP entry today and why.\n"
            "3. Sectors or names to avoid (earnings risk, news risk, high vol without premium).\n"
            "4. One tactical note — what should a CSP trader do differently today vs a normal day?\n"
            "Keep the whole response under 200 words."
        )
        pulse = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=MODEL_MID,
            max_tokens=350,
        ) or "Market pulse temporarily unavailable."

        from grok_utils import _extract_sentiment
        sentiment = _extract_sentiment(pulse)
        _write_cache(cache_file, {"pulse": pulse, "sentiment": sentiment})

        return jsonify({"pulse": pulse, "sentiment": sentiment})
    except Exception as e:
        logger.error(f"Market pulse error: {e}")
        return jsonify({"error": f"Market pulse unavailable: {e}"}), 500


@app.route('/grok/analyze/<symbol>')
def grok_analyze(symbol):
    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not set"})

    # Model selection: ?reasoning=true for full reasoning, default=mid-tier quality, ?fast=true for cheap
    if request.args.get("reasoning", "false").lower() == "true":
        model = MODEL_REASONING
    elif request.args.get("fast", "false").lower() == "true":
        model = MODEL_FAST
    else:
        model = MODEL_MID  # user-facing prose — quality without reasoning overhead

    # Get current price/IV for context
    try:
        tk = yf.Ticker(symbol)
        info = tk.info
        price = info.get('regularMarketPrice') or info.get('previousClose', 'N/A')
        options = tk.options
        iv = 'N/A'
        if options:
            chain = tk.option_chain(options[0])
            iv = round(chain.calls['impliedVolatility'].mean() * 100, 1) if not chain.calls.empty else 'N/A'
    except Exception:
        price = 'N/A'
        iv = 'N/A'

    current_date = datetime.now().strftime('%B %d, %Y')

    system = (
        "You are an elite options trader specializing in the wheel strategy (CSPs + covered calls) "
        "and LEAPS. Respond in bullet points. Be specific with numbers. Keep under 500 words total."
    )
    prompt = (
        f"Date: {current_date} | {symbol} @ ${price} | IV: {iv}%\n\n"
        "Analyze for wheel strategy suitability:\n"
        "1. **Wheel/LEAPS Suitability** — 2-3 bullets\n"
        "2. **Sentiment & Risks** — recent news, earnings proximity, macro risks (2-3 bullets)\n"
        "3. **Technical Levels** — support, resistance, 50/200 MA, RSI, trend direction\n"
        "4. **CSP Suggestion** — recommended strike, DTE range, target delta, estimated premium, "
        "capital per contract, annualized return %\n"
        "5. **LEAPS Call** (only if bullish case exists) — strike, DTE, cost, "
        "covered call overlay strategy (%OTM, DTE for CCs), dividend yield if any\n"
    )

    content = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        model=model,
        max_tokens=650,
    )
    if content:
        return jsonify({"analysis": content, "model_used": model})
    return jsonify({"error": "Grok API unavailable"})


@app.route('/grok/analyze_option', methods=['POST'])
def grok_analyze_option():
    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not configured"})

    data = request.get_json()

    # Model selection: reasoning=true for full reasoning, fast=true for cheap, default=mid-tier
    if data.get("reasoning", False) or request.args.get("reasoning", "false").lower() == "true":
        model = MODEL_REASONING
    elif data.get("fast", False) or request.args.get("fast", "false").lower() == "true":
        model = MODEL_FAST
    else:
        model = MODEL_MID

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
    except Exception:
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

    system = "You are an expert options trader. Be direct, data-driven, and actionable. Use bullet points."

    prompt = (
        f"Date: {current_date} | {symbol} @ ${current_price}\n"
        f"Trade: {direction} {opt_type} | Strike ${strike} | Premium ${premium} | DTE {dte}\n"
        f"{extra_context}"
    )

    if delta is not None: prompt += f"Delta: {delta:.3f}\n"
    if theta is not None: prompt += f"Theta: {theta:.3f}\n"
    if vega is not None: prompt += f"Vega: {vega:.3f}\n"
    if iv is not None: prompt += f"IV: {iv:.1f}%\n"

    if strategy == 'CSP':
        prompt += (
            "\nCSP (wheel strategy). Focus on: probability of profit, "
            "downside breakeven/protection, annualized return, assignment risk, vs buying stock outright.\n"
        )
    elif strategy == 'LEAPS':
        prompt += (
            "\nLEAPS long call (stock replacement). Focus on: leverage vs shares, "
            "delta exposure, time decay risk, breakeven, max loss.\n"
        )
    elif strategy == 'CC':
        prompt += (
            "\nCovered Call. Focus on: income vs upside cap, "
            "probability of being called away, if-called vs if-not-called scenarios.\n"
        )
    else:
        prompt += f"\nGeneral analysis of {direction.lower()}ing a {opt_type.lower()} option.\n"

    prompt += (
        "\nProvide concise bullet-point analysis:\n"
        "- Trade summary\n- Estimated probability of profit\n- Breakeven\n"
        "- Expected return / annualized\n- Key risks\n- Alternatives\n"
        "- Final verdict: Strong Yes / Yes / Neutral / Caution / No\n"
        "Keep under 350 words."
    )

    content = _call_grok(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        model=model,
        max_tokens=450,
    )
    if content:
        return jsonify({"analysis": content.strip(), "model_used": model})
    return jsonify({"error": "Grok API error"})

@app.route('/grok/trade_analysis', methods=['POST'])
def grok_trade_analysis():
    try:
        # Load closed trades from SQLite trade outcome tracker
        records = _ot_get_recent_trades(limit=200)
        closed_trades = [r for r in records if r.get('exit_date') and r.get('outcome') != 'open']

        if not closed_trades:
            return {"analysis": "No closed trades found for analysis."}

        # Build rich trade data
        trades_text = ""
        for i, t in enumerate(closed_trades, 1):
            symbol = t.get('symbol', 'N/A')
            strike = safe_float(t.get('strike', 0))
            exp_str = t.get('expiration', 'N/A')
            entry_prem = safe_float(t.get('entry_premium', 0))
            exit_prem = safe_float(t.get('exit_premium', 0)) or 0
            pl = safe_float(t.get('pnl', 0))
            days_held = safe_int(t.get('holding_days', 0))

            trades_text += (
                f"#{i}: {symbol} ${strike:.2f}P exp {exp_str} | "
                f"Entry ${entry_prem:.2f} → Exit ${exit_prem:.2f} | "
                f"{days_held}d | P/L ${pl:+,.0f} {'WIN' if pl > 0 else 'LOSS' if pl < 0 else 'BE'}\n"
            )

        system = (
            "You are an elite wheel strategy analyst. "
            "Analyze closed CSP trades and identify patterns. "
            "Use Markdown tables with | separators. Be data-driven."
        )

        prompt = (
            f"My last {len(closed_trades)} closed CSP trades:\n\n"
            f"{trades_text}\n"
            "Analyze and report:\n"
            "1. **Overall Performance** — win rate, total P/L, avg P/L, best/worst\n"
            "2. **Patterns** — by DTE range (0-10/11-21/22-45/45+), IV (high/mid/low), RSI, sector\n"
            "3. **Best Setups** — highest win rate combos, highest ROI patterns\n"
            "4. **Recommendations** — my winning DNA, setups to target, risks to avoid\n\n"
            "Use proper Markdown tables. Keep under 500 words."
        )

        analysis = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=MODEL_MID,
            max_tokens=700,
        )

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
    try:
        trades_result = _get_open_positions_compat()
        if trades_result is None:
            return jsonify([])

        df, _ = trades_result
        if df is None or df.empty:
            return jsonify([])

        system = (
            "You are a wheel strategy specialist. Suggest optimal CSP roll parameters. "
            "Be specific with numbers. Keep each suggestion under 100 words."
        )

        suggestions = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            strike = safe_float(row.get('Strike', 0))
            exp_date = row.get('Exp Date', '')
            entry_premium = safe_float(row.get('Entry Premium', 0))
            dte = safe_int(row.get('DTE', 0))

            # Try to get current underlying price from Schwab quote
            try:
                from schwab_utils import get_client as _sc
                _cl = _sc()
                _cl.set_enforce_enums(False)
                _qr = _cl.get_quotes([symbol])
                if _qr.status_code == 200:
                    _qd = _qr.json().get(symbol, {}).get('quote', {})
                    underlying = float(_qd.get('lastPrice', 0) or 0)
                else:
                    underlying = 0
            except Exception:
                underlying = 0

            prompt = (
                f"{symbol} @ ${underlying:.2f} | Short ${strike:.2f}P exp {exp_date} | "
                f"DTE {dte} | Entry credit ${entry_premium:.2f}\n\n"
                "Should I roll this position? If yes:\n"
                "- New strike & expiration (DTE)\n"
                "- Expected net credit/debit\n"
                "- Reasoning (delta improvement, more time, better premium capture)\n"
                "If no: why hold or close instead."
            )

            suggestion = _call_grok(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                model=MODEL_MID,
                max_tokens=200,
            )
            suggestions.append({
                "symbol": symbol,
                "strike": strike,
                "exp_date": exp_date,
                "suggestion": suggestion or "Analysis unavailable"
            })

        return jsonify(suggestions)
    except Exception as e:
        return {"result": f"Rollover Analysis Failed: {str(e)}"}

@app.post("/csp/mark_closed")
def mark_trade_closed():
    """Log a trade exit in SQLite. The position disappears from Schwab live data automatically."""
    try:
        data = request.json
        symbol = data.get('symbol', '').strip().upper()
        strike = safe_float(data.get('strike', 0))
        exit_premium = safe_float(data.get('exit_premium', 0))
        outcome = data.get('outcome', 'closed_profit')

        if not symbol or not strike:
            return {"status": "Error: Missing symbol or strike"}, 400

        # Find matching open trade in SQLite
        open_trades = _ot_get_open_trades()
        trade_id = None
        for t in open_trades:
            if (t.get('symbol', '').upper() == symbol and
                    abs(safe_float(t.get('strike', 0)) - strike) < 0.01):
                trade_id = t['id']
                break

        if trade_id:
            _ot_log_trade_exit(trade_id, exit_premium=exit_premium, outcome=outcome)
            return {"status": f"{symbol} ${strike} marked as closed in trade journal."}

        return {"status": f"{symbol} not found in open trades — may already be closed or not yet logged."}, 404

    except Exception as e:
        logger.error(f"Mark closed failed: {e}")
        return {"status": "Server error — check logs"}, 500

@app.get("/grok/dna")
def get_trading_dna():
    try:
        # Load closed trades from SQLite trade outcome tracker
        records = _ot_get_recent_trades(limit=100)
        closed_trades = [r for r in records if r.get('exit_date') and r.get('outcome') != 'open']

        if len(closed_trades) < 5:
            return {"dna": "🔍 <strong>Not enough closed trades for DNA analysis</strong><br><br>"
                           "Need at least 5 completed trades to identify patterns.<br>"
                           "Keep building your history!"}

        # Build concise trade list (limit to avoid token overflow)
        trades_summary = ""
        for i, t in enumerate(closed_trades[:20], 1):
            symbol = t.get('symbol', 'N/A')
            strike = safe_float(t.get('strike', 0))
            pl = safe_float(t.get('pnl', 0))
            days = safe_int(t.get('holding_days', 0))
            result = "WIN" if pl > 0 else "LOSS" if pl < 0 else "BE"
            trades_summary += f"{i}. {symbol} ${strike:.0f}P | {days}d | ${pl:+,.0f} | {result}\n"

        system = (
            "You are an elite wheel trading coach. "
            "Identify personal trading patterns and winning DNA from trade history. "
            "Be specific and actionable. Use bullets."
        )

        prompt = (
            f"My last {len(closed_trades)} closed CSP trades:\n\n"
            f"{trades_summary}\n"
            "Identify my winning patterns:\n"
            "- Highest win rate DTE ranges\n"
            "- Best delta/IV/RSI conditions\n"
            "- Top performing sectors/tickers\n"
            "- Most profitable setup combinations\n"
            "- Specific recommendations for future trades\n\n"
            "Keep under 300 words."
        )

        analysis = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=MODEL_MID,
            max_tokens=400,
        )

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
        logger.error(f"DNA analysis error: {e}")
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
        system = "You are a wheel strategy position manager. Be concise and actionable."
        grok_prompt = (
            f"Open short {symbol} ${strike:.2f}P exp {exp}\n"
            f"Entry ${entry_premium:.2f} → Mark ${mark:.2f} ({progress_pct:.1f}% captured) | "
            f"DTE {dte} | {days_open}d open\n"
            f"Underlying ${underlying:.2f} | Delta {delta:.2f} | IV {iv:.1f}% | P/L ${pl_dollars:+,.0f}\n\n"
            "Action advice: close early, hold, or roll (out/up/down)? New risks? Under 80 words."
        )

        analysis = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": grok_prompt}],
            model=MODEL_FAST,
            max_tokens=150,
        )
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
        from simple_options_scanner import SIMPLE_WATCHLIST

        system = (
            "You are a market strategist for wheel strategy (CSP + CC) traders. "
            "Be concise and actionable. Use the headers provided."
        )

        prompt = (
            f"Date: {current_date}\n"
            f"Watchlist: {', '.join(sorted(set(SIMPLE_WATCHLIST)))}\n\n"
            "Market pulse for CSP/wheel traders:\n"
            "**Overall Sentiment** — bullish/bearish/neutral, VIX context\n"
            "**Strongest Sectors** — 2-3 sectors with momentum\n"
            "**Best Wheel Picks** — top 5 from watchlist (strong sectors, good IV)\n"
            "**Quick Advice** — actionable entry guidance\n\n"
            "Keep under 300 words."
        )

        analysis = _call_grok(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=MODEL_MID,
            max_tokens=400,
        )

        if not analysis:
            analysis = "Market pulse unavailable — try again soon."

        return {"pulse": analysis}

    except Exception as e:
        return {"pulse": "Pulse failed — check server"}
    
@app.post("/refresh_dashboard")
def refresh_dashboard():
    """Spawn generate_dashboard.py as a subprocess so it gets a fresh Python
    interpreter — avoids any module-level import state issues in the server."""
    import subprocess, sys
    global _refresh_process, _refresh_started_at
    try:
        if _refresh_process is not None and _refresh_process.poll() is None:
            return {
                "status": "Dashboard refresh already running.",
                "state": "running",
            }

        python = sys.executable
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_dashboard.py')
        log_handle = open(_refresh_log_path, 'a')
        _refresh_process = subprocess.Popen(
            [python, script],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        _refresh_started_at = time.time()
        logger.info("Dashboard regeneration started as subprocess — see /tmp/csp-generate.log")
        return {
            "status": "Dashboard refresh started. This can take 1-3 minutes.",
            "state": "started",
        }
    except Exception as e:
        logger.error(f"refresh_dashboard failed: {e}")
        return {"status": f"Refresh failed: {str(e)}"}, 500


@app.get("/api/refresh_status")
def refresh_dashboard_status():
    global _refresh_process, _refresh_started_at

    if _refresh_process is None:
        return jsonify({
            'state': 'idle',
            'message': 'No dashboard refresh has been started.'
        })

    return_code = _refresh_process.poll()
    elapsed = round(time.time() - (_refresh_started_at or time.time()), 1)

    # Extract the most recent STEP: line from the log for granular progress
    current_step = None
    try:
        with open(_refresh_log_path, 'rb') as lf:
            lf.seek(0, 2)
            size = lf.tell()
            lf.seek(max(0, size - 8192))
            tail = lf.read().decode('utf-8', errors='replace')
        for line in reversed(tail.splitlines()):
            if line.startswith('STEP:'):
                current_step = line[len('STEP:'):].strip()
                break
    except Exception:
        pass

    if return_code is None:
        return jsonify({
            'state': 'running',
            'elapsed_seconds': elapsed,
            'current_step': current_step,
            'message': f'Dashboard refresh running for {elapsed:.1f}s.'
        })

    state = 'completed' if return_code == 0 else 'failed'
    message = 'Dashboard refresh completed. Reloading page.' if return_code == 0 else f'Dashboard refresh failed with exit code {return_code}. Check /tmp/csp-generate.log.'

    _refresh_process = None
    _refresh_started_at = None
    return jsonify({
        'state': state,
        'exit_code': return_code,
        'message': message,
    })

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
        # Load current trades from Schwab live positions
        trades_result = _get_open_positions_compat()

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
        from trade_outcome_tracker import get_open_trades
        client = get_client()

        # Build a lookup of SQLite Greeks keyed by (symbol, strike, expiration_iso)
        _sqlite_trades = get_open_trades()
        _greeks_lookup = {}
        for _t in _sqlite_trades:
            _exp_iso = _t['expiration']  # already YYYY-MM-DD in SQLite
            _greeks_lookup[(_t['symbol'].upper(), float(_t['strike']), _exp_iso)] = _t

        positions = []
        for _, row in df.iterrows():
            try:
                symbol = str(row.get('Symbol', ''))
                strike = safe_float(row.get('Strike', 0))
                exp_date = str(row.get('Exp Date', ''))
                quantity = int(row.get('Quantity', 0))

                # Normalise expiration to YYYY-MM-DD for lookup
                if '/' in exp_date:
                    exp_iso = datetime.strptime(exp_date, '%m/%d/%Y').strftime('%Y-%m-%d')
                else:
                    exp_iso = exp_date

                # STRATEGY: SQLite (periodically updated by update_greeks_from_schwab.py)
                # is the primary source.  Fall back to live Schwab API quote if zero.
                sqlite_trade = _greeks_lookup.get((symbol.upper(), strike, exp_iso))
                if sqlite_trade:
                    delta = safe_float(sqlite_trade.get('delta', 0))
                    gamma = safe_float(sqlite_trade.get('gamma', 0))
                    theta = safe_float(sqlite_trade.get('theta', 0))
                    vega  = safe_float(sqlite_trade.get('vega', 0))
                    logger.debug(f"SQLite Greeks for {symbol}: delta={delta:.3f}, theta={theta:.3f}, "
                                 f"gamma={gamma:.3f}, vega={vega:.3f}")
                else:
                    delta = gamma = theta = vega = 0.0
                    logger.debug(f"No SQLite trade found for {symbol} ${strike}P {exp_iso}")

                # Try to enhance with live API Greeks if SQLite values are zero or missing
                if delta == 0 and theta == 0:
                    logger.info(f"SQLite Greeks are zero for {symbol}, attempting live API fetch...")
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
                    logger.debug(f"Using SQLite Greeks for {symbol} (non-zero values)")

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
        trades_result = _get_open_positions_compat()

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

                # Use Current Price from live Schwab position data
                current_price = safe_float(row.get('Current Price', 0))
                current_iv = safe_float(row.get('IV', 0.30))

                # Get current regime for regime-linked stop-loss scaling
                grok_sentiment_val, _ = get_grok_sentiment()
                # regime_key comes from cached sentiment string (same keys as REGIME_SETTINGS)
                _valid_regimes = {'STRONG_BULL', 'MILD_BULL', 'NEUTRAL_OR_WEAK', 'CAUTIOUS', 'BEARISH_HIGH_VOL'}
                regime_key_val = grok_sentiment_val if grok_sentiment_val in _valid_regimes else 'MILD_BULL'

                # Calculate exit targets — pass positional args correctly
                target = calculate_exit_targets(
                    current_price=current_price,
                    entry_price=entry_premium,
                    position_type="short_put",
                    strike=strike,
                    premium_collected=entry_premium,
                    iv=current_iv if current_iv > 0 else 0.30,
                    regime_key=regime_key_val,
                )

                # Compute simple current P&L pct relative to entry premium
                current_pnl_pct = 0.0
                if entry_premium > 0 and current_premium > 0:
                    current_pnl_pct = round((entry_premium - current_premium) / entry_premium * 100, 1)

                exit_data.append({
                    'symbol': symbol,
                    'strike': strike,
                    'recommendation': target.get('recommendation', ''),
                    'profit_target': target.get('profit_target', 0),
                    'profit_target_pct': target.get('profit_target_pct', 50),
                    'early_exit_target': target.get('early_exit_target', 0),
                    'stop_loss_price': target.get('stop_loss_price', 0),
                    'emergency_exit_price': target.get('emergency_exit_price', 0),
                    'adjustment_trigger_delta': target.get('adjustment_trigger_delta', 0.45),
                    'adjustment_trigger_dte': target.get('adjustment_trigger_dte', 7),
                    'current_pnl_pct': current_pnl_pct,
                    'regime_key': regime_key_val,
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
    Get trade performance summary from SQLite trade outcome tracker.

    Returns win rate, profit factor, expectancy, and trade statistics.
    """
    try:
        with _ot_get_db() as conn:
            rows = conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl) as total_pnl,
                    AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                    ABS(AVG(CASE WHEN pnl <= 0 THEN pnl END)) as avg_loss,
                    AVG(holding_days) as avg_holding_days
                FROM trades
                WHERE outcome != 'open' AND exit_date IS NOT NULL
            """).fetchone()

        total_trades = rows['total_trades'] or 0
        wins = rows['wins'] or 0
        losses = rows['losses'] or 0
        total_pnl = float(rows['total_pnl'] or 0)
        avg_win = float(rows['avg_win'] or 0)
        avg_loss = float(rows['avg_loss'] or 0)
        avg_days_held = float(rows['avg_holding_days'] or 0)

        if not total_trades:
            return jsonify({'total_trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
                            'total_pnl': 0.0, 'expectancy': 0.0})

        win_rate = wins / total_trades * 100
        loss_rate = 1 - win_rate / 100

        if avg_loss > 0:
            profit_factor = min(avg_win * wins / (avg_loss * losses), 999.99) if losses > 0 else 999.99
        else:
            profit_factor = 999.99 if avg_win > 0 else 0.0

        expectancy = (avg_win * win_rate / 100) - (avg_loss * loss_rate)

        logger.info(f"Performance from {total_trades} trades: {wins}W/{losses}L")

        return jsonify({
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': round(win_rate, 1),
            'profit_factor': round(profit_factor, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'expectancy': round(expectancy, 2),
            'avg_dte': 0,
            'avg_days_held': round(avg_days_held, 1)
        })

    except Exception as e:
        logger.error(f"Trade performance failed: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/recent_trades')
def get_api_recent_trades():
    """
    Get recent trades from SQLite + live Schwab positions.

    Query params:
    - limit: Number of trades to return (default 10)
    - status: Filter by status ('open', 'closed', 'all')
    """
    try:
        limit = int(request.args.get('limit', 10))
        status_filter = request.args.get('status', 'all').lower()

        trades = []

        # Load CLOSED trades from SQLite trade outcome tracker
        if status_filter in ['closed', 'all']:
            try:
                records = _ot_get_recent_trades(limit=limit * 2)
                for row in records:
                    if row.get('outcome') == 'open':
                        continue
                    trades.append({
                        'symbol': row.get('symbol', ''),
                        'strike': row.get('strike', 0),
                        'status': 'closed',
                        'entry_date': row.get('entry_date', ''),
                        'exit_date': row.get('exit_date', ''),
                        'entry_premium': row.get('entry_premium', 0),
                        'exit_premium': row.get('exit_premium', 0),
                        'quantity': row.get('contracts', 1),
                        'pnl': row.get('pnl', 0),
                        'roc': row.get('pnl_pct', 0),
                        'days_held': row.get('holding_days', 0),
                    })
                logger.info(f"Loaded {len(trades)} closed trades from SQLite")
            except Exception as e:
                logger.error(f"Failed to load closed trades from SQLite: {e}")

        # Load OPEN trades from Schwab live positions
        if status_filter in ['open', 'all']:
            try:
                for row in get_schwab_csp_positions():
                    quantity = safe_int(row.get('Quantity', 1))
                    entry_premium = safe_float(row.get('Entry Premium', 0))
                    current_premium = safe_float(row.get('Current Premium', 0))
                    trades.append({
                        'symbol': row.get('Symbol', ''),
                        'strike': row.get('Strike', 0),
                        'status': 'open',
                        'entry_date': '',
                        'entry_premium': entry_premium,
                        'current_premium': current_premium,
                        'quantity': quantity,
                        'pnl': round((entry_premium - current_premium) * quantity * 100, 2),
                    })
            except Exception as e:
                logger.error(f"Failed to load open positions from Schwab: {e}")

        # Sort by entry date, most recent first
        trades.sort(key=lambda x: x.get('entry_date', '') or x.get('exit_date', ''), reverse=True)

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
        "grok_score": 85,
        "win_rate": 65.0,
        "existing_sectors": ["Technology", "Technology"]
    }
    """
    try:
        data = request.get_json() or {}

        account_value = safe_float(data.get('account_value', 50000))
        strike_price = safe_float(data.get('strike_price', 100))
        grok_score = safe_float(data.get('grok_score', 70))
        win_rate = safe_float(data.get('win_rate', 65)) / 100  # normalise to 0-1
        # existing_sectors: list of sector strings for open positions
        existing_sectors: list = data.get('existing_sectors', [])
        new_sector: str = data.get('sector', '')
        strategy: str = data.get('strategy', 'moderate')

        sizing = calculate_position_size(
            account_balance=account_value,           # correct param name
            underlying_price=strike_price,           # correct param name
            prob_profit=grok_score,                  # maps Grok score → probability kwarg
            win_rate=win_rate,                       # pass via kwargs
            strategy=strategy,
        )

        recommended_contracts = sizing.get('recommended_contracts', 1)

        # Correlation-adjusted sizing: cap at 50% if ≥ 2 open positions in same sector
        sector_concentration_warning = None
        if new_sector and existing_sectors:
            same_sector_count = sum(1 for s in existing_sectors if s == new_sector)
            if same_sector_count >= 2:
                recommended_contracts = max(1, int(recommended_contracts * 0.5))
                sector_concentration_warning = (
                    f"Sector concentration limit: {same_sector_count} existing positions in "
                    f"'{new_sector}' — contracts halved to {recommended_contracts}"
                )

        # Capital required = contracts × strike × 100 (CSP margin requirement)
        capital_required = round(recommended_contracts * strike_price * 100, 2) if strike_price > 0 else 0

        return jsonify({
            'recommended_contracts': recommended_contracts,
            'capital_required': capital_required,
            'position_value': sizing.get('position_value', 0),
            'risk_amount': sizing.get('risk_amount', 0),
            'risk_percent': sizing.get('risk_percent', 0),
            'kelly_fraction': sizing.get('kelly_fraction', 0),
            'sizing_method': sizing.get('sizing_method', 'Fixed Risk'),
            'sector_concentration_warning': sector_concentration_warning,
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

                # Load current positions from Schwab
                positions = get_schwab_csp_positions()

                if not positions:
                    return {'status': 'success', 'message': 'No trades to scan'}

                # Convert to format expected by smart_alerts
                trades = [{
                    'symbol': row.get('Symbol', ''),
                    'strike': safe_float(row.get('Strike', 0)),
                    'entry_premium': safe_float(row.get('Entry Premium', 0)),
                    'current_premium': safe_float(row.get('Current Premium', 0)),
                    'underlying_price': safe_float(row.get('Current Price', 0)),
                    'expiration_date': str(row.get('Exp Date', '')),
                    'current_iv': safe_float(row.get('IV', 0.30))
                } for row in positions]

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


@app.route('/api/opportunity/live', methods=['GET'])
def get_live_opportunity_data():
    """
    Get live data for a specific opportunity.

    Query params:
        symbol: Underlying symbol (e.g., 'AAPL')
        strike: Strike price (e.g., 175.0)
        expiration: Expiration date in YYYY-MM-DD format

    Returns:
        JSON with live metrics:
        {
            'current_price': float,
            'premium': float (current bid),
            'bid': float,
            'ask': float,
            'spread_pct': float,
            'delta': float,
            'gamma': float,
            'theta': float,
            'vega': float,
            'iv': float,
            'open_interest': int,
            'volume': int,
            'distance_pct': float (strike vs current price),
            'last_update': timestamp,
            'error': str (if any)
        }
    """
    try:
        symbol = request.args.get('symbol', '').upper()
        strike = float(request.args.get('strike'))
        expiration = request.args.get('expiration')  # YYYY-MM-DD format

        if not symbol or not strike or not expiration:
            return jsonify({'error': 'Missing required parameters'}), 400

        from schwab_utils import get_schwab_client

        # Get current stock price
        ticker = yf.Ticker(symbol)
        current_price = ticker.info.get('currentPrice') or ticker.info.get('regularMarketPrice', 0)

        if not current_price:
            hist = ticker.history(period="1d")
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]

        # Get option chain
        client = get_schwab_client()
        chain_resp = client.get_option_chain(
            symbol,
            contract_type="PUT",
            strike_count=50,
            include_underlying_quote=True,
            from_date=expiration,
            to_date=expiration
        )

        if not chain_resp or 'putExpDateMap' not in chain_resp:
            return jsonify({'error': 'No option data available'}), 404

        # Find the matching strike in the chain
        option_data = None
        for exp_date, strikes in chain_resp['putExpDateMap'].items():
            strike_key = f"{strike:.1f}"
            if strike_key in strikes:
                contracts = strikes[strike_key]
                if contracts:
                    option_data = contracts[0]
                    break

        if not option_data:
            return jsonify({'error': f'No data for strike {strike}'}), 404

        # Extract live metrics
        bid = option_data.get('bid', 0)
        ask = option_data.get('ask', 0)
        premium = bid  # Premium is what you receive (bid price)
        spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100) if (ask + bid) > 0 else 0

        distance_pct = ((strike - current_price) / current_price * 100) if current_price > 0 else 0

        result = {
            'symbol': symbol,
            'strike': strike,
            'expiration': expiration,
            'current_price': round(current_price, 2),
            'premium': round(premium, 2),
            'bid': round(bid, 2),
            'ask': round(ask, 2),
            'spread_pct': round(spread_pct, 2),
            'delta': round(option_data.get('delta', 0), 4),
            'gamma': round(option_data.get('gamma', 0), 4),
            'theta': round(option_data.get('theta', 0), 4),
            'vega': round(option_data.get('vega', 0), 4),
            'iv': round(option_data.get('volatility', 0) * 100, 2),  # Convert to percentage
            'open_interest': int(option_data.get('openInterest', 0)),
            'volume': int(option_data.get('totalVolume', 0)),
            'distance_pct': round(distance_pct, 2),
            'last_update': datetime.now().isoformat(),
            'success': True
        }

        return jsonify(result)

    except Exception as e:
        logger.error(f"Live opportunity data API failed for {symbol} ${strike}: {e}")
        return jsonify({
            'error': str(e),
            'success': False,
            'last_update': datetime.now().isoformat()
        }), 500


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



# _parse_schwab_occ_symbol and _get_schwab_csp_positions moved to schwab_positions.py
# Both are imported at the top of this file from schwab_positions.
# Alias allows internal references to keep working without touching all call sites.
_get_schwab_csp_positions = get_schwab_csp_positions


@app.route('/api/open_csps')
def get_open_csps():
    """
    Get updated open CSPs data with LIVE market data for dashboard refresh.
    Primary source: Schwab account positions API.
    Fallback: Google Sheet + live stock quotes.

    Query Parameters:
        force_refresh: If 'true', bypass cache and fetch fresh data

    Returns:
        JSON with enriched CSP positions and summary statistics
    """
    try:
        logger.info("Open CSPs endpoint called")
        from datetime import datetime

        force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
        logger.info(f"Force refresh: {force_refresh}")

        # Simple in-memory cache (5 minute TTL for normal requests)
        cache_key = 'open_csps_data'
        cache_ttl = 300  # 5 minutes

        # Check cache unless force refresh
        if not force_refresh and hasattr(get_open_csps, '_cache'):
            cached = get_open_csps._cache.get(cache_key)
            if cached and (datetime.now() - cached['timestamp']).total_seconds() < cache_ttl:
                logger.info("Returning cached Open CSPs data")
                return jsonify(cached['data'])

        logger.info(f"Fetching fresh Open CSPs data (force_refresh={force_refresh})")

        # Fetch from Schwab live positions (sole source of truth)
        enriched_rows = _get_schwab_csp_positions()
        data_source = 'schwab'

        if not enriched_rows:
            logger.warning("No trades found from Schwab or Sheet")
            return jsonify({'csps': [], 'summary': {}, 'last_updated': datetime.now().isoformat(),
                            'data_source': 'none'})

        # Calculate summary statistics
        total_credit = sum(float(r.get('_total_credit', 0) or 0) for r in enriched_rows)
        total_pl = sum(float(r.get('_pl_dollars', 0) or 0) for r in enriched_rows)
        avg_progress = sum(float(r.get('_progress_pct', 0) or 0) for r in enriched_rows) / len(enriched_rows) if enriched_rows else 0
        avg_dte = sum(float(r.get('_dte', 0) or 0) for r in enriched_rows) / len(enriched_rows) if enriched_rows else 0
        total_realized_theta = sum(float(r.get('_daily_theta_decay_dollars', 0) or 0) for r in enriched_rows)
        avg_theta_per_pos = total_realized_theta / len(enriched_rows) if enriched_rows else 0
        total_expected_decay = sum(float(r.get('_forward_theta_daily', 0) or 0) for r in enriched_rows)
        projected_remaining = sum(float(r.get('_projected_decay', 0) or 0) for r in enriched_rows)

        summary = {
            'positions_count': len(enriched_rows),
            'total_credit': round(total_credit, 2),
            'total_pl': round(total_pl, 2),
            'avg_progress': round(avg_progress, 1),
            'avg_dte': round(avg_dte, 1),
            'total_realized_theta': round(total_realized_theta, 2),
            'avg_theta_per_pos': round(avg_theta_per_pos, 2),
            'total_expected_decay': round(total_expected_decay, 2),
            'projected_remaining': round(projected_remaining, 2)
        }

        last_updated = datetime.now().isoformat()

        response_data = {
            'csps': enriched_rows,
            'summary': summary,
            'last_updated': last_updated,
            'data_source': data_source,
        }

        # Cache the response
        if not hasattr(get_open_csps, '_cache'):
            get_open_csps._cache = {}
        get_open_csps._cache[cache_key] = {
            'data': response_data,
            'timestamp': datetime.now()
        }

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Failed to get open CSPs: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ==================== ANALYTICS API ====================

@app.route('/api/token_cost')
def get_token_cost():
    """Return today's estimated Grok API token cost."""
    try:
        cost = get_daily_token_cost()
        return jsonify({'ok': True, 'data': cost})
    except Exception as e:
        logger.error("token_cost error: %s", e)
        return jsonify({'ok': False, 'error': {'code': 'INTERNAL_ERROR', 'message': str(e)}}), 500


@app.route('/api/regime_history')
def get_regime_history_route():
    """Return recent regime change log entries."""
    try:
        from trade_outcome_tracker import get_regime_history
        limit = int(request.args.get('limit', 60))
        history = get_regime_history(limit=limit)
        return jsonify({'ok': True, 'data': history})
    except Exception as e:
        logger.error("regime_history error: %s", e)
        return jsonify({'ok': False, 'error': {'code': 'INTERNAL_ERROR', 'message': str(e)}}), 500


@app.route('/api/sector_scores')
def get_sector_scores_route():
    """Return scored sentiment for all 11 GICS sectors (4-hour cached)."""
    try:
        from sector_sentiment import get_sector_scores
        force = request.args.get('force', 'false').lower() == 'true'
        scores = get_sector_scores(force_refresh=force)
        # Defensive: ensure scores is a dict of dicts with 'score' keys
        if not isinstance(scores, dict) or not all(isinstance(v, dict) and 'score' in v for v in scores.values()):
            logger.error("sector_scores: invalid scores structure: %r", scores)
            return jsonify({'ok': False, 'error': {'code': 'INTERNAL_ERROR', 'message': 'Invalid sector scores structure'}}), 500
        return jsonify({'ok': True, 'data': scores})
    except Exception as e:
        logger.error("sector_scores error: %s", e, exc_info=True)
        # Always return JSON, never HTML
        return jsonify({'ok': False, 'error': {'code': 'INTERNAL_ERROR', 'message': str(e)}}), 200


@app.route('/api/analytics/summary')
def get_analytics_summary():
    """
    Aggregate trade performance stats from SQLite trade outcome tracker.
    Uses last 90 days of closed trades by default.
    """
    try:
        from trade_outcome_tracker import get_trade_stats
        days = int(request.args.get('days', 90))
        stats = get_trade_stats(days=days)

        # Augment with live Schwab position count
        live_positions = _get_schwab_csp_positions()
        if live_positions:
            stats['open_count'] = len(live_positions)

        return jsonify({'ok': True, 'data': stats})
    except Exception as e:
        logger.error(f"Analytics summary failed: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/analytics/history')
def get_analytics_history():
    """
    Recent closed trades from SQLite trade outcome tracker.
    Returns records formatted for the Analytics Trade History table.
    """
    try:
        from trade_outcome_tracker import get_recent_trades
        limit = int(request.args.get('limit', 50))
        trades = get_recent_trades(limit=limit)

        # Normalise fields to match the expected Analytics table shape
        formatted = []
        for t in trades:
            if t.get('outcome') == 'open':
                continue  # open trades shown separately
            pnl = float(t.get('pnl') or 0)
            outcome = t.get('outcome', '')
            win_loss = (
                'WIN' if outcome in ('expired_worthless', 'closed_profit') else
                'LOSS' if outcome in ('closed_loss', 'assigned') else
                outcome.upper()
            )
            formatted.append({
                'Symbol': t.get('symbol', ''),
                'Strike': t.get('strike', 0),
                'Entry Date': t.get('entry_date', ''),
                'Exit Date': t.get('exit_date', ''),
                'Days Held': t.get('holding_days', 0),
                'Entry Premium': t.get('entry_premium', 0),
                'Exit Premium': t.get('exit_premium', 0),
                'Net Profit $': pnl,
                'ROI %': t.get('pnl_pct', 0),
                'Win/Loss': win_loss,
                'outcome': outcome,
                'regime': t.get('regime', ''),
                'grok_score': t.get('grok_trade_score', 0),
            })

        return jsonify({'ok': True, 'data': formatted, 'count': len(formatted)})
    except Exception as e:
        logger.error(f"Analytics history failed: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/analytics/open')
def get_analytics_open():
    """
    Live open short PUT positions from Schwab for the Analytics Open Trades table.
    Mirrors /api/open_csps but without the summary block, for lightweight use in analytics.
    """
    try:
        positions = _get_schwab_csp_positions()
        return jsonify({'ok': True, 'data': positions, 'count': len(positions)})
    except Exception as e:
        logger.error(f"Analytics open positions failed: {e}", exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


# ==================== PHASE 4: OPTIONS CHAIN HEATMAP API ====================

@app.route('/api/chain_heatmap/<symbol>')
def get_chain_heatmap(symbol):
    """
    Generate options chain heatmap for a symbol.

    Query Parameters:
        contract_type: 'PUT' or 'CALL' (default: 'PUT')
        viz_type: 'open_interest', 'volume', 'liquidity', 'iv_surface',
                  'delta', 'gamma', 'theta', 'vega', 'dashboard' (default: 'open_interest')
        days_out: Days from now to fetch (default: 60)

    Returns:
        JSON with heatmap plot data and analysis metrics
    """
    try:
        from chain_visualizer import fetch_option_chain_data, generate_chain_heatmap
        import json

        symbol = symbol.upper()
        contract_type = request.args.get('contract_type', 'PUT').upper()
        # Support both 'viz_type' (frontend) and 'visualization' (legacy) parameter names
        visualization = request.args.get('viz_type') or request.args.get('visualization', 'open_interest')
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

        # Calculate analysis metrics from the DataFrame
        underlying_price = df['underlying_price'].iloc[0] if 'underlying_price' in df.columns and len(df) > 0 else None

        # Calculate max pain (strike with minimum total value of options)
        max_pain = None
        try:
            if 'strike' in df.columns:
                strikes = df['strike'].unique()
                min_pain_value = float('inf')
                for strike in strikes:
                    call_oi = df[df['strike'] >= strike]['call_open_interest'].sum()
                    put_oi = df[df['strike'] <= strike]['put_open_interest'].sum()
                    pain_value = call_oi + put_oi
                    if pain_value < min_pain_value:
                        min_pain_value = pain_value
                        max_pain = float(strike)
        except Exception as e:
            logger.warning(f"Max pain calculation failed: {e}")

        # Calculate most liquid strike
        most_liquid_strike = None
        try:
            if 'call_volume' in df.columns and 'put_volume' in df.columns:
                df['total_volume'] = df['call_volume'].fillna(0) + df['put_volume'].fillna(0)
                if df['total_volume'].max() > 0:
                    most_liquid_idx = df['total_volume'].idxmax()
                    most_liquid_strike = float(df.loc[most_liquid_idx, 'strike'])
        except Exception as e:
            logger.warning(f"Most liquid strike calculation failed: {e}")

        # Total OI calculations
        total_calls_oi = int(df['call_open_interest'].sum()) if 'call_open_interest' in df.columns else 0
        total_puts_oi = int(df['put_open_interest'].sum()) if 'put_open_interest' in df.columns else 0
        put_call_ratio = round(total_puts_oi / total_calls_oi, 2) if total_calls_oi > 0 else 0

        logger.info(f"API: Heatmap generated - {len(df)} options, max_pain=${max_pain}")

        return jsonify({
            'success': True,
            'symbol': symbol,
            'contract_type': contract_type,
            'visualization': visualization,
            'heatmap': fig_json,  # Raw JSON for Plotly (frontend expects this)
            'current_price': underlying_price,
            'max_pain': max_pain,
            'liquidity_analysis': {
                'most_liquid_strike': most_liquid_strike
            },
            'total_calls_oi': total_calls_oi,
            'total_puts_oi': total_puts_oi,
            'put_call_ratio': put_call_ratio
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

        # Get parameters
        confidence = float(request.args.get('confidence', 0.95))
        time_horizon = int(request.args.get('time_horizon', 1))
        num_simulations = int(request.args.get('num_simulations', 10000))
        account_value = float(request.args.get('account_value', 100000))

        logger.info(f"API: Calculating VaR with {confidence*100}% confidence, {time_horizon}d horizon")

        # Load open positions from Schwab
        trades_result = _get_open_positions_compat()
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

        lookback_days = int(request.args.get('lookback_days', 90))

        logger.info(f"API: Calculating correlation matrix with {lookback_days}d lookback")

        # Load open positions from Schwab
        trades_result = _get_open_positions_compat()
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

        account_value = float(request.args.get('account_value', 100000))

        logger.info(f"API: Calculating margin requirements")

        # Load open positions from Schwab
        trades_result = _get_open_positions_compat()
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

        account_value = float(request.args.get('account_value', 100000))
        confidence = float(request.args.get('confidence', 0.95))

        logger.info(f"API: Generating full risk report")

        # Load open positions from Schwab
        trades_result = _get_open_positions_compat()
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

        # Load current positions from Schwab
        positions = get_schwab_csp_positions()
        if not positions:
            return jsonify({'alerts': [], 'message': 'No open trades'})

        # Convert to dict format expected by smart_alerts
        trades = positions

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

        # Load current trades from Schwab
        positions = get_schwab_csp_positions()
        if not positions:
            return jsonify({'success': True, 'portfolio_heat': 0, 'var_95': 0, 'message': 'No open positions'})

        trades = positions

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

        # Load current positions from Schwab
        trades = get_schwab_csp_positions()
        if not trades:
            return jsonify({'success': True, 'health_score': 100, 'status': 'No positions',
                            'message': 'Portfolio is empty'})

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

        # Load current positions from Schwab
        trades = get_schwab_csp_positions()
        if not trades:
            return jsonify({'success': True, 'scores': [], 'message': 'No open positions'})

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


@app.route('/api/grok/cost')
def grok_cost():
    """Return in-process Grok token usage and estimated cost since last server restart."""
    return jsonify(get_daily_token_cost())


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
        comparison = _call_grok(
            [
                {"role": "system", "content": "You are a quantitative options trading analyst specializing in cash-secured put strategies."},
                {"role": "user", "content": prompt},
            ],
            model=MODEL_FAST,
            max_tokens=300,
        )

        if not comparison:
            return jsonify({"success": False, "error": "Grok API unavailable"}), 502

        logger.info(f"Grok comparison completed: {len(comparison)} chars")

        return jsonify({
            'success': True,
            'comparison': comparison.strip()
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


# =============================================================================
# SCHWAB OAUTH WEB FLOW
# Uses schwab-py's local HTTPS listener on 127.0.0.1 (recommended callback 8182)
# while keeping the dashboard itself on port 5000.
# REDIRECT_URI in .env must exactly match an approved Schwab callback URL.
# =============================================================================

# Schwab auth state for the local single-user dashboard.
_pending_auth_context = None
_auth_flow_lock = threading.Lock()
_auth_flow_state = {
    'status': 'idle',
    'message': '',
    'started_at': None,
}

TOKEN_PATH = os.getenv('TOKEN_PATH', 'cache_files/schwab_token.json')
LAST_AUTH_PATH = 'cache_files/schwab_last_auth.txt'


def _set_auth_flow_state(status: str, message: str = '') -> None:
    _auth_flow_state['status'] = status
    _auth_flow_state['message'] = message
    _auth_flow_state['started_at'] = time.time() if status == 'running' else _auth_flow_state.get('started_at')


def _run_schwab_login_flow() -> None:
    from schwab import auth as schwab_auth

    api_key = os.getenv('SCHWAB_API_KEY')
    app_secret = os.getenv('SCHWAB_APP_SECRET')
    callback_url = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')

    try:
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        logger.info(f"Starting schwab-py login flow with callback {callback_url}")
        schwab_auth.client_from_login_flow(
            api_key=api_key,
            app_secret=app_secret,
            callback_url=callback_url,
            token_path=TOKEN_PATH,
            asyncio=False,
            enforce_enums=True,
            callback_timeout=300.0,
            interactive=False,
        )

        with open(LAST_AUTH_PATH, 'w') as f:
            f.write(str(time.time()))

        _set_auth_flow_state('success', 'Schwab token refreshed successfully.')
        logger.info('Schwab login flow completed successfully')
    except Exception as e:
        logger.error(f"Schwab login flow failed: {e}", exc_info=True)
        _set_auth_flow_state('error', f'Schwab sign-in failed: {e}')
    finally:
        with _auth_flow_lock:
            if _auth_flow_state['status'] == 'running':
                _set_auth_flow_state('idle', '')


@app.route('/api/auth/status')
def auth_status():
    """Check Schwab token health. Returns JSON without triggering any browser flow."""
    if _auth_flow_state['status'] == 'running':
        return jsonify({
            'status': 'auth_in_progress',
            'needs_reauth': False,
            'message': _auth_flow_state['message'] or 'Schwab sign-in flow is running in your browser.'
        })

    if _auth_flow_state['status'] == 'error':
        return jsonify({
            'status': 'auth_error',
            'needs_reauth': True,
            'message': _auth_flow_state['message'] or 'Schwab sign-in failed. Try again.'
        })

    if not os.path.exists(TOKEN_PATH):
        return jsonify({
            'status': 'missing',
            'needs_reauth': True,
            'message': 'No token file found — re-authorization required.'
        })

    try:
        with open(TOKEN_PATH, 'r') as f:
            token = json.load(f)

        now = time.time()

        # Determine days since last full OAuth (refresh token age proxy)
        if os.path.exists(LAST_AUTH_PATH):
            with open(LAST_AUTH_PATH, 'r') as f:
                last_auth_ts = float(f.read().strip())
        else:
            # Fall back to token file mtime as a proxy
            last_auth_ts = os.path.getmtime(TOKEN_PATH)

        days_since_auth = (now - last_auth_ts) / 86400
        needs_reauth = days_since_auth >= 7
        expiring_soon = days_since_auth >= 5

        if needs_reauth:
            return jsonify({
                'status': 'expired',
                'needs_reauth': True,
                'days_since_auth': round(days_since_auth, 1),
                'message': f'Schwab refresh token expired ({round(days_since_auth, 1)} days old). Click Re-Authorize.'
            })
        elif expiring_soon:
            return jsonify({
                'status': 'expiring_soon',
                'needs_reauth': False,
                'days_since_auth': round(days_since_auth, 1),
                'days_until_expiry': round(7 - days_since_auth, 1),
                'message': f'Token expires in {round(7 - days_since_auth, 1)} days — consider re-authorizing soon.'
            })
        else:
            return jsonify({
                'status': 'ok',
                'needs_reauth': False,
                'days_since_auth': round(days_since_auth, 1),
                'message': 'Schwab token is valid.'
            })

    except (json.JSONDecodeError, ValueError, OSError) as e:
        return jsonify({
            'status': 'error',
            'needs_reauth': True,
            'message': f'Token check failed: {str(e)}'
        })


@app.route('/auth/start')
def auth_start():
    """Start Schwab re-authorization using schwab-py's local HTTPS callback listener."""
    api_key = os.getenv('SCHWAB_API_KEY')
    app_secret = os.getenv('SCHWAB_APP_SECRET')
    callback_url = os.getenv('REDIRECT_URI', 'https://127.0.0.1:8182')

    if not api_key or not app_secret:
        return jsonify({'error': 'SCHWAB_API_KEY and SCHWAB_APP_SECRET must be configured'}), 500

    if not callback_url.startswith('https://127.0.0.1'):
        return jsonify({'error': 'REDIRECT_URI must use https://127.0.0.1 for schwab-py local login flow'}), 500

    try:
        with _auth_flow_lock:
            if _auth_flow_state['status'] == 'running':
                return """<html><body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px;text-align:center">
                    <h2 style="color:#60a5fa">Schwab Sign-In Already Running</h2>
                    <p>The browser-assisted login flow is already active. Finish the login window that opened, then return here.</p>
                    <p><a href="/" style="color:#60a5fa">Back to Dashboard</a></p>
                </body></html>"""

            _set_auth_flow_state('running', 'Opening Schwab sign-in in your browser...')
            thread = threading.Thread(target=_run_schwab_login_flow, daemon=True)
            thread.start()

        logger.info(f"Schwab OAuth flow started on callback listener {callback_url}")
        return f"""<html><head><meta http-equiv="refresh" content="10;url=/"></head>
        <body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px;text-align:center">
            <h2 style="color:#60a5fa">Starting Schwab Sign-In</h2>
            <p>Your browser should open automatically for Schwab login.</p>
            <p>Callback listener: <code>{callback_url}</code></p>
            <p>If the browser shows a self-signed certificate warning for <code>127.0.0.1:8182</code>, continue anyway. That is expected for the local callback listener.</p>
            <p>After you finish login, you will be redirected back to the dashboard.</p>
            <p><a href="/" style="color:#60a5fa">Return to Dashboard</a></p>
        </body></html>"""
    except Exception as e:
        _set_auth_flow_state('error', f'Schwab sign-in failed: {e}')
        logger.error(f"auth_start failed: {e}")
        return f"<h3>Failed to start auth: {e}</h3><a href='/'>Back</a>", 500


@app.route('/auth/callback')
def auth_callback():
    """Handle Schwab OAuth callback — exchanges code for tokens and saves them."""
    global _pending_auth_context
    from schwab import auth as schwab_auth

    if 'error' in request.args:
        err = request.args.get('error', 'unknown')
        logger.error(f"Schwab auth callback error: {err}")
        return f"""<html><body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px">
            <h2 style="color:#fb923c">&#9888; Authorization Failed</h2>
            <p>Schwab returned error: <code>{err}</code></p>
            <a href="/" style="color:#60a5fa">&#8592; Back to Dashboard</a>
        </body></html>""", 400

    if 'code' not in request.args:
        return "<h3>No authorization code received from Schwab.</h3><a href='/'>Back</a>", 400

    if _pending_auth_context is None:
        return """<html><body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px">
            <h2 style="color:#fb923c">&#9888; Session Expired</h2>
            <p>Auth session not found. Please <a href="/auth/start" style="color:#60a5fa">start again</a>.</p>
        </body></html>""", 400

    api_key = os.getenv('SCHWAB_API_KEY')
    app_secret = os.getenv('SCHWAB_APP_SECRET')
    received_url = request.url

    # HTTPS-behind-proxy fix: Schwab may redirect to http:// but we need to
    # match the registered callback URL exactly.
    if received_url.startswith('http://') and 'localhost' in received_url:
        pass  # localhost http is fine

    def token_write_func(token):
        os.makedirs('cache_files', exist_ok=True)
        with open(TOKEN_PATH, 'w') as tf:
            json.dump(token, tf, indent=2)
        logger.info(f"Schwab token written to {TOKEN_PATH}")

    try:
        schwab_auth.client_from_received_url(
            api_key=api_key,
            app_secret=app_secret,
            auth_context=_pending_auth_context,
            received_url=received_url,
            token_write_func=token_write_func,
        )

        # Record the timestamp so /api/auth/status can track the 7-day window
        os.makedirs('cache_files', exist_ok=True)
        with open(LAST_AUTH_PATH, 'w') as f:
            f.write(str(time.time()))

        _pending_auth_context = None
        logger.info("Schwab OAuth re-authorization successful")

        return """<html><head><meta http-equiv="refresh" content="2;url=/"></head>
        <body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px;text-align:center">
            <h2 style="color:#34d399">&#10003; Re-authorized Successfully!</h2>
            <p>Redirecting to dashboard&hellip;</p>
        </body></html>"""

    except Exception as e:
        logger.error(f"OAuth callback failed: {e}", exc_info=True)
        _pending_auth_context = None
        return f"""<html><body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:40px">
            <h2 style="color:#fb923c">&#9888; Token Exchange Failed</h2>
            <p>{e}</p>
            <p><a href="/auth/start" style="color:#60a5fa">Try again</a> &nbsp;|
               <a href="/" style="color:#60a5fa">Dashboard</a></p>
        </body></html>""", 500


# ==================== CACHE MANAGEMENT ROUTES ====================

CACHE_KEEP = {"schwab_token.json", "schwab_last_auth.txt"}

@app.route('/api/clear_cache', methods=['POST'])
def api_clear_cache():
    """
    Delete Grok opportunity caches, scanner caches, and market pulse cache.
    Preserves schwab_token.json, schwab_last_auth.txt, and the trade outcomes DB.
    """
    import glob
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache_files')
    patterns = [
        'grok_opp_*.json',
        'grok_market_sentiment.json',
        'grok_market_pulse.json',
        'simple_scanner_cache.json',
        'leaps_cache.json',
        '0dte_spreads_cache.json',
        'support_resistance_cache.json',
    ]
    deleted = []
    errors = []
    for pattern in patterns:
        for path in glob.glob(os.path.join(cache_dir, pattern)):
            fname = os.path.basename(path)
            if fname in CACHE_KEEP:
                continue
            try:
                os.remove(path)
                deleted.append(fname)
            except Exception as e:
                errors.append(f"{fname}: {e}")

    logger.info(f"Cache cleared: {len(deleted)} files deleted")
    return jsonify({'ok': True, 'data': {
        'deleted': len(deleted),
        'files': deleted,
        'errors': errors,
    }})


# ==================== TRADE JOURNAL ROUTES ====================

@app.route('/api/trade_journal/stats')
def api_trade_journal_stats():
    """Get aggregate trade statistics."""
    try:
        from trade_outcome_tracker import get_trade_stats
        days = request.args.get('days', 90, type=int)
        stats = get_trade_stats(days)
        return jsonify({'ok': True, 'data': stats})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/trade_journal/recent')
def api_trade_journal_recent():
    """Get recent trades."""
    try:
        from trade_outcome_tracker import get_recent_trades
        limit = request.args.get('limit', 20, type=int)
        trades = get_recent_trades(limit)
        return jsonify({'ok': True, 'data': trades})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/trade_journal/open')
def api_trade_journal_open():
    """Get open trades."""
    try:
        from trade_outcome_tracker import get_open_trades
        trades = get_open_trades()
        return jsonify({'ok': True, 'data': trades})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/trade_journal/sync')
def api_trade_journal_sync():
    """Trigger Schwab API sync for trade outcomes."""
    try:
        from trade_outcome_tracker import sync_from_schwab
        result = sync_from_schwab()
        return jsonify({'ok': True, 'data': result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/trade_journal/score_vs_outcome')
def api_trade_journal_calibration():
    """Score vs outcome analysis for calibration."""
    try:
        from trade_outcome_tracker import get_score_vs_outcome
        data = get_score_vs_outcome()
        return jsonify({'ok': True, 'data': data})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("DASHBOARD SERVER STARTING")
    logger.info("=" * 60)
    logger.info("Server URL: http://127.0.0.1:5101")
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
    logger.info("Trade Journal:")
    logger.info("  GET  /api/trade_journal/stats       - Aggregate trade stats (win rate, PnL)")
    logger.info("  GET  /api/trade_journal/recent      - Recent trades")
    logger.info("  GET  /api/trade_journal/open        - Open trades")
    logger.info("  GET  /api/trade_journal/sync        - Manual Schwab sync trigger")
    logger.info("  GET  /api/trade_journal/score_vs_outcome - Score calibration data")
    logger.info("")
    logger.info("Improvements:")
    logger.info("  - Parallel scanner execution (2-3x faster)")
    logger.info("  - Circuit breaker for API resilience")
    logger.info("  - SQLite database backend")
    logger.info("  - Portfolio Greeks tracking")
    logger.info("  - Dynamic exit targets")
    logger.info("  - Trade journal with analytics")
    logger.info("  - Bollinger Bands technical analysis")

    # --- Schwab token expiry Telegram alert (runs every 12 hours) ---
    def _check_token_and_alert():
        """Send a Telegram message if the Schwab refresh token is expiring soon."""
        try:
            _WARN_DAYS = 2  # alert when <= 2 days remain in the 7-day window
            _INTERVAL_HOURS = 12

            tg_token = os.getenv('TELEGRAM_TOKEN')
            tg_chat  = os.getenv('TELEGRAM_CHAT_ID')

            if tg_token and tg_chat and os.path.exists(LAST_AUTH_PATH):
                with open(LAST_AUTH_PATH) as f:
                    last_auth_ts = float(f.read().strip())
                days_since = (time.time() - last_auth_ts) / 86400
                days_left  = 7 - days_since
                if days_left <= _WARN_DAYS:
                    msg = (
                        f"⚠️ Schwab token expiring in {days_left:.1f} day(s)!\n"
                        f"Re-authorize now: https://127.0.0.1:5000/auth/start"
                    )
                    asyncio.run(
                        telegram_bot(token=tg_token).send_message(chat_id=tg_chat, text=msg)
                    )
                    logger.info(f"Schwab token expiry Telegram alert sent ({days_left:.1f} days left)")
        except Exception as e:
            logger.warning(f"Token expiry alert check failed: {e}")
        finally:
            # Re-schedule regardless of success/failure
            threading.Timer(12 * 3600, _check_token_and_alert).start()

    # Run immediately on startup, then every 12 hours
    threading.Timer(30, _check_token_and_alert).start()  # 30s delay so server is ready first
    logger.info("Schwab token expiry check scheduled (every 12 hours, Telegram alert if <= 2 days remain)")

    # --- Auto-regenerate dashboard HTML on startup ---
    def _regen_dashboard():
        try:
            import generate_dashboard
            generate_dashboard.generate_html()
            logger.info("Dashboard HTML regenerated on startup")
        except Exception as e:
            logger.warning(f"Dashboard auto-regen failed: {e}")
    threading.Timer(5, _regen_dashboard).start()
    logger.info("Dashboard HTML will auto-regenerate in 5s")

    # --- Schwab trade sync (runs every 4 hours during market hours) ---
    def _scheduled_schwab_sync():
        """Sync trade outcomes from Schwab API on a recurring schedule."""
        try:
            now_et = datetime.now(ET_TZ)
            hour = now_et.hour
            weekday = now_et.weekday()  # 0=Mon, 6=Sun

            # Only sync Mon-Fri, 6am-8pm ET (covers pre-market through post-close)
            if weekday < 5 and 6 <= hour <= 20:
                from trade_outcome_tracker import sync_from_schwab
                result = sync_from_schwab()
                if result.get('success'):
                    logger.info(
                        f"Scheduled Schwab sync: {result.get('trades_created', 0)} new, "
                        f"{result.get('trades_closed', 0)} closed"
                    )
                else:
                    logger.warning(f"Scheduled Schwab sync failed: {result.get('error', 'unknown')}")
            else:
                logger.debug("Schwab sync skipped (outside market hours)")
        except Exception as e:
            logger.warning(f"Scheduled Schwab sync error: {e}")
        finally:
            threading.Timer(4 * 3600, _scheduled_schwab_sync).start()

    # Start after 60s so server and auth are ready
    threading.Timer(60, _scheduled_schwab_sync).start()
    logger.info("Schwab trade sync scheduled (every 4 hours, Mon-Fri 6am-8pm ET)")

    # Production-ready configuration
    # For development, use Flask dev server
    # For production, use: gunicorn -w 4 -b 0.0.0.0:5000 dashboard_server:app
    app.run(
        host='127.0.0.1',
        port=5101,
        debug=False,
        use_reloader=False,
        threaded=True,   # Enable threading for concurrent requests
        ssl_context='adhoc'  # HTTPS required for Schwab OAuth callback
    )