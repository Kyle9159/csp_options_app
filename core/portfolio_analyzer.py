"""
Portfolio Analyzer - Performance tracking and portfolio-level analytics
Provides comprehensive portfolio insights and recommendations
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
from datetime import datetime, timedelta
from helper_functions import safe_float, safe_int
import numpy as np

logger = logging.getLogger(__name__)


class PortfolioAnalyzer:
    """
    Portfolio-level performance analysis and insights
    """

    def __init__(self, config):
        """
        Initialize portfolio analyzer

        Args:
            config: Configuration object
        """
        self.config = config

    def analyze_portfolio(self, trades: List[Dict[str, Any]],
                         total_capital: float) -> Dict[str, Any]:
        """
        Generate comprehensive portfolio analysis

        Args:
            trades: List of open trades
            total_capital: Total account capital

        Returns:
            Complete portfolio analysis with metrics and insights
        """
        try:
            if not trades:
                return self._empty_portfolio_analysis()

            # Calculate overview metrics
            overview = self._calculate_overview(trades, total_capital)

            # Calculate performance metrics
            performance = self._calculate_performance(trades)

            # Calculate exposure analysis
            exposure = self._calculate_exposure(trades, total_capital)

            # Calculate Greeks summary
            greeks_summary = self._calculate_greeks_summary(trades)

            # Calculate win rate and statistics
            statistics = self._calculate_statistics(trades)

            # Generate recommendations
            recommendations = self._generate_recommendations(
                overview, performance, exposure, greeks_summary
            )

            # Calculate trend analysis
            trends = self._calculate_trends(trades)

            return {
                'overview': overview,
                'performance': performance,
                'exposure': exposure,
                'greeks_summary': greeks_summary,
                'statistics': statistics,
                'recommendations': recommendations,
                'trends': trends,
                'health_score': self._calculate_health_score(performance, exposure, statistics)
            }

        except Exception as e:
            logger.error(f"Error analyzing portfolio: {e}")
            return self._empty_portfolio_analysis()

    def _calculate_overview(self, trades: List[Dict[str, Any]],
                           total_capital: float) -> Dict[str, Any]:
        """Calculate high-level overview metrics"""
        total_positions = len(trades)

        total_exposure = sum(abs(safe_float(t.get('Max Loss', 0))) for t in trades)
        total_premium_collected = sum(safe_float(t.get('Premium Collected', 0)) for t in trades)
        total_current_pl = sum(safe_float(t.get('Current P/L $', 0)) for t in trades)

        # Calculate unrealized P/L
        unrealized_pl = sum(
            safe_float(t.get('Current P/L $', 0))
            for t in trades
            if safe_float(t.get('Current P/L $', 0)) != 0
        )

        # Calculate average trade score
        trade_scores = [safe_float(t.get('_trade_score', 50)) for t in trades]
        avg_trade_score = np.mean(trade_scores) if trade_scores else 50.0

        # Calculate capital utilization
        capital_utilization = (total_exposure / total_capital * 100) if total_capital > 0 else 0

        return {
            'total_positions': total_positions,
            'total_exposure': round(total_exposure, 2),
            'total_premium_collected': round(total_premium_collected, 2),
            'total_current_pl': round(total_current_pl, 2),
            'unrealized_pl': round(unrealized_pl, 2),
            'avg_trade_score': round(avg_trade_score, 2),
            'capital_utilization': round(capital_utilization, 2),
            'available_capital': round(total_capital - total_exposure, 2)
        }

    def _calculate_performance(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate performance metrics"""
        # Overall P/L
        total_pl = sum(safe_float(t.get('Current P/L $', 0)) for t in trades)
        total_premium = sum(safe_float(t.get('Premium Collected', 0)) for t in trades)

        # ROI calculation
        total_risk = sum(abs(safe_float(t.get('Max Loss', 0))) for t in trades)
        roi = (total_pl / total_risk * 100) if total_risk > 0 else 0

        # Winners vs Losers
        winners = [t for t in trades if safe_float(t.get('Current P/L $', 0)) > 0]
        losers = [t for t in trades if safe_float(t.get('Current P/L $', 0)) < 0]

        win_count = len(winners)
        loss_count = len(losers)
        total_count = win_count + loss_count

        win_rate = (win_count / total_count * 100) if total_count > 0 else 0

        # Average win/loss
        avg_win = np.mean([safe_float(t.get('Current P/L $', 0)) for t in winners]) if winners else 0
        avg_loss = np.mean([safe_float(t.get('Current P/L $', 0)) for t in losers]) if losers else 0

        # Profit factor
        total_wins = sum(safe_float(t.get('Current P/L $', 0)) for t in winners)
        total_losses = abs(sum(safe_float(t.get('Current P/L $', 0)) for t in losers))
        profit_factor = (total_wins / total_losses) if total_losses > 0 else 0

        # Best and worst trades
        best_trade = max(trades, key=lambda t: safe_float(t.get('Current P/L $', 0))) if trades else None
        worst_trade = min(trades, key=lambda t: safe_float(t.get('Current P/L $', 0))) if trades else None

        return {
            'total_pl': round(total_pl, 2),
            'total_premium': round(total_premium, 2),
            'roi_percent': round(roi, 2),
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': round(win_rate, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'best_trade': {
                'symbol': best_trade.get('Symbol', 'N/A'),
                'pl': safe_float(best_trade.get('Current P/L $', 0))
            } if best_trade else None,
            'worst_trade': {
                'symbol': worst_trade.get('Symbol', 'N/A'),
                'pl': safe_float(worst_trade.get('Current P/L $', 0))
            } if worst_trade else None
        }

    def _calculate_exposure(self, trades: List[Dict[str, Any]],
                           total_capital: float) -> Dict[str, Any]:
        """Calculate exposure breakdown by various dimensions"""
        # By symbol
        symbol_exposure = {}
        for trade in trades:
            symbol = trade.get('Symbol', 'Unknown')
            exposure = abs(safe_float(trade.get('Max Loss', 0)))
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0) + exposure

        # Top 5 exposures
        top_exposures = sorted(
            [{'symbol': k, 'exposure': v} for k, v in symbol_exposure.items()],
            key=lambda x: x['exposure'],
            reverse=True
        )[:5]

        # By DTE buckets
        dte_buckets = {
            '0-7 days': 0,
            '8-14 days': 0,
            '15-21 days': 0,
            '22-30 days': 0,
            '30+ days': 0
        }

        for trade in trades:
            dte = safe_int(trade.get('DTE', 0))
            exposure = abs(safe_float(trade.get('Max Loss', 0)))

            if dte <= 7:
                dte_buckets['0-7 days'] += exposure
            elif dte <= 14:
                dte_buckets['8-14 days'] += exposure
            elif dte <= 21:
                dte_buckets['15-21 days'] += exposure
            elif dte <= 30:
                dte_buckets['22-30 days'] += exposure
            else:
                dte_buckets['30+ days'] += exposure

        # By P/L status
        winning_exposure = sum(
            abs(safe_float(t.get('Max Loss', 0)))
            for t in trades
            if safe_float(t.get('Current P/L $', 0)) > 0
        )
        losing_exposure = sum(
            abs(safe_float(t.get('Max Loss', 0)))
            for t in trades
            if safe_float(t.get('Current P/L $', 0)) < 0
        )

        return {
            'top_exposures': top_exposures,
            'dte_buckets': dte_buckets,
            'winning_exposure': round(winning_exposure, 2),
            'losing_exposure': round(losing_exposure, 2),
            'total_symbols': len(symbol_exposure)
        }

    def _calculate_greeks_summary(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate aggregate Greeks across portfolio"""
        total_delta = sum(safe_float(t.get('Delta', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_gamma = sum(safe_float(t.get('Gamma', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_theta = sum(safe_float(t.get('Theta', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)
        total_vega = sum(safe_float(t.get('Vega', 0)) * safe_int(t.get('Quantity', 1)) for t in trades)

        # Daily theta income (multiply by 100 for contract multiplier)
        daily_theta_income = total_theta * 100

        # Directional bias
        if total_delta > 0.5:
            bias = 'BULLISH'
        elif total_delta < -0.5:
            bias = 'BEARISH'
        else:
            bias = 'NEUTRAL'

        return {
            'total_delta': round(total_delta, 4),
            'total_gamma': round(total_gamma, 4),
            'total_theta': round(total_theta, 4),
            'total_vega': round(total_vega, 4),
            'daily_theta_income': round(daily_theta_income, 2),
            'directional_bias': bias
        }

    def _calculate_statistics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate statistical metrics"""
        # Collect P/L percentages
        pl_percentages = [safe_float(t.get('Current P/L %', 0)) for t in trades]

        if not pl_percentages:
            return {
                'avg_pl_percent': 0,
                'median_pl_percent': 0,
                'std_dev_pl': 0,
                'max_drawdown_percent': 0
            }

        avg_pl = np.mean(pl_percentages)
        median_pl = np.median(pl_percentages)
        std_dev = np.std(pl_percentages)

        # Max drawdown (worst loss %)
        max_drawdown = min(pl_percentages) if pl_percentages else 0

        return {
            'avg_pl_percent': round(avg_pl, 2),
            'median_pl_percent': round(median_pl, 2),
            'std_dev_pl': round(std_dev, 2),
            'max_drawdown_percent': round(max_drawdown, 2)
        }

    def _calculate_trends(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Identify portfolio trends"""
        # Positions approaching expiration
        expiring_soon = [t for t in trades if safe_int(t.get('DTE', 99)) <= 7]

        # Positions needing attention (high delta or losing)
        needs_attention = [
            t for t in trades
            if abs(safe_float(t.get('Delta', 0))) > 0.40
            or safe_float(t.get('Current P/L %', 0)) < -25
        ]

        # Positions ready to close (at profit target)
        ready_to_close = [
            t for t in trades
            if safe_float(t.get('Current P/L %', 0)) >= 50
        ]

        # High risk positions
        high_risk = [
            t for t in trades
            if abs(safe_float(t.get('Max Loss', 0))) > 5000
        ]

        return {
            'expiring_soon_count': len(expiring_soon),
            'needs_attention_count': len(needs_attention),
            'ready_to_close_count': len(ready_to_close),
            'high_risk_count': len(high_risk),
            'expiring_soon': [t.get('Symbol') for t in expiring_soon],
            'needs_attention': [t.get('Symbol') for t in needs_attention],
            'ready_to_close': [t.get('Symbol') for t in ready_to_close]
        }

    def _generate_recommendations(self, overview: Dict, performance: Dict,
                                  exposure: Dict, greeks: Dict) -> List[str]:
        """Generate actionable portfolio recommendations"""
        recommendations = []

        # Capital utilization
        util = overview.get('capital_utilization', 0)
        if util > 80:
            recommendations.append("⚠️ High capital utilization - consider closing profitable positions")
        elif util < 30:
            recommendations.append("💡 Low capital utilization - room for new positions")

        # Win rate
        win_rate = performance.get('win_rate', 0)
        if win_rate < 50:
            recommendations.append("📉 Win rate below 50% - review position selection criteria")
        elif win_rate > 75:
            recommendations.append("✅ Excellent win rate - maintain current strategy")

        # Profit factor
        profit_factor = performance.get('profit_factor', 0)
        if profit_factor < 1.0:
            recommendations.append("🚨 Profit factor <1.0 - losses exceeding wins")
        elif profit_factor > 2.0:
            recommendations.append("✅ Strong profit factor - strategy performing well")

        # Greeks
        abs_delta = abs(greeks.get('total_delta', 0))
        if abs_delta > 1.0:
            recommendations.append(f"⚠️ High directional exposure (Delta: {greeks.get('total_delta', 0):.2f})")

        daily_theta = greeks.get('daily_theta_income', 0)
        if daily_theta < 0:
            recommendations.append(f"💰 Collecting ${abs(daily_theta):.2f} in daily theta income")

        # Exposure concentration
        top_exp = exposure.get('top_exposures', [])
        if top_exp and len(top_exp) > 0:
            top_symbol = top_exp[0]
            if len(exposure.get('top_exposures', [])) < 5:
                recommendations.append("💡 Consider diversifying across more symbols")

        return recommendations

    def _calculate_health_score(self, performance: Dict, exposure: Dict,
                                statistics: Dict) -> Dict[str, Any]:
        """Calculate overall portfolio health score (0-100)"""
        score = 50.0  # Start neutral

        # Win rate contribution
        win_rate = performance.get('win_rate', 50)
        if win_rate >= 70:
            score += 20
        elif win_rate >= 60:
            score += 15
        elif win_rate >= 50:
            score += 10
        elif win_rate < 40:
            score -= 15

        # Profit factor contribution
        profit_factor = performance.get('profit_factor', 1.0)
        if profit_factor > 2.0:
            score += 15
        elif profit_factor > 1.5:
            score += 10
        elif profit_factor > 1.0:
            score += 5
        elif profit_factor < 1.0:
            score -= 20

        # ROI contribution
        roi = performance.get('roi_percent', 0)
        if roi > 10:
            score += 10
        elif roi > 5:
            score += 5
        elif roi < -5:
            score -= 10

        # Drawdown penalty
        max_dd = statistics.get('max_drawdown_percent', 0)
        if max_dd < -50:
            score -= 20
        elif max_dd < -25:
            score -= 10

        score = max(0, min(100, score))

        # Determine health rating
        if score >= 80:
            rating = 'EXCELLENT'
        elif score >= 70:
            rating = 'GOOD'
        elif score >= 60:
            rating = 'FAIR'
        elif score >= 50:
            rating = 'NEEDS IMPROVEMENT'
        else:
            rating = 'POOR'

        return {
            'score': round(score, 2),
            'rating': rating
        }

    def _empty_portfolio_analysis(self) -> Dict[str, Any]:
        """Return empty analysis template"""
        return {
            'overview': {
                'total_positions': 0,
                'total_exposure': 0,
                'total_premium_collected': 0,
                'total_current_pl': 0,
                'unrealized_pl': 0,
                'avg_trade_score': 0,
                'capital_utilization': 0,
                'available_capital': 0
            },
            'performance': {
                'total_pl': 0,
                'total_premium': 0,
                'roi_percent': 0,
                'win_count': 0,
                'loss_count': 0,
                'win_rate': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'profit_factor': 0
            },
            'exposure': {},
            'greeks_summary': {},
            'statistics': {},
            'recommendations': ['No positions to analyze'],
            'trends': {},
            'health_score': {'score': 0, 'rating': 'N/A'}
        }
