"""
Risk Calculator - Comprehensive portfolio risk analysis
Calculates portfolio heat, risk metrics, and risk-adjusted position sizing
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
from typing import Dict, List, Any, Optional
from datetime import datetime
from helper_functions import safe_float, safe_int
import numpy as np

logger = logging.getLogger(__name__)


class RiskCalculator:
    """
    Portfolio risk analysis and calculation engine
    """

    def __init__(self, config):
        """
        Initialize risk calculator with configuration

        Args:
            config: Configuration object with trading parameters
        """
        self.config = config
        self.max_portfolio_heat = 0.20  # Max 20% of capital at risk
        self.max_position_heat = 0.05   # Max 5% per position
        self.correlation_threshold = 0.70  # High correlation warning

    def calculate_portfolio_risk(self, trades: List[Dict[str, Any]],
                                 total_capital: float) -> Dict[str, Any]:
        """
        Calculate comprehensive portfolio risk metrics

        Args:
            trades: List of open trades
            total_capital: Total account capital

        Returns:
            Dictionary with risk metrics and alerts
        """
        try:
            if not trades or total_capital <= 0:
                return self._empty_risk_report()

            # Calculate total capital at risk
            total_risk = sum(abs(safe_float(t.get('Max Loss', 0))) for t in trades)

            # Calculate portfolio heat (% of capital at risk)
            portfolio_heat = (total_risk / total_capital) * 100 if total_capital > 0 else 0

            # Calculate individual position risks
            position_risks = []
            for trade in trades:
                max_loss = abs(safe_float(trade.get('Max Loss', 0)))
                position_heat = (max_loss / total_capital) * 100 if total_capital > 0 else 0
                position_risks.append({
                    'symbol': trade.get('Symbol', 'Unknown'),
                    'max_loss': max_loss,
                    'position_heat': round(position_heat, 2),
                    'risk_rating': self._get_risk_rating(position_heat)
                })

            # Calculate risk concentration
            concentration = self._calculate_concentration(position_risks, total_risk)

            # Calculate Greeks-based risk
            greeks_risk = self._calculate_greeks_risk(trades)

            # Calculate VaR (Value at Risk) - simplified 95% confidence
            var_95 = self._calculate_var(trades, total_capital, confidence=0.95)

            # Generate risk alerts
            alerts = self._generate_risk_alerts(
                portfolio_heat, position_risks, concentration, greeks_risk
            )

            # Calculate risk score (0-100, lower is riskier)
            risk_score = self._calculate_risk_score(
                portfolio_heat, concentration, greeks_risk
            )

            return {
                'total_capital': total_capital,
                'total_risk': round(total_risk, 2),
                'portfolio_heat': round(portfolio_heat, 2),
                'portfolio_heat_rating': self._get_risk_rating(portfolio_heat * 5),  # Scaled
                'available_capital': round(total_capital - total_risk, 2),
                'capital_utilization': round((total_risk / total_capital) * 100, 2),
                'position_count': len(trades),
                'position_risks': position_risks,
                'concentration': concentration,
                'greeks_risk': greeks_risk,
                'var_95': round(var_95, 2),
                'risk_score': round(risk_score, 2),
                'risk_grade': self._get_risk_grade(risk_score),
                'alerts': alerts,
                'max_new_position_size': self._calculate_max_new_position(
                    total_capital, total_risk
                )
            }

        except Exception as e:
            logger.error(f"Error calculating portfolio risk: {e}")
            return self._empty_risk_report()

    def calculate_position_risk_reward(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate risk/reward metrics for a single position

        Args:
            trade: Trade dictionary

        Returns:
            Risk/reward analysis
        """
        try:
            premium = safe_float(trade.get('Premium Collected', 0))
            max_loss = abs(safe_float(trade.get('Max Loss', 0)))
            current_pl = safe_float(trade.get('Current P/L $', 0))

            # Risk/Reward Ratio
            rr_ratio = premium / max_loss if max_loss > 0 else 0

            # Expected Value (simplified)
            prob_profit = safe_float(trade.get('grok_profit_prob', 70)) / 100
            expected_value = (premium * prob_profit) - (max_loss * (1 - prob_profit))

            # Sharpe-like ratio (return / risk)
            sharpe = (premium / max_loss) if max_loss > 0 else 0

            return {
                'premium': premium,
                'max_loss': max_loss,
                'current_pl': current_pl,
                'risk_reward_ratio': round(rr_ratio, 3),
                'expected_value': round(expected_value, 2),
                'sharpe_ratio': round(sharpe, 3),
                'prob_profit': prob_profit * 100,
                'rating': self._rate_risk_reward(rr_ratio)
            }

        except Exception as e:
            logger.error(f"Error calculating risk/reward: {e}")
            return {
                'premium': 0, 'max_loss': 0, 'current_pl': 0,
                'risk_reward_ratio': 0, 'expected_value': 0,
                'sharpe_ratio': 0, 'prob_profit': 0,
                'rating': 'Unknown'
            }

    def calculate_margin_requirements(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calculate margin requirements for all positions

        Args:
            trades: List of trades

        Returns:
            Margin analysis
        """
        try:
            total_margin = 0
            positions_detail = []

            for trade in trades:
                # For CSPs: margin = strike * 100 * quantity (simplified)
                strike = safe_float(trade.get('Strike', 0))
                quantity = safe_int(trade.get('Quantity', 1))

                position_margin = strike * 100 * quantity

                total_margin += position_margin

                positions_detail.append({
                    'symbol': trade.get('Symbol', 'Unknown'),
                    'strike': strike,
                    'quantity': quantity,
                    'margin_required': round(position_margin, 2)
                })

            return {
                'total_margin_required': round(total_margin, 2),
                'positions': positions_detail,
                'avg_margin_per_position': round(total_margin / len(trades), 2) if trades else 0
            }

        except Exception as e:
            logger.error(f"Error calculating margin: {e}")
            return {
                'total_margin_required': 0,
                'positions': [],
                'avg_margin_per_position': 0
            }

    def _calculate_concentration(self, position_risks: List[Dict], total_risk: float) -> Dict[str, Any]:
        """Calculate risk concentration metrics"""
        if not position_risks or total_risk == 0:
            return {'herfindahl_index': 0, 'top_3_concentration': 0, 'rating': 'N/A'}

        # Sort by max_loss descending
        sorted_positions = sorted(position_risks, key=lambda x: x['max_loss'], reverse=True)

        # Top 3 concentration
        top_3_risk = sum(p['max_loss'] for p in sorted_positions[:3])
        top_3_concentration = (top_3_risk / total_risk) * 100 if total_risk > 0 else 0

        # Herfindahl Index (concentration measure)
        # Sum of squared market shares
        herfindahl = sum((p['max_loss'] / total_risk) ** 2 for p in position_risks) if total_risk > 0 else 0

        # Rating
        if top_3_concentration > 80:
            rating = 'HIGHLY CONCENTRATED'
        elif top_3_concentration > 60:
            rating = 'CONCENTRATED'
        elif top_3_concentration > 40:
            rating = 'MODERATE'
        else:
            rating = 'DIVERSIFIED'

        return {
            'herfindahl_index': round(herfindahl, 3),
            'top_3_concentration': round(top_3_concentration, 2),
            'rating': rating,
            'top_positions': [
                {'symbol': p['symbol'], 'heat': p['position_heat']}
                for p in sorted_positions[:3]
            ]
        }

    def _calculate_greeks_risk(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate risk from aggregate Greeks"""
        total_delta = sum(safe_float(t.get('Delta', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_gamma = sum(safe_float(t.get('Gamma', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_vega = sum(safe_float(t.get('Vega', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_theta = sum(safe_float(t.get('Theta', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)

        # Risk ratings
        delta_risk = 'HIGH' if abs(total_delta) > 1.0 else 'MODERATE' if abs(total_delta) > 0.5 else 'LOW'
        vega_risk = 'HIGH' if abs(total_vega) > 15 else 'MODERATE' if abs(total_vega) > 8 else 'LOW'

        return {
            'total_delta': round(total_delta, 4),
            'total_gamma': round(total_gamma, 4),
            'total_vega': round(total_vega, 4),
            'total_theta': round(total_theta, 4),
            'delta_risk': delta_risk,
            'vega_risk': vega_risk,
            'directional_exposure': 'BULLISH' if total_delta > 0.5 else 'BEARISH' if total_delta < -0.5 else 'NEUTRAL'
        }

    def _calculate_var(self, trades: List[Dict[str, Any]], total_capital: float,
                       confidence: float = 0.95) -> float:
        """
        Calculate Value at Risk (VaR) - simplified Monte Carlo approach

        Args:
            trades: List of trades
            total_capital: Total capital
            confidence: Confidence level (0.95 = 95%)

        Returns:
            VaR amount in dollars
        """
        try:
            # Simplified VaR: use current P/L distribution
            pl_values = [safe_float(t.get('Current P/L $', 0)) for t in trades]

            if not pl_values:
                return 0.0

            # Use percentile method
            var_percentile = (1 - confidence) * 100
            var = np.percentile(pl_values, var_percentile)

            return abs(var)

        except Exception as e:
            logger.error(f"Error calculating VaR: {e}")
            return 0.0

    def _calculate_risk_score(self, portfolio_heat: float, concentration: Dict,
                             greeks_risk: Dict) -> float:
        """
        Calculate overall risk score (0-100, higher is safer)
        """
        score = 100.0

        # Portfolio heat penalty
        if portfolio_heat > 20:
            score -= 30
        elif portfolio_heat > 15:
            score -= 20
        elif portfolio_heat > 10:
            score -= 10

        # Concentration penalty
        if concentration.get('rating') == 'HIGHLY CONCENTRATED':
            score -= 20
        elif concentration.get('rating') == 'CONCENTRATED':
            score -= 10

        # Greeks risk penalty
        if greeks_risk.get('delta_risk') == 'HIGH':
            score -= 15
        elif greeks_risk.get('delta_risk') == 'MODERATE':
            score -= 5

        if greeks_risk.get('vega_risk') == 'HIGH':
            score -= 15
        elif greeks_risk.get('vega_risk') == 'MODERATE':
            score -= 5

        return max(0, min(100, score))

    def _get_risk_rating(self, heat: float) -> str:
        """Convert heat percentage to rating"""
        if heat > 10:
            return 'EXTREME'
        elif heat > 7:
            return 'HIGH'
        elif heat > 5:
            return 'ELEVATED'
        elif heat > 3:
            return 'MODERATE'
        else:
            return 'LOW'

    def _get_risk_grade(self, score: float) -> str:
        """Convert risk score to letter grade"""
        if score >= 90:
            return 'A (Excellent)'
        elif score >= 80:
            return 'B (Good)'
        elif score >= 70:
            return 'C (Acceptable)'
        elif score >= 60:
            return 'D (Concerning)'
        else:
            return 'F (Dangerous)'

    def _rate_risk_reward(self, rr_ratio: float) -> str:
        """Rate risk/reward ratio"""
        if rr_ratio > 0.05:
            return 'EXCELLENT'
        elif rr_ratio > 0.03:
            return 'GOOD'
        elif rr_ratio > 0.02:
            return 'ACCEPTABLE'
        elif rr_ratio > 0.01:
            return 'POOR'
        else:
            return 'VERY POOR'

    def _generate_risk_alerts(self, portfolio_heat: float, position_risks: List,
                             concentration: Dict, greeks_risk: Dict) -> List[str]:
        """Generate risk warning alerts"""
        alerts = []

        if portfolio_heat > 20:
            alerts.append("🚨 PORTFOLIO HEAT CRITICAL - Exceeds 20% limit")
        elif portfolio_heat > 15:
            alerts.append("⚠️ Portfolio heat elevated - approaching 20% limit")

        # Position-specific alerts
        high_risk_positions = [p for p in position_risks if p['position_heat'] > 5]
        if high_risk_positions:
            alerts.append(f"⚠️ {len(high_risk_positions)} position(s) exceed 5% individual risk limit")

        # Concentration alerts
        if concentration.get('rating') == 'HIGHLY CONCENTRATED':
            alerts.append("⚠️ Portfolio highly concentrated - consider diversification")

        # Greeks alerts
        if greeks_risk.get('delta_risk') == 'HIGH':
            alerts.append(f"⚠️ High directional risk - Delta: {greeks_risk.get('total_delta', 0):.2f}")

        if greeks_risk.get('vega_risk') == 'HIGH':
            alerts.append(f"⚠️ High volatility risk - Vega: {greeks_risk.get('total_vega', 0):.2f}")

        return alerts

    def _calculate_max_new_position(self, total_capital: float, current_risk: float) -> float:
        """Calculate maximum size for new position"""
        max_portfolio_risk = total_capital * self.max_portfolio_heat
        remaining_risk_capacity = max_portfolio_risk - current_risk

        # Also respect individual position limit
        max_individual_position = total_capital * self.max_position_heat

        return round(min(remaining_risk_capacity, max_individual_position), 2)

    def _empty_risk_report(self) -> Dict[str, Any]:
        """Return empty risk report template"""
        return {
            'total_capital': 0,
            'total_risk': 0,
            'portfolio_heat': 0,
            'portfolio_heat_rating': 'N/A',
            'available_capital': 0,
            'capital_utilization': 0,
            'position_count': 0,
            'position_risks': [],
            'concentration': {'herfindahl_index': 0, 'top_3_concentration': 0, 'rating': 'N/A'},
            'greeks_risk': {},
            'var_95': 0,
            'risk_score': 50,
            'risk_grade': 'N/A',
            'alerts': [],
            'max_new_position_size': 0
        }
