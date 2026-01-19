"""
Portfolio Greeks Calculator - Calculate aggregate Greeks across all positions
"""

from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class PositionGreeks:
    """Represents Greeks for a single position"""
    symbol: str
    quantity: int
    delta: float
    gamma: float
    theta: float
    vega: float
    underlying_price: float = 0.0
    strike: float = 0.0
    expiration: str = ""


def calculate_portfolio_greeks(positions: List[PositionGreeks]) -> Dict[str, Any]:
    """
    Calculate aggregate portfolio Greeks from individual positions.

    Args:
        positions: List of PositionGreeks objects

    Returns:
        Dictionary with aggregate Greeks and risk metrics
    """
    if not positions:
        return {
            'total_delta': 0.0,
            'total_gamma': 0.0,
            'total_theta': 0.0,
            'total_vega': 0.0,
            'position_count': 0,
            'net_directional_exposure': 'NEUTRAL',
            'daily_theta_income': 0.0,
            'vega_risk_score': 'LOW',
            'alerts': []
        }

    # Sum up all Greeks (adjusted for quantity)
    total_delta = sum(pos.delta * pos.quantity for pos in positions)
    total_gamma = sum(pos.gamma * pos.quantity for pos in positions)
    total_theta = sum(pos.theta * pos.quantity for pos in positions)
    total_vega = sum(pos.vega * pos.quantity for pos in positions)

    # Daily theta income (theta is per day, multiply by 100 for contracts)
    daily_theta_income = total_theta * 100

    # Determine directional exposure
    if total_delta > 0.5:
        net_directional_exposure = 'BULLISH'
    elif total_delta < -0.5:
        net_directional_exposure = 'BEARISH'
    else:
        net_directional_exposure = 'NEUTRAL'

    # Vega risk score
    abs_vega = abs(total_vega)
    if abs_vega > 15:
        vega_risk_score = 'HIGH'
    elif abs_vega > 8:
        vega_risk_score = 'MEDIUM'
    else:
        vega_risk_score = 'LOW'

    # Generate alerts
    alerts = []
    if abs(total_delta) > 1.0:
        alerts.append(f"⚠️ High directional exposure: Delta = {total_delta:.2f}")
    if abs_vega > 15:
        alerts.append(f"⚠️ High volatility risk: Vega = {total_vega:.2f}")
    if total_theta < -50:
        alerts.append(f"⚠️ High time decay: Theta = {total_theta:.2f}")

    return {
        'total_delta': round(total_delta, 4),
        'total_gamma': round(total_gamma, 4),
        'total_theta': round(total_theta, 4),
        'total_vega': round(total_vega, 4),
        'position_count': len(positions),
        'net_directional_exposure': net_directional_exposure,
        'daily_theta_income': round(daily_theta_income, 2),
        'vega_risk_score': vega_risk_score,
        'alerts': alerts,
        'positions': [
            {
                'symbol': pos.symbol,
                'quantity': pos.quantity,
                'delta': round(pos.delta, 4),
                'theta': round(pos.theta, 4),
                'vega': round(pos.vega, 4)
            }
            for pos in positions
        ]
    }
