"""
Earnings Calendar - Track earnings dates and provide trade recommendations
Integrates with yfinance for earnings data and IV analysis
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

from datetime import datetime, timedelta
from typing import Dict, Optional, List
import logging
import yfinance as yf

logger = logging.getLogger(__name__)


def check_earnings_conflict(symbol: str, target_date: datetime) -> bool:
    """
    Check if symbol has earnings around target date

    Args:
        symbol: Stock symbol
        target_date: Target expiration date

    Returns:
        True if earnings conflict exists, False otherwise
    """
    try:
        ticker = yf.Ticker(symbol)

        # Get earnings dates from yfinance
        calendar = ticker.calendar

        if calendar is None or calendar.empty:
            # No earnings data available - assume no conflict (safe default)
            return False

        # Check if 'Earnings Date' exists in calendar
        if 'Earnings Date' in calendar.index:
            earnings_dates = calendar.loc['Earnings Date']

            # Handle both single date and date range
            if isinstance(earnings_dates, (list, tuple)):
                for earnings_date in earnings_dates:
                    if isinstance(earnings_date, datetime):
                        days_diff = abs((earnings_date - target_date).days)
                        if days_diff <= 7:  # Within 1 week
                            logger.info(f"{symbol} has earnings on {earnings_date.date()} near target {target_date.date()}")
                            return True
            elif isinstance(earnings_dates, datetime):
                days_diff = abs((earnings_dates - target_date).days)
                if days_diff <= 7:
                    logger.info(f"{symbol} has earnings on {earnings_dates.date()} near target {target_date.date()}")
                    return True

        return False

    except Exception as e:
        logger.error(f"Error checking earnings for {symbol}: {e}")
        # Return False (no conflict) as safe default
        return False


def get_earnings_recommendation(symbol: str, days_until_earnings: int) -> str:
    """
    Get trading recommendation based on earnings proximity

    Args:
        symbol: Stock symbol
        days_until_earnings: Days until next earnings

    Returns:
        Recommendation string
    """
    if days_until_earnings <= 0:
        return "🚨 AVOID - Earnings today or passed"
    elif days_until_earnings <= 1:
        return "🚨 AVOID - Earnings within 1 day"
    elif days_until_earnings <= 3:
        return "⚠️ CAUTION - Earnings within 3 days (high IV)"
    elif days_until_earnings <= 7:
        return "⚠️ PROCEED WITH CAUTION - Earnings next week"
    elif days_until_earnings <= 14:
        return "💡 MONITOR - Earnings in 2 weeks"
    elif days_until_earnings <= 30:
        return "✅ ACCEPTABLE - Earnings 2-4 weeks out"
    else:
        return "✅ GOOD - Earnings >30 days away"


def get_next_earnings_date(symbol: str) -> Optional[datetime]:
    """
    Get next earnings date for symbol

    Args:
        symbol: Stock symbol

    Returns:
        Next earnings date or None if not available
    """
    try:
        ticker = yf.Ticker(symbol)
        calendar = ticker.calendar

        if calendar is None or calendar.empty:
            return None

        if 'Earnings Date' in calendar.index:
            earnings_dates = calendar.loc['Earnings Date']

            # Handle both single date and date range
            if isinstance(earnings_dates, (list, tuple)) and len(earnings_dates) > 0:
                # Return first date in range (typically the expected date)
                return earnings_dates[0] if isinstance(earnings_dates[0], datetime) else None
            elif isinstance(earnings_dates, datetime):
                return earnings_dates

        return None

    except Exception as e:
        logger.error(f"Error getting earnings date for {symbol}: {e}")
        return None


def analyze_earnings_iv_impact(symbol: str) -> Dict[str, any]:
    """
    Analyze potential IV impact from upcoming earnings

    Args:
        symbol: Stock symbol

    Returns:
        Dictionary with IV analysis and recommendations
    """
    try:
        # Get next earnings date
        next_earnings = get_next_earnings_date(symbol)

        if not next_earnings:
            return {
                'has_earnings_data': False,
                'recommendation': '✅ No earnings data available - proceed normally',
                'days_until_earnings': None,
                'earnings_date': None
            }

        # Calculate days until earnings
        today = datetime.now()
        days_until = (next_earnings - today).days

        # Get recommendation
        recommendation = get_earnings_recommendation(symbol, days_until)

        # Estimate IV impact based on proximity
        if days_until <= 3:
            iv_impact = 'VERY HIGH - IV crush likely post-earnings'
        elif days_until <= 7:
            iv_impact = 'HIGH - IV elevated pre-earnings'
        elif days_until <= 14:
            iv_impact = 'MODERATE - IV starting to build'
        else:
            iv_impact = 'LOW - Normal IV levels'

        # Strategy suggestions
        if days_until <= 1:
            strategy_suggestion = 'Avoid new positions. Consider closing existing positions.'
        elif days_until <= 3:
            strategy_suggestion = 'Avoid CSPs. Consider credit spreads to limit risk.'
        elif days_until <= 7:
            strategy_suggestion = 'Use tighter strikes. Reduce position size.'
        else:
            strategy_suggestion = 'Normal trading - monitor earnings date as expiration approaches'

        return {
            'has_earnings_data': True,
            'earnings_date': next_earnings.strftime('%Y-%m-%d'),
            'days_until_earnings': days_until,
            'recommendation': recommendation,
            'iv_impact': iv_impact,
            'strategy_suggestion': strategy_suggestion,
            'should_avoid': days_until <= 3
        }

    except Exception as e:
        logger.error(f"Error analyzing earnings IV impact for {symbol}: {e}")
        return {
            'has_earnings_data': False,
            'recommendation': '⚠️ Error fetching earnings data',
            'error': str(e)
        }


def get_earnings_calendar_for_symbols(symbols: List[str]) -> Dict[str, Dict]:
    """
    Get earnings calendar for multiple symbols

    Args:
        symbols: List of stock symbols

    Returns:
        Dictionary mapping symbol to earnings info
    """
    earnings_calendar = {}

    for symbol in symbols:
        try:
            earnings_info = analyze_earnings_iv_impact(symbol)
            earnings_calendar[symbol] = earnings_info
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            earnings_calendar[symbol] = {
                'has_earnings_data': False,
                'error': str(e)
            }

    return earnings_calendar


def filter_symbols_by_earnings(symbols: List[str], max_days_until_earnings: int = 7) -> List[str]:
    """
    Filter out symbols with earnings within specified days

    Args:
        symbols: List of symbols to filter
        max_days_until_earnings: Max days until earnings (default 7)

    Returns:
        Filtered list of symbols without earnings conflicts
    """
    filtered = []

    for symbol in symbols:
        try:
            next_earnings = get_next_earnings_date(symbol)

            if not next_earnings:
                # No earnings data - include symbol
                filtered.append(symbol)
                continue

            days_until = (next_earnings - datetime.now()).days

            if days_until > max_days_until_earnings:
                filtered.append(symbol)
            else:
                logger.info(f"Filtered out {symbol} - earnings in {days_until} days")

        except Exception as e:
            logger.error(f"Error filtering {symbol}: {e}")
            # Include on error (conservative - don't exclude without certainty)
            filtered.append(symbol)

    return filtered
