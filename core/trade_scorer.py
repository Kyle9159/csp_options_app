"""
Trade Scorer - Comprehensive scoring system for options trades
Scores trades on a 0-100 scale based on multiple factors
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
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from helper_functions import safe_float, safe_int

logger = logging.getLogger(__name__)


class TradeScorer:
    """
    Comprehensive trade scoring engine
    Evaluates trades across multiple dimensions and provides actionable insights
    """

    def __init__(self):
        self.weights = {
            'greeks': 0.25,      # Delta, theta, vega quality
            'risk': 0.30,        # Risk/reward, max loss, probability
            'profitability': 0.25,  # Current P/L, ROI, win rate
            'management': 0.20   # Time management, adjustment needs
        }

    def score_trade(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Score a trade comprehensively

        Args:
            trade: Dictionary with trade details (symbol, greeks, P/L, etc.)

        Returns:
            Dictionary with overall score, component scores, and recommendations
        """
        try:
            # Calculate component scores
            greeks_score = self._score_greeks(trade)
            risk_score = self._score_risk(trade)
            profitability_score = self._score_profitability(trade)
            management_score = self._score_management(trade)

            # Calculate weighted overall score
            overall_score = (
                greeks_score * self.weights['greeks'] +
                risk_score * self.weights['risk'] +
                profitability_score * self.weights['profitability'] +
                management_score * self.weights['management']
            )

            # Generate recommendation
            recommendation = self._generate_recommendation(
                overall_score, greeks_score, risk_score,
                profitability_score, management_score, trade
            )

            # Generate action items
            actions = self._generate_actions(trade, overall_score)

            return {
                'overall_score': round(overall_score, 2),
                'component_scores': {
                    'greeks': round(greeks_score, 2),
                    'risk': round(risk_score, 2),
                    'profitability': round(profitability_score, 2),
                    'management': round(management_score, 2)
                },
                'grade': self._get_grade(overall_score),
                'recommendation': recommendation,
                'actions': actions,
                'alerts': self._generate_alerts(trade, overall_score)
            }

        except Exception as e:
            logger.error(f"Error scoring trade {trade.get('Symbol', 'Unknown')}: {e}")
            return {
                'overall_score': 50.0,
                'component_scores': {'greeks': 50, 'risk': 50, 'profitability': 50, 'management': 50},
                'grade': 'C',
                'recommendation': 'MONITOR - Error calculating score',
                'actions': [],
                'alerts': [f"⚠️ Scoring error: {e}"]
            }

    def _score_greeks(self, trade: Dict[str, Any]) -> float:
        """Score based on Greeks quality (0-100)"""
        score = 50.0  # Start neutral

        # Delta scoring (want ~0.30 for CSPs)
        delta = abs(safe_float(trade.get('Delta', 0)))
        if 0.25 <= delta <= 0.35:
            score += 20  # Sweet spot
        elif 0.20 <= delta <= 0.40:
            score += 10  # Acceptable
        elif delta < 0.15:
            score -= 10  # Too far OTM
        elif delta > 0.50:
            score -= 20  # Too risky (ITM or close)

        # Theta scoring (higher is better for sellers)
        theta = abs(safe_float(trade.get('Theta', 0)))
        if theta > 0.05:
            score += 15  # Excellent theta decay
        elif theta > 0.03:
            score += 10  # Good theta
        elif theta > 0.01:
            score += 5   # Moderate theta
        else:
            score -= 5   # Low theta (not earning much)

        # Vega scoring (lower is better for stability)
        vega = abs(safe_float(trade.get('Vega', 0)))
        if vega < 0.10:
            score += 15  # Low vol risk
        elif vega < 0.20:
            score += 10  # Moderate vol risk
        elif vega > 0.40:
            score -= 10  # High vol risk

        return max(0, min(100, score))

    def _score_risk(self, trade: Dict[str, Any]) -> float:
        """Score based on risk metrics (0-100)"""
        score = 50.0

        # Max loss as % of capital
        max_loss = abs(safe_float(trade.get('Max Loss', 0)))
        premium = safe_float(trade.get('Premium Collected', 0))

        if max_loss > 0 and premium > 0:
            risk_reward = premium / max_loss
            if risk_reward > 0.05:  # >5% return on risk
                score += 20
            elif risk_reward > 0.03:  # >3% return
                score += 15
            elif risk_reward > 0.02:  # >2% return
                score += 10
            elif risk_reward < 0.01:  # <1% return
                score -= 15

        # Probability of profit (if available from Grok)
        prob_profit = safe_float(trade.get('grok_profit_prob', 0))
        if prob_profit > 0:
            if prob_profit >= 75:
                score += 20
            elif prob_profit >= 65:
                score += 15
            elif prob_profit >= 55:
                score += 10
            elif prob_profit < 40:
                score -= 15

        # Distance from current price (safety margin)
        current_price = safe_float(trade.get('Current Price', 0))
        strike = safe_float(trade.get('Strike', 0))

        if current_price > 0 and strike > 0:
            distance_pct = ((current_price - strike) / current_price) * 100
            if distance_pct > 15:  # >15% OTM
                score += 15
            elif distance_pct > 10:  # >10% OTM
                score += 10
            elif distance_pct > 5:   # >5% OTM
                score += 5
            elif distance_pct < 0:   # ITM
                score -= 20

        return max(0, min(100, score))

    def _score_profitability(self, trade: Dict[str, Any]) -> float:
        """Score based on profitability (0-100)"""
        score = 50.0

        # Current P/L percentage
        pl_pct = safe_float(trade.get('Current P/L %', 0))

        if pl_pct > 0:
            # Winning trade
            if pl_pct >= 50:    # Target hit
                score += 30
            elif pl_pct >= 25:  # Partial profit
                score += 20
            elif pl_pct >= 10:  # Small profit
                score += 10
        else:
            # Losing trade
            if pl_pct <= -50:   # Major loss
                score -= 30
            elif pl_pct <= -25: # Significant loss
                score -= 20
            elif pl_pct <= -10: # Minor loss
                score -= 10

        # Premium quality (higher premiums = better trades)
        premium = safe_float(trade.get('Premium Collected', 0))
        if premium > 500:
            score += 10
        elif premium > 300:
            score += 5
        elif premium < 100:
            score -= 5

        # ROI on capital at risk
        max_loss = abs(safe_float(trade.get('Max Loss', 0)))
        if max_loss > 0 and premium > 0:
            roi = (premium / max_loss) * 100
            if roi > 5:
                score += 10
            elif roi > 3:
                score += 5

        return max(0, min(100, score))

    def _score_management(self, trade: Dict[str, Any]) -> float:
        """Score based on trade management needs (0-100)"""
        score = 50.0

        # Days to expiration
        dte = safe_int(trade.get('DTE', 0))

        if dte > 30:
            score += 15  # Plenty of time
        elif dte > 21:
            score += 10  # Good time
        elif dte > 14:
            score += 5   # Moderate time
        elif dte > 7:
            score -= 5   # Getting close
        elif dte > 3:
            score -= 15  # Very close, needs attention
        else:
            score -= 25  # Critical - expiring soon

        # Theta efficiency (theta/DTE ratio)
        theta = abs(safe_float(trade.get('Theta', 0)))
        if dte > 0:
            theta_efficiency = theta / dte
            if theta_efficiency > 0.003:
                score += 15  # Highly efficient decay
            elif theta_efficiency > 0.002:
                score += 10
            elif theta_efficiency > 0.001:
                score += 5

        # Adjustment needs (based on delta changes)
        delta = abs(safe_float(trade.get('Delta', 0)))
        if delta > 0.50:  # ITM or very close
            score -= 20  # Needs immediate attention
        elif delta > 0.40:
            score -= 10  # Watch closely

        # Time-based scoring
        pl_pct = safe_float(trade.get('Current P/L %', 0))
        if pl_pct >= 50 and dte > 7:
            score += 15  # Can close early for profit

        return max(0, min(100, score))

    def _get_grade(self, score: float) -> str:
        """Convert score to letter grade"""
        if score >= 90:
            return 'A+'
        elif score >= 85:
            return 'A'
        elif score >= 80:
            return 'A-'
        elif score >= 75:
            return 'B+'
        elif score >= 70:
            return 'B'
        elif score >= 65:
            return 'B-'
        elif score >= 60:
            return 'C+'
        elif score >= 55:
            return 'C'
        elif score >= 50:
            return 'C-'
        elif score >= 45:
            return 'D+'
        elif score >= 40:
            return 'D'
        else:
            return 'F'

    def _generate_recommendation(self, overall_score: float, greeks: float,
                                 risk: float, profit: float, mgmt: float,
                                 trade: Dict[str, Any]) -> str:
        """Generate action recommendation"""
        pl_pct = safe_float(trade.get('Current P/L %', 0))
        dte = safe_int(trade.get('DTE', 0))
        delta = abs(safe_float(trade.get('Delta', 0)))

        # Critical situations first
        if delta > 0.50:
            return "🚨 ADJUST NOW - Position too close to strike"

        if dte <= 3 and pl_pct < -10:
            return "🚨 CLOSE NOW - Near expiration with loss"

        if pl_pct >= 50:
            return "✅ CLOSE FOR PROFIT - Target reached"

        if pl_pct <= -50:
            return "⚠️ CONSIDER CLOSING - Significant loss"

        # Based on overall score
        if overall_score >= 80:
            return "✅ EXCELLENT - Hold and monitor"
        elif overall_score >= 70:
            return "👍 GOOD - Continue as planned"
        elif overall_score >= 60:
            return "⚠️ ACCEPTABLE - Watch closely"
        elif overall_score >= 50:
            return "⚠️ MARGINAL - Consider adjustments"
        else:
            return "🚨 POOR - Review exit strategy"

    def _generate_actions(self, trade: Dict[str, Any], score: float) -> list:
        """Generate specific action items"""
        actions = []

        pl_pct = safe_float(trade.get('Current P/L %', 0))
        dte = safe_int(trade.get('DTE', 0))
        delta = abs(safe_float(trade.get('Delta', 0)))

        # Profit taking
        if pl_pct >= 50:
            actions.append("Consider closing at 50% profit target")

        # Risk management
        if delta > 0.40:
            actions.append("Delta elevated - consider rolling down/out")

        # Time management
        if dte <= 7 and pl_pct > 25:
            actions.append("Near expiration - consider early close for profit")

        if dte <= 3:
            actions.append("⚠️ Expiration approaching - close or prepare for assignment")

        # Loss management
        if pl_pct <= -25:
            actions.append("Stop loss approaching - review exit options")

        if pl_pct <= -50:
            actions.append("🚨 Stop loss breached - consider immediate exit")

        # Score-based actions
        if score < 50:
            actions.append("Low score - reassess position thesis")

        return actions

    def _generate_alerts(self, trade: Dict[str, Any], score: float) -> list:
        """Generate warning alerts"""
        alerts = []

        delta = abs(safe_float(trade.get('Delta', 0)))
        pl_pct = safe_float(trade.get('Current P/L %', 0))
        dte = safe_int(trade.get('DTE', 0))

        if delta > 0.50:
            alerts.append("⚠️ HIGH DELTA - Position at risk")

        if pl_pct <= -50:
            alerts.append("🚨 STOP LOSS BREACHED")

        if dte <= 3:
            alerts.append("⏰ EXPIRING SOON")

        if score < 40:
            alerts.append("📉 LOW SCORE - Position quality degraded")

        return alerts
