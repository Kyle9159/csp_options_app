"""
Smart Alerts - Intelligent trade alerts based on technical indicators
Uses Bollinger Bands, RSI, volume, and Greeks to generate actionable alerts
"""

import sys
import io

# FIX WINDOWS EMOJI ENCODING FIRST
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import numpy as np
from helper_functions import safe_float, safe_int

logger = logging.getLogger(__name__)


def run_alert_scan(trades: Optional[List[Dict[str, Any]]] = None,
                   watchlist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Run smart alerts scan on open trades and watchlist

    Args:
        trades: List of open trades to monitor
        watchlist: List of symbols to watch for opportunities

    Returns:
        List of alert dictionaries with priority, type, and message
    """
    alerts = []

    try:
        # Scan open trades
        if trades:
            logger.info(f"Scanning {len(trades)} open trades for alerts...")
            for trade in trades:
                trade_alerts = _scan_trade_for_alerts(trade)
                alerts.extend(trade_alerts)

        # Scan watchlist for opportunities
        if watchlist:
            logger.info(f"Scanning {len(watchlist)} watchlist symbols...")
            for symbol in watchlist:
                watchlist_alerts = _scan_symbol_for_opportunities(symbol)
                alerts.extend(watchlist_alerts)

        # Sort by priority (HIGH, MEDIUM, LOW)
        priority_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
        alerts.sort(key=lambda x: priority_order.get(x.get('priority', 'LOW'), 4))

        logger.info(f"Generated {len(alerts)} smart alerts")
        return alerts

    except Exception as e:
        logger.error(f"Error running smart alerts scan: {e}")
        return []


def _scan_trade_for_alerts(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Scan a single trade for alerts"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')

    try:
        # Get current market data
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='30d')

        if hist.empty:
            return alerts

        current_price = hist['Close'].iloc[-1]

        # Calculate Bollinger Bands
        bb_data = calculate_bollinger_bands(hist)

        # Greeks-based alerts
        alerts.extend(_check_greeks_alerts(trade))

        # Price-based alerts
        alerts.extend(_check_price_alerts(trade, current_price, bb_data))

        # Time-based alerts
        alerts.extend(_check_time_alerts(trade))

        # Volume alerts
        alerts.extend(_check_volume_alerts(trade, hist))

        # P/L alerts
        alerts.extend(_check_pl_alerts(trade))

    except Exception as e:
        logger.error(f"Error scanning trade {symbol}: {e}")

    return alerts


def _scan_symbol_for_opportunities(symbol: str) -> List[Dict[str, Any]]:
    """Scan watchlist symbol for trading opportunities"""
    alerts = []

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='30d')

        if hist.empty:
            return alerts

        current_price = hist['Close'].iloc[-1]

        # Calculate Bollinger Bands
        bb_data = calculate_bollinger_bands(hist)

        # Check for oversold conditions (Bollinger Band bounce opportunity)
        if bb_data and bb_data.get('position') == 'below_lower':
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'OPPORTUNITY',
                'symbol': symbol,
                'message': f"💡 {symbol} at lower Bollinger Band - potential bounce opportunity",
                'current_price': round(current_price, 2),
                'lower_band': bb_data.get('lower_band')
            })

        # Check for overbought (potential covered call opportunity)
        if bb_data and bb_data.get('position') == 'above_upper':
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'OPPORTUNITY',
                'symbol': symbol,
                'message': f"💡 {symbol} at upper Bollinger Band - potential covered call opportunity",
                'current_price': round(current_price, 2),
                'upper_band': bb_data.get('upper_band')
            })

        # Volume surge detection
        avg_volume = hist['Volume'].mean()
        current_volume = hist['Volume'].iloc[-1]

        if current_volume > avg_volume * 2:
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'VOLUME_SURGE',
                'symbol': symbol,
                'message': f"📊 {symbol} volume surge - {current_volume/avg_volume:.1f}x average",
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            })

    except Exception as e:
        logger.error(f"Error scanning symbol {symbol}: {e}")

    return alerts


def _check_greeks_alerts(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check for Greeks-based alerts"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')

    # Delta alerts
    delta = abs(safe_float(trade.get('Delta', 0)))
    if delta > 0.50:
        alerts.append({
            'priority': 'HIGH',
            'type': 'GREEKS',
            'symbol': symbol,
            'message': f"⚠️ {symbol} - Delta {delta:.2f} > 0.50 - Position at risk",
            'delta': delta
        })
    elif delta > 0.40:
        alerts.append({
            'priority': 'MEDIUM',
            'type': 'GREEKS',
            'symbol': symbol,
            'message': f"⚠️ {symbol} - Delta {delta:.2f} > 0.40 - Monitor closely",
            'delta': delta
        })

    # Vega alerts (high vol risk)
    vega = abs(safe_float(trade.get('Vega', 0)))
    if vega > 0.40:
        alerts.append({
            'priority': 'MEDIUM',
            'type': 'GREEKS',
            'symbol': symbol,
            'message': f"📊 {symbol} - High vega {vega:.2f} - Volatility risk elevated",
            'vega': vega
        })

    # Theta efficiency alert (positive - good decay)
    theta = abs(safe_float(trade.get('Theta', 0)))
    dte = safe_int(trade.get('DTE', 30))

    if dte > 0:
        theta_efficiency = theta / dte
        if theta_efficiency > 0.004:
            alerts.append({
                'priority': 'LOW',
                'type': 'GREEKS',
                'symbol': symbol,
                'message': f"✅ {symbol} - Excellent theta decay (${theta*100:.2f}/day)",
                'theta': theta,
                'dte': dte
            })

    return alerts


def _check_price_alerts(trade: Dict[str, Any], current_price: float,
                       bb_data: Optional[Dict]) -> List[Dict[str, Any]]:
    """Check for price-based alerts using Bollinger Bands"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')
    strike = safe_float(trade.get('Strike', 0))

    # Price approaching strike
    if strike > 0:
        distance_pct = ((current_price - strike) / current_price) * 100

        if distance_pct < 5 and distance_pct > 0:
            alerts.append({
                'priority': 'HIGH',
                'type': 'PRICE',
                'symbol': symbol,
                'message': f"⚠️ {symbol} within 5% of strike ${strike} - Current: ${current_price:.2f}",
                'current_price': current_price,
                'strike': strike,
                'distance_pct': round(distance_pct, 2)
            })
        elif distance_pct < 0:  # ITM
            alerts.append({
                'priority': 'CRITICAL',
                'type': 'PRICE',
                'symbol': symbol,
                'message': f"🚨 {symbol} ITM - Price ${current_price:.2f} below strike ${strike}",
                'current_price': current_price,
                'strike': strike
            })

    # Bollinger Band alerts
    if bb_data:
        position = bb_data.get('position')

        if position == 'below_lower':
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'TECHNICAL',
                'symbol': symbol,
                'message': f"📉 {symbol} below lower Bollinger Band - Oversold, potential bounce",
                'current_price': current_price,
                'lower_band': bb_data.get('lower_band')
            })
        elif position == 'above_upper':
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'TECHNICAL',
                'symbol': symbol,
                'message': f"📈 {symbol} above upper Bollinger Band - Overbought",
                'current_price': current_price,
                'upper_band': bb_data.get('upper_band')
            })

    return alerts


def _check_time_alerts(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check for time-based alerts"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')
    dte = safe_int(trade.get('DTE', 99))

    if dte <= 3:
        alerts.append({
            'priority': 'CRITICAL',
            'type': 'TIME',
            'symbol': symbol,
            'message': f"⏰ {symbol} - {dte} DTE - EXPIRING SOON",
            'dte': dte
        })
    elif dte <= 7:
        alerts.append({
            'priority': 'HIGH',
            'type': 'TIME',
            'symbol': symbol,
            'message': f"⏰ {symbol} - {dte} DTE - Approaching expiration",
            'dte': dte
        })
    elif dte <= 14:
        alerts.append({
            'priority': 'MEDIUM',
            'type': 'TIME',
            'symbol': symbol,
            'message': f"📅 {symbol} - {dte} DTE - 2 weeks remaining",
            'dte': dte
        })

    return alerts


def _check_volume_alerts(trade: Dict[str, Any], hist: pd.DataFrame) -> List[Dict[str, Any]]:
    """Check for volume-based alerts"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')

    try:
        if len(hist) < 5:
            return alerts

        avg_volume = hist['Volume'].iloc[-20:].mean() if len(hist) >= 20 else hist['Volume'].mean()
        current_volume = hist['Volume'].iloc[-1]

        # Volume surge (2x+ average)
        if current_volume > avg_volume * 2:
            alerts.append({
                'priority': 'MEDIUM',
                'type': 'VOLUME',
                'symbol': symbol,
                'message': f"📊 {symbol} - Volume surge: {current_volume/avg_volume:.1f}x average",
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            })

        # Volume drying up (could signal reversal)
        if current_volume < avg_volume * 0.5:
            alerts.append({
                'priority': 'LOW',
                'type': 'VOLUME',
                'symbol': symbol,
                'message': f"📊 {symbol} - Low volume: {current_volume/avg_volume:.1f}x average",
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            })

    except Exception as e:
        logger.error(f"Error checking volume for {symbol}: {e}")

    return alerts


def _check_pl_alerts(trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check for P/L-based alerts"""
    alerts = []
    symbol = trade.get('Symbol', 'Unknown')
    pl_pct = safe_float(trade.get('Current P/L %', 0))

    # Profit target hit
    if pl_pct >= 50:
        alerts.append({
            'priority': 'HIGH',
            'type': 'PROFIT',
            'symbol': symbol,
            'message': f"✅ {symbol} - Profit target hit: {pl_pct:.1f}% - Consider closing",
            'pl_pct': pl_pct
        })
    elif pl_pct >= 25:
        alerts.append({
            'priority': 'MEDIUM',
            'type': 'PROFIT',
            'symbol': symbol,
            'message': f"💰 {symbol} - Partial profit: {pl_pct:.1f}%",
            'pl_pct': pl_pct
        })

    # Stop loss alerts
    if pl_pct <= -50:
        alerts.append({
            'priority': 'CRITICAL',
            'type': 'LOSS',
            'symbol': symbol,
            'message': f"🚨 {symbol} - STOP LOSS BREACHED: {pl_pct:.1f}%",
            'pl_pct': pl_pct
        })
    elif pl_pct <= -25:
        alerts.append({
            'priority': 'HIGH',
            'type': 'LOSS',
            'symbol': symbol,
            'message': f"⚠️ {symbol} - Significant loss: {pl_pct:.1f}%",
            'pl_pct': pl_pct
        })

    return alerts


def calculate_bollinger_bands(hist: pd.DataFrame, period: int = 20,
                              std_dev: float = 2.0) -> Optional[Dict[str, Any]]:
    """
    Calculate Bollinger Bands for price data

    Args:
        hist: DataFrame with price history (must have 'Close' column)
        period: Moving average period (default 20)
        std_dev: Standard deviations for bands (default 2.0)

    Returns:
        Dictionary with upper_band, middle_band, lower_band, and current position
    """
    try:
        if len(hist) < period:
            return None

        # Calculate bands
        sma = hist['Close'].rolling(window=period).mean()
        std = hist['Close'].rolling(window=period).std()

        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)

        # Get latest values
        current_price = hist['Close'].iloc[-1]
        latest_upper = upper_band.iloc[-1]
        latest_middle = sma.iloc[-1]
        latest_lower = lower_band.iloc[-1]

        # Determine position
        if current_price > latest_upper:
            position = 'above_upper'
        elif current_price < latest_lower:
            position = 'below_lower'
        elif current_price > latest_middle:
            position = 'upper_half'
        else:
            position = 'lower_half'

        # Calculate %B (position within bands)
        percent_b = (current_price - latest_lower) / (latest_upper - latest_lower) if (latest_upper - latest_lower) > 0 else 0.5

        # Calculate bandwidth (volatility measure)
        bandwidth = ((latest_upper - latest_lower) / latest_middle) * 100 if latest_middle > 0 else 0

        return {
            'upper_band': round(latest_upper, 2),
            'middle_band': round(latest_middle, 2),
            'lower_band': round(latest_lower, 2),
            'current_price': round(current_price, 2),
            'position': position,
            'percent_b': round(percent_b, 2),
            'bandwidth': round(bandwidth, 2)
        }

    except Exception as e:
        logger.error(f"Error calculating Bollinger Bands: {e}")
        return None


def calculate_rsi(hist: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Calculate RSI (Relative Strength Index)

    Args:
        hist: DataFrame with price history
        period: RSI period (default 14)

    Returns:
        RSI value (0-100)
    """
    try:
        if len(hist) < period + 1:
            return None

        # Calculate price changes
        delta = hist['Close'].diff()

        # Separate gains and losses
        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)

        # Calculate average gains and losses
        avg_gains = gains.rolling(window=period).mean()
        avg_losses = losses.rolling(window=period).mean()

        # Calculate RS and RSI
        rs = avg_gains / avg_losses
        rsi = 100 - (100 / (1 + rs))

        return round(rsi.iloc[-1], 2)

    except Exception as e:
        logger.error(f"Error calculating RSI: {e}")
        return None
