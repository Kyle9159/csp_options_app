"""
Dynamic Exit Targets - Intelligent exit target calculation
Uses theta decay, volatility, Greeks, and support/resistance
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

from typing import Dict, Optional
from datetime import datetime, timedelta
from helper_functions import safe_float, safe_int
import logging

logger = logging.getLogger(__name__)


def calculate_exit_targets(
    current_price: float,
    entry_price: float,
    position_type: str = "short_put",
    **kwargs
) -> Dict[str, float]:
    """
    Calculate dynamic exit targets based on multiple factors

    Args:
        current_price: Current underlying price
        entry_price: Entry price (for stock) or premium (for options)
        position_type: Type of position ("long", "short", "short_put", "covered_call")
        **kwargs: Additional parameters:
            - theta: Theta value
            - delta: Delta value
            - vega: Vega value
            - dte: Days to expiration
            - iv: Implied volatility
            - strike: Strike price (for options)
            - premium_collected: Premium collected (for options)
            - support: Support level
            - resistance: Resistance level

    Returns:
        Dictionary with profit_target, stop_loss, early_exit_target, and adjustment_trigger
    """
    try:
        # Extract optional parameters
        theta = safe_float(kwargs.get('theta', 0))
        delta = safe_float(kwargs.get('delta', 0))
        vega = safe_float(kwargs.get('vega', 0))
        dte = safe_int(kwargs.get('dte', 30))
        iv = safe_float(kwargs.get('iv', 0.30))
        strike = safe_float(kwargs.get('strike', 0))
        premium = safe_float(kwargs.get('premium_collected', entry_price))
        support = safe_float(kwargs.get('support', 0))
        resistance = safe_float(kwargs.get('resistance', 0))

        if position_type.lower() in ["short_put", "csp", "cash_secured_put"]:
            return _calculate_short_put_targets(
                current_price, strike, premium, theta, delta, dte, iv, support
            )
        elif position_type.lower() in ["covered_call", "cc"]:
            return _calculate_covered_call_targets(
                current_price, strike, premium, theta, delta, dte, iv, resistance
            )
        elif position_type.lower() == "long":
            return _calculate_long_targets(current_price, entry_price, support, resistance)
        else:  # "short"
            return _calculate_short_targets(current_price, entry_price, support, resistance)

    except Exception as e:
        logger.error(f"Error calculating exit targets: {e}")
        return _fallback_targets(current_price, entry_price, position_type)


def _calculate_short_put_targets(
    current_price: float,
    strike: float,
    premium: float,
    theta: float,
    delta: float,
    dte: int,
    iv: float,
    support: float = 0
) -> Dict[str, any]:
    """Calculate exit targets for cash-secured puts"""

    # Profit target: typically 50% of max profit
    profit_target_premium = premium * 0.50  # Take profit at 50% of premium

    # Early exit: 25% profit if DTE < 21
    early_exit_premium = premium * 0.75  # Close when premium decays to 75% of original (25% profit)

    # Stop loss: if delta > 0.50 or price approaches strike
    if strike > 0:
        stop_loss_price = strike * 1.02  # 2% buffer above strike
        emergency_exit_price = strike * 0.98  # Emergency if price goes below strike

        # Adjust based on support
        if support > 0 and support < strike:
            # Support exists below strike - can be more aggressive
            stop_loss_price = strike * 1.01  # Tighter stop
    else:
        # Fallback if no strike provided
        stop_loss_price = current_price * 0.95
        emergency_exit_price = current_price * 0.90

    # Theta-adjusted early exit
    # If theta decay is strong and we've captured significant premium, consider early exit
    if dte > 0 and abs(theta) > 0:
        theta_efficiency = abs(theta) / dte
        if theta_efficiency > 0.003:  # High theta efficiency
            # Can exit earlier to lock in gains
            early_exit_premium = premium * 0.80  # Exit at 20% profit if theta is high

    # Adjustment trigger: when delta exceeds threshold or DTE is low
    adjustment_trigger_delta = 0.45  # Adjust if delta exceeds 45
    adjustment_trigger_dte = 7  # Consider rolling if DTE < 7 and position is challenged

    return {
        'profit_target': round(profit_target_premium, 2),
        'profit_target_pct': 50.0,
        'early_exit_target': round(early_exit_premium, 2),
        'early_exit_pct': 25.0,
        'stop_loss_price': round(stop_loss_price, 2),
        'emergency_exit_price': round(emergency_exit_price, 2),
        'adjustment_trigger_delta': adjustment_trigger_delta,
        'adjustment_trigger_dte': adjustment_trigger_dte,
        'recommendation': _get_short_put_recommendation(
            current_price, strike, delta, dte, premium, profit_target_premium
        )
    }


def _calculate_covered_call_targets(
    current_price: float,
    strike: float,
    premium: float,
    theta: float,
    delta: float,
    dte: int,
    iv: float,
    resistance: float = 0
) -> Dict[str, any]:
    """Calculate exit targets for covered calls"""

    # Profit target: 50% of max profit
    profit_target_premium = premium * 0.50

    # Early exit: 25% profit
    early_exit_premium = premium * 0.75

    # Stop loss: if stock drops significantly
    stop_loss_price = current_price * 0.90  # 10% stock drop triggers review

    # If resistance exists above, we're safer
    if resistance > 0 and resistance > strike:
        # Resistance above strike - safer position
        pass  # No adjustment needed

    # Assignment risk: if price > strike and DTE < 7
    assignment_risk_price = strike * 1.02  # Within 2% of strike near expiration = high assignment risk

    return {
        'profit_target': round(profit_target_premium, 2),
        'profit_target_pct': 50.0,
        'early_exit_target': round(early_exit_premium, 2),
        'early_exit_pct': 25.0,
        'stop_loss_price': round(stop_loss_price, 2),
        'assignment_risk_price': round(assignment_risk_price, 2),
        'adjustment_trigger_dte': 7,
        'recommendation': _get_covered_call_recommendation(
            current_price, strike, delta, dte, premium, profit_target_premium
        )
    }


def _calculate_long_targets(
    current_price: float,
    entry_price: float,
    support: float = 0,
    resistance: float = 0
) -> Dict[str, float]:
    """Calculate exit targets for long positions"""

    # Use resistance as profit target if available
    if resistance > 0 and resistance > current_price:
        profit_target = resistance
    else:
        profit_target = entry_price * 1.10  # 10% profit

    # Use support as stop loss if available
    if support > 0 and support < current_price:
        stop_loss = support * 0.98  # Slightly below support
    else:
        stop_loss = entry_price * 0.95  # 5% stop loss

    return {
        'profit_target': round(profit_target, 2),
        'stop_loss': round(stop_loss, 2),
        'entry_price': round(entry_price, 2)
    }


def _calculate_short_targets(
    current_price: float,
    entry_price: float,
    support: float = 0,
    resistance: float = 0
) -> Dict[str, float]:
    """Calculate exit targets for short positions"""

    # Use support as profit target if available
    if support > 0 and support < current_price:
        profit_target = support
    else:
        profit_target = entry_price * 0.90  # 10% profit

    # Use resistance as stop loss if available
    if resistance > 0 and resistance > current_price:
        stop_loss = resistance * 1.02  # Slightly above resistance
    else:
        stop_loss = entry_price * 1.05  # 5% stop loss

    return {
        'profit_target': round(profit_target, 2),
        'stop_loss': round(stop_loss, 2),
        'entry_price': round(entry_price, 2)
    }


def _get_short_put_recommendation(
    current_price: float,
    strike: float,
    delta: float,
    dte: int,
    current_premium: float,
    profit_target: float
) -> str:
    """Generate recommendation for short put position"""

    # Check if at profit target
    if current_premium <= profit_target:
        return "✅ CLOSE - Profit target reached (50%)"

    # Check if near expiration with profit
    if dte <= 7 and current_premium < profit_target * 1.5:
        return "✅ CLOSE EARLY - Near expiration with profit"

    # Check if delta is too high
    if abs(delta) > 0.50:
        return "⚠️ ADJUST - Delta >0.50, consider rolling"

    # Check if price near strike
    if strike > 0 and current_price < strike * 1.05:
        return "⚠️ MONITOR CLOSELY - Price near strike"

    # Check if DTE is low
    if dte <= 3:
        return "⏰ MANAGE - Expiration approaching"

    # Default
    return "✅ HOLD - Position on track"


def _get_covered_call_recommendation(
    current_price: float,
    strike: float,
    delta: float,
    dte: int,
    current_premium: float,
    profit_target: float
) -> str:
    """Generate recommendation for covered call position"""

    # Check if at profit target
    if current_premium <= profit_target:
        return "✅ CLOSE - Profit target reached (50%)"

    # Check assignment risk
    if dte <= 7 and current_price > strike * 0.98:
        return "⚠️ ASSIGNMENT LIKELY - Consider rolling or accepting assignment"

    # Check if far OTM
    if abs(delta) < 0.20:
        return "💡 CONSIDER ROLLING UP - Far OTM, could sell closer strike"

    # Default
    return "✅ HOLD - Position on track"


def _fallback_targets(current_price: float, entry_price: float, position_type: str) -> Dict[str, float]:
    """Fallback targets if calculation fails"""
    if position_type.lower() == "long":
        return {
            'profit_target': round(current_price * 1.10, 2),
            'stop_loss': round(current_price * 0.95, 2)
        }
    else:
        return {
            'profit_target': round(current_price * 0.90, 2),
            'stop_loss': round(current_price * 1.05, 2)
        }
