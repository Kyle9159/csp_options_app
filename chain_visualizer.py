"""
Chain Visualizer - Options Chain Heatmap Generation
Creates interactive Plotly visualizations for options chains
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
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

try:
    import plotly.graph_objects as go
    import plotly.express as px
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    logging.warning("Plotly not installed. Install with: pip install plotly")

from schwab_utils import get_client
from helper_functions import safe_float, safe_int
from schwab.client import Client

logger = logging.getLogger(__name__)


def fetch_option_chain_data(symbol: str, exp_date: str = None) -> pd.DataFrame:
    """
    Fetch options chain data from Schwab API

    Args:
        symbol: Stock symbol (e.g., 'SPY')
        exp_date: Optional specific expiration date (YYYY-MM-DD)

    Returns:
        DataFrame with options chain data
    """
    try:
        client = get_client()

        # Fetch full option chain
        response = client.get_option_chain(
            symbol=symbol,
            contract_type=Client.Options.ContractType.ALL,
            include_underlying_quote=True
        )

        # Parse response to DataFrame
        df = parse_chain_to_dataframe(response.json())

        # Filter by expiration if specified
        if exp_date and not df.empty:
            df = df[df['expiration'] == exp_date]

        logger.info(f"Fetched {len(df)} options for {symbol}")
        return df

    except Exception as e:
        logger.error(f"Error fetching option chain for {symbol}: {e}")
        return pd.DataFrame()


def parse_chain_to_dataframe(chain_data: dict) -> pd.DataFrame:
    """
    Parse Schwab API option chain response to DataFrame

    Args:
        chain_data: Raw API response JSON

    Returns:
        DataFrame with columns: strike, expiration, call_*, put_*, underlying_price
    """
    try:
        if not chain_data or chain_data.get('status') != 'SUCCESS':
            return pd.DataFrame()

        rows = []
        underlying_price = chain_data.get('underlyingPrice', 0)

        # Parse call options
        call_exp_map = chain_data.get('callExpDateMap', {})
        for exp_key, strikes in call_exp_map.items():
            # exp_key format: "2024-12-20:45" (date:DTE)
            exp_date = exp_key.split(':')[0]

            for strike_key, contracts in strikes.items():
                strike = float(strike_key)
                contract = contracts[0] if contracts else {}

                row = {
                    'strike': strike,
                    'expiration': exp_date,
                    'call_bid': safe_float(contract.get('bid', 0)),
                    'call_ask': safe_float(contract.get('ask', 0)),
                    'call_last': safe_float(contract.get('last', 0)),
                    'call_volume': safe_int(contract.get('totalVolume', 0)),
                    'call_open_interest': safe_int(contract.get('openInterest', 0)),
                    'call_delta': safe_float(contract.get('delta', 0)),
                    'call_gamma': safe_float(contract.get('gamma', 0)),
                    'call_theta': safe_float(contract.get('theta', 0)),
                    'call_vega': safe_float(contract.get('vega', 0)),
                    'call_iv': safe_float(contract.get('volatility', 0)),
                    'underlying_price': underlying_price
                }
                rows.append(row)

        # Parse put options
        put_exp_map = chain_data.get('putExpDateMap', {})
        for exp_key, strikes in put_exp_map.items():
            exp_date = exp_key.split(':')[0]

            for strike_key, contracts in strikes.items():
                strike = float(strike_key)
                contract = contracts[0] if contracts else {}

                # Find matching call row or create new
                matching_row = next((r for r in rows if r['strike'] == strike and r['expiration'] == exp_date), None)

                if matching_row:
                    # Add put data to existing row
                    matching_row.update({
                        'put_bid': safe_float(contract.get('bid', 0)),
                        'put_ask': safe_float(contract.get('ask', 0)),
                        'put_last': safe_float(contract.get('last', 0)),
                        'put_volume': safe_int(contract.get('totalVolume', 0)),
                        'put_open_interest': safe_int(contract.get('openInterest', 0)),
                        'put_delta': safe_float(contract.get('delta', 0)),
                        'put_gamma': safe_float(contract.get('gamma', 0)),
                        'put_theta': safe_float(contract.get('theta', 0)),
                        'put_vega': safe_float(contract.get('vega', 0)),
                        'put_iv': safe_float(contract.get('volatility', 0))
                    })
                else:
                    # Create new row with just put data
                    row = {
                        'strike': strike,
                        'expiration': exp_date,
                        'put_bid': safe_float(contract.get('bid', 0)),
                        'put_ask': safe_float(contract.get('ask', 0)),
                        'put_last': safe_float(contract.get('last', 0)),
                        'put_volume': safe_int(contract.get('totalVolume', 0)),
                        'put_open_interest': safe_int(contract.get('openInterest', 0)),
                        'put_delta': safe_float(contract.get('delta', 0)),
                        'put_gamma': safe_float(contract.get('gamma', 0)),
                        'put_theta': safe_float(contract.get('theta', 0)),
                        'put_vega': safe_float(contract.get('vega', 0)),
                        'put_iv': safe_float(contract.get('volatility', 0)),
                        'underlying_price': underlying_price
                    }
                    rows.append(row)

        df = pd.DataFrame(rows)

        # Fill NaN values with 0
        df = df.fillna(0)

        # Sort by expiration and strike
        if not df.empty:
            df = df.sort_values(['expiration', 'strike'])

        return df

    except Exception as e:
        logger.error(f"Error parsing chain data: {e}")
        return pd.DataFrame()


def generate_chain_heatmap(df: pd.DataFrame, viz_type: str = 'open_interest',
                          symbol: str = '') -> Optional[str]:
    """
    Generate Plotly heatmap visualization

    Args:
        df: Options chain DataFrame
        viz_type: Visualization type (open_interest, volume, liquidity, iv_surface, delta, gamma, theta, vega)
        symbol: Stock symbol for title

    Returns:
        Plotly figure as JSON string, or None if error
    """
    if not PLOTLY_AVAILABLE:
        logger.error("Plotly not available")
        return None

    if df.empty:
        logger.error("Empty DataFrame provided")
        return None

    try:
        # Create pivot table based on visualization type
        if viz_type == 'open_interest':
            fig = _create_oi_heatmap(df, symbol)
        elif viz_type == 'volume':
            fig = _create_volume_heatmap(df, symbol)
        elif viz_type == 'liquidity':
            fig = _create_liquidity_heatmap(df, symbol)
        elif viz_type == 'iv_surface':
            fig = _create_iv_surface(df, symbol)
        elif viz_type in ['delta', 'gamma', 'theta', 'vega']:
            fig = _create_greeks_heatmap(df, viz_type, symbol)
        elif viz_type == 'dashboard':
            fig = _create_dashboard_view(df, symbol)
        else:
            logger.error(f"Unknown visualization type: {viz_type}")
            return None

        # Return as JSON
        return fig.to_json()

    except Exception as e:
        logger.error(f"Error generating heatmap: {e}")
        return None


def _create_oi_heatmap(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create open interest heatmap"""
    # Pivot table: strikes x expirations
    pivot_calls = df.pivot_table(
        values='call_open_interest',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    pivot_puts = df.pivot_table(
        values='put_open_interest',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    # Create figure with subplots (calls and puts side by side)
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Open Interest', 'Put Open Interest'),
        shared_yaxes=True
    )

    # Add call heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values,
            x=pivot_calls.columns,
            y=pivot_calls.index,
            colorscale='Greens',
            name='Calls',
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>OI: %{z}<extra></extra>'
        ),
        row=1, col=1
    )

    # Add put heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values,
            x=pivot_puts.columns,
            y=pivot_puts.index,
            colorscale='Reds',
            name='Puts',
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>OI: %{z}<extra></extra>'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=f'{symbol} Options Chain - Open Interest',
        height=800,
        showlegend=False
    )

    fig.update_xaxes(title_text="Expiration", row=1, col=1)
    fig.update_xaxes(title_text="Expiration", row=1, col=2)
    fig.update_yaxes(title_text="Strike Price", row=1, col=1)

    return fig


def _create_volume_heatmap(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create volume heatmap"""
    pivot_calls = df.pivot_table(
        values='call_volume',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    pivot_puts = df.pivot_table(
        values='put_volume',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Volume', 'Put Volume'),
        shared_yaxes=True
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values,
            x=pivot_calls.columns,
            y=pivot_calls.index,
            colorscale='Blues',
            name='Calls'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values,
            x=pivot_puts.columns,
            y=pivot_puts.index,
            colorscale='Oranges',
            name='Puts'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=f'{symbol} Options Chain - Volume',
        height=800,
        showlegend=False
    )

    return fig


def _create_liquidity_heatmap(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create liquidity heatmap (based on bid-ask spread)"""
    # Calculate bid-ask spread percentage
    df['call_spread_pct'] = ((df['call_ask'] - df['call_bid']) / df['call_bid'] * 100).fillna(0)
    df['put_spread_pct'] = ((df['put_ask'] - df['put_bid']) / df['put_bid'] * 100).fillna(0)

    pivot_calls = df.pivot_table(
        values='call_spread_pct',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    pivot_puts = df.pivot_table(
        values='put_spread_pct',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Liquidity (Lower % = Better)', 'Put Liquidity'),
        shared_yaxes=True
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values,
            x=pivot_calls.columns,
            y=pivot_calls.index,
            colorscale='RdYlGn_r',  # Reverse: red=high spread (bad), green=low spread (good)
            name='Calls'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values,
            x=pivot_puts.columns,
            y=pivot_puts.index,
            colorscale='RdYlGn_r',
            name='Puts'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=f'{symbol} Options Chain - Liquidity (Bid-Ask Spread %)',
        height=800,
        showlegend=False
    )

    return fig


def _create_iv_surface(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create 3D IV surface plot"""
    # Average call and put IV
    df['avg_iv'] = (df['call_iv'] + df['put_iv']) / 2

    # Create 3D surface
    pivot = df.pivot_table(
        values='avg_iv',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    fig = go.Figure(data=[go.Surface(
        z=pivot.values,
        x=list(range(len(pivot.columns))),
        y=pivot.index,
        colorscale='Viridis',
        hovertemplate='Strike: %{y}<br>IV: %{z:.2%}<extra></extra>'
    )])

    fig.update_layout(
        title=f'{symbol} Implied Volatility Surface',
        scene=dict(
            xaxis_title='Expiration (index)',
            yaxis_title='Strike Price',
            zaxis_title='Implied Volatility'
        ),
        height=800
    )

    return fig


def _create_greeks_heatmap(df: pd.DataFrame, greek: str, symbol: str) -> go.Figure:
    """Create Greeks heatmap (delta, gamma, theta, vega)"""
    # Average call and put greeks
    call_col = f'call_{greek}'
    put_col = f'put_{greek}'

    pivot_calls = df.pivot_table(
        values=call_col,
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    pivot_puts = df.pivot_table(
        values=put_col,
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=(f'Call {greek.capitalize()}', f'Put {greek.capitalize()}'),
        shared_yaxes=True
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values,
            x=pivot_calls.columns,
            y=pivot_calls.index,
            colorscale='RdBu',
            name='Calls'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values,
            x=pivot_puts.columns,
            y=pivot_puts.index,
            colorscale='RdBu',
            name='Puts'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=f'{symbol} Options Chain - {greek.capitalize()}',
        height=800,
        showlegend=False
    )

    return fig


def _create_dashboard_view(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create multi-panel dashboard view"""
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=('Open Interest', 'Volume', 'IV', 'Delta'),
        specs=[[{'type': 'heatmap'}, {'type': 'heatmap'}],
               [{'type': 'heatmap'}, {'type': 'heatmap'}]]
    )

    # OI
    pivot_oi = df.pivot_table(
        values='call_open_interest',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )
    fig.add_trace(
        go.Heatmap(z=pivot_oi.values, x=pivot_oi.columns, y=pivot_oi.index, colorscale='Greens', showscale=False),
        row=1, col=1
    )

    # Volume
    pivot_vol = df.pivot_table(
        values='call_volume',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )
    fig.add_trace(
        go.Heatmap(z=pivot_vol.values, x=pivot_vol.columns, y=pivot_vol.index, colorscale='Blues', showscale=False),
        row=1, col=2
    )

    # IV
    pivot_iv = df.pivot_table(
        values='call_iv',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )
    fig.add_trace(
        go.Heatmap(z=pivot_iv.values, x=pivot_iv.columns, y=pivot_iv.index, colorscale='Viridis', showscale=False),
        row=2, col=1
    )

    # Delta
    pivot_delta = df.pivot_table(
        values='call_delta',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )
    fig.add_trace(
        go.Heatmap(z=pivot_delta.values, x=pivot_delta.columns, y=pivot_delta.index, colorscale='RdBu', showscale=False),
        row=2, col=2
    )

    fig.update_layout(
        title=f'{symbol} Options Chain Dashboard',
        height=1000,
        showlegend=False
    )

    return fig


def analyze_liquidity_zones(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Identify high and low liquidity zones in options chain

    Args:
        df: Options chain DataFrame

    Returns:
        Dictionary with liquidity analysis
    """
    try:
        if df.empty:
            return {}

        # Calculate total volume and OI
        df['total_volume'] = df['call_volume'] + df['put_volume']
        df['total_oi'] = df['call_open_interest'] + df['put_open_interest']

        # Group by strike to find zones
        strike_summary = df.groupby('strike').agg({
            'total_volume': 'sum',
            'total_oi': 'sum'
        }).reset_index()

        # Identify high liquidity zones (top 20% by volume + OI)
        threshold_volume = strike_summary['total_volume'].quantile(0.80)
        threshold_oi = strike_summary['total_oi'].quantile(0.80)

        high_liquidity = strike_summary[
            (strike_summary['total_volume'] >= threshold_volume) |
            (strike_summary['total_oi'] >= threshold_oi)
        ]

        # Low liquidity zones (bottom 20%)
        low_liquidity = strike_summary[
            (strike_summary['total_volume'] <= strike_summary['total_volume'].quantile(0.20)) &
            (strike_summary['total_oi'] <= strike_summary['total_oi'].quantile(0.20))
        ]

        return {
            'high_liquidity_strikes': high_liquidity['strike'].tolist(),
            'low_liquidity_strikes': low_liquidity['strike'].tolist(),
            'most_liquid_strike': strike_summary.loc[strike_summary['total_volume'].idxmax(), 'strike'],
            'least_liquid_strike': strike_summary.loc[strike_summary['total_volume'].idxmin(), 'strike']
        }

    except Exception as e:
        logger.error(f"Error analyzing liquidity: {e}")
        return {}


def calculate_max_pain(df: pd.DataFrame) -> float:
    """
    Calculate max pain strike price

    Max pain = strike where option holders experience maximum loss
    (i.e., where total intrinsic value of all options is minimized)

    Args:
        df: Options chain DataFrame

    Returns:
        Max pain strike price
    """
    try:
        if df.empty:
            return 0.0

        underlying_price = df['underlying_price'].iloc[0] if len(df) > 0 else 0

        strikes = df['strike'].unique()
        max_pain_values = []

        for strike in strikes:
            # Calculate intrinsic value for calls (if underlying > strike)
            call_intrinsic = df[df['strike'] < underlying_price].apply(
                lambda row: max(0, underlying_price - row['strike']) * row['call_open_interest'],
                axis=1
            ).sum()

            # Calculate intrinsic value for puts (if underlying < strike)
            put_intrinsic = df[df['strike'] > underlying_price].apply(
                lambda row: max(0, row['strike'] - underlying_price) * row['put_open_interest'],
                axis=1
            ).sum()

            total_value = call_intrinsic + put_intrinsic
            max_pain_values.append((strike, total_value))

        # Max pain = strike with minimum total intrinsic value
        max_pain_strike = min(max_pain_values, key=lambda x: x[1])[0] if max_pain_values else 0

        return float(max_pain_strike)

    except Exception as e:
        logger.error(f"Error calculating max pain: {e}")
        return 0.0
