"""
Position Sizing - Kelly Criterion and risk-adjusted position sizing
Calculates optimal position sizes based on multiple risk factors
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

from typing import Dict, Any
import logging
from helper_functions import safe_float, safe_int

logger = logging.getLogger(__name__)


def calculate_position_size(
    account_balance: float,
    risk_per_trade: float = 0.02,
    stop_loss_percent: float = 0.05,
    underlying_price: float = 0,
    **kwargs
) -> Dict[str, Any]:
    """
    Calculate position sizing based on risk management principles

    Args:
        account_balance: Total account balance
        risk_per_trade: Max risk per trade as decimal (default 0.02 = 2%)
        stop_loss_percent: Stop loss as percentage (default 0.05 = 5%)
        underlying_price: Current price of underlying
        **kwargs: Additional parameters:
            - win_rate: Historical win rate (0-1) for Kelly Criterion
            - avg_win: Average winning trade amount
            - avg_loss: Average losing trade amount
            - prob_profit: Probability of profit from Grok (0-100)
            - strategy: 'aggressive', 'moderate', 'conservative'
            - current_positions: Number of current open positions
            - max_positions: Maximum allowed positions

    Returns:
        Dictionary with sizing recommendations
    """
    try:
        if not account_balance or account_balance <= 0:
            return _empty_position_size()

        # Extract optional parameters
        win_rate = safe_float(kwargs.get('win_rate', 0.65))  # Default 65% win rate
        avg_win = safe_float(kwargs.get('avg_win', 0))
        avg_loss = safe_float(kwargs.get('avg_loss', 0))
        prob_profit = safe_float(kwargs.get('prob_profit', 70)) / 100  # Convert to decimal
        strategy = kwargs.get('strategy', 'moderate')
        current_positions = safe_int(kwargs.get('current_positions', 0))
        max_positions = safe_int(kwargs.get('max_positions', 10))

        # Calculate Kelly Criterion if we have win/loss data
        kelly_fraction = calculate_kelly_criterion(
            win_rate=win_rate if win_rate > 0 else prob_profit,
            avg_win=avg_win,
            avg_loss=avg_loss,
            risk_reward_ratio=kwargs.get('risk_reward_ratio', 0)
        )

        # Adjust Kelly based on strategy
        if strategy == 'aggressive':
            kelly_multiplier = 1.0  # Full Kelly
        elif strategy == 'conservative':
            kelly_multiplier = 0.25  # Quarter Kelly
        else:  # moderate
            kelly_multiplier = 0.5  # Half Kelly (recommended)

        adjusted_kelly = kelly_fraction * kelly_multiplier

        # Calculate risk amount
        # Use the smaller of: fixed risk per trade or Kelly recommendation
        kelly_risk = account_balance * adjusted_kelly
        fixed_risk = account_balance * risk_per_trade
        risk_amount = min(kelly_risk, fixed_risk)

        # Calculate position size based on stop loss
        if underlying_price > 0 and stop_loss_percent > 0:
            max_loss_per_share = underlying_price * stop_loss_percent
            shares = risk_amount / max_loss_per_share if max_loss_per_share > 0 else 0

            # For options (100 shares per contract)
            contracts = max(1, int(shares / 100))
        else:
            shares = 0
            contracts = 0

        # Portfolio heat adjustment
        # Reduce size if approaching max positions
        if max_positions > 0:
            position_utilization = current_positions / max_positions
            if position_utilization > 0.80:  # 80%+ utilized
                heat_adjustment = 0.5  # Reduce size by 50%
                contracts = max(1, int(contracts * heat_adjustment))

        # Never exceed max risk per position (5% of account)
        max_single_position_risk = account_balance * 0.05
        if risk_amount > max_single_position_risk:
            risk_amount = max_single_position_risk
            contracts = max(1, int(contracts * (max_single_position_risk / risk_amount)))

        # Calculate actual dollar amounts
        position_value = contracts * underlying_price * 100 if underlying_price > 0 else 0

        return {
            'recommended_contracts': contracts,
            'recommended_shares': int(shares),
            'position_value': round(position_value, 2),
            'risk_amount': round(risk_amount, 2),
            'risk_percent': round((risk_amount / account_balance * 100), 2),
            'kelly_fraction': round(kelly_fraction, 4),
            'adjusted_kelly': round(adjusted_kelly, 4),
            'strategy': strategy,
            'kelly_recommendation': round(kelly_risk, 2),
            'fixed_risk_recommendation': round(fixed_risk, 2),
            'sizing_method': 'Kelly Criterion' if kelly_risk < fixed_risk else 'Fixed Risk',
            'max_loss_per_contract': round(underlying_price * stop_loss_percent * 100, 2) if underlying_price > 0 else 0
        }

    except Exception as e:
        logger.error(f"Error calculating position size: {e}")
        return _empty_position_size()


def calculate_kelly_criterion(
    win_rate: float,
    avg_win: float = 0,
    avg_loss: float = 0,
    risk_reward_ratio: float = 0
) -> float:
    """
    Calculate Kelly Criterion for optimal position sizing

    Kelly Formula: f = (bp - q) / b
    Where:
        f = fraction of capital to wager
        b = odds received on wager (risk/reward ratio)
        p = probability of winning (win rate)
        q = probability of losing (1 - p)

    Args:
        win_rate: Historical or expected win rate (0-1)
        avg_win: Average winning trade amount
        avg_loss: Average losing trade amount (positive number)
        risk_reward_ratio: Risk/reward ratio (if avg_win/avg_loss not provided)

    Returns:
        Kelly fraction (0-1), capped at 0.25 for safety
    """
    try:
        # Ensure win_rate is valid
        if win_rate <= 0 or win_rate >= 1:
            return 0.02  # Default to 2% if invalid

        # Calculate risk/reward ratio
        if avg_win > 0 and avg_loss > 0:
            b = avg_win / avg_loss
        elif risk_reward_ratio > 0:
            b = risk_reward_ratio
        else:
            b = 2.0  # Default assumption: 2:1 risk/reward

        # Kelly formula
        p = win_rate
        q = 1 - p

        kelly = (b * p - q) / b

        # Safety caps
        # Kelly can suggest very large positions if win rate is high
        # Cap at 25% max (conservative)
        kelly = max(0, min(kelly, 0.25))

        return kelly

    except Exception as e:
        logger.error(f"Error calculating Kelly Criterion: {e}")
        return 0.02  # Safe default


def calculate_portfolio_heat(
    total_capital: float,
    open_positions: list,
    max_heat: float = 0.20
) -> Dict[str, Any]:
    """
    Calculate current portfolio heat (total capital at risk)

    Args:
        total_capital: Total account capital
        open_positions: List of open positions with 'Max Loss' field
        max_heat: Maximum allowed portfolio heat (default 20%)

    Returns:
        Dictionary with heat analysis
    """
    try:
        total_risk = sum(abs(safe_float(pos.get('Max Loss', 0))) for pos in open_positions)
        current_heat = (total_risk / total_capital) if total_capital > 0 else 0

        remaining_capacity = max_heat - current_heat
        can_add_positions = remaining_capacity > 0

        # Calculate max size for new position
        max_new_position_risk = total_capital * min(remaining_capacity, 0.05)  # Max 5% per position

        return {
            'total_risk': round(total_risk, 2),
            'current_heat': round(current_heat, 4),
            'current_heat_percent': round(current_heat * 100, 2),
            'max_heat_percent': max_heat * 100,
            'remaining_capacity': round(remaining_capacity * 100, 2),
            'can_add_positions': can_add_positions,
            'max_new_position_risk': round(max_new_position_risk, 2),
            'status': _get_heat_status(current_heat, max_heat)
        }

    except Exception as e:
        logger.error(f"Error calculating portfolio heat: {e}")
        return {
            'total_risk': 0,
            'current_heat': 0,
            'can_add_positions': True,
            'status': 'ERROR'
        }


def _get_heat_status(current_heat: float, max_heat: float) -> str:
    """Get heat status description"""
    utilization = current_heat / max_heat if max_heat > 0 else 0

    if utilization > 1.0:
        return 'CRITICAL - Exceeds limit'
    elif utilization > 0.90:
        return 'VERY HIGH - Approaching limit'
    elif utilization > 0.75:
        return 'HIGH - Limited capacity'
    elif utilization > 0.50:
        return 'MODERATE - Good capacity'
    elif utilization > 0.25:
        return 'LOW - Plenty of capacity'
    else:
        return 'VERY LOW - Large capacity'


def calculate_sector_limits(
    total_capital: float,
    positions_by_sector: Dict[str, list],
    max_sector_concentration: float = 0.30
) -> Dict[str, Any]:
    """
    Calculate sector concentration limits

    Args:
        total_capital: Total account capital
        positions_by_sector: Dict mapping sector name to list of positions
        max_sector_concentration: Max % of capital in single sector (default 30%)

    Returns:
        Sector analysis with warnings
    """
    try:
        sector_analysis = {}

        for sector, positions in positions_by_sector.items():
            sector_risk = sum(abs(safe_float(p.get('Max Loss', 0))) for p in positions)
            sector_concentration = (sector_risk / total_capital) if total_capital > 0 else 0

            sector_analysis[sector] = {
                'position_count': len(positions),
                'total_risk': round(sector_risk, 2),
                'concentration': round(sector_concentration * 100, 2),
                'is_overconcentrated': sector_concentration > max_sector_concentration,
                'max_concentration': max_sector_concentration * 100
            }

        return sector_analysis

    except Exception as e:
        logger.error(f"Error calculating sector limits: {e}")
        return {}


def _empty_position_size() -> Dict[str, Any]:
    """Return empty position size template"""
    return {
        'recommended_contracts': 0,
        'recommended_shares': 0,
        'position_value': 0,
        'risk_amount': 0,
        'risk_percent': 0,
        'kelly_fraction': 0,
        'adjusted_kelly': 0,
        'strategy': 'N/A',
        'kelly_recommendation': 0,
        'fixed_risk_recommendation': 0,
        'sizing_method': 'N/A',
        'max_loss_per_contract': 0
    }
