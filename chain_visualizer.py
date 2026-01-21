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

    # Get strike values and ensure consistent ordering
    strike_vals = sorted(pivot_calls.index.tolist())
    pivot_calls = pivot_calls.reindex(strike_vals)
    pivot_puts = pivot_puts.reindex(strike_vals)

    # Format labels for display (categorical)
    strike_labels = [f'${s:.0f}' for s in strike_vals]
    exp_labels = [str(e)[:10] for e in pivot_calls.columns.tolist()]

    # Create figure with subplots (calls and puts side by side)
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Open Interest', 'Put Open Interest'),
        shared_yaxes=True,
        horizontal_spacing=0.15
    )

    # Add call heatmap - use categorical labels directly
    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Greens',
            name='Calls',
            colorbar=dict(title='OI', x=0.42, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>OI: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=1
    )

    # Add put heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Reds',
            name='Puts',
            colorbar=dict(title='OI', x=1.02, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>OI: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=dict(text=f'{symbol} Options Chain - Open Interest', x=0.5, xanchor='center'),
        height=650,
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=80, r=100, t=80, b=80)
    )

    # Update axes - categorical type for proper cell rendering
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=1)
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=2)
    fig.update_yaxes(title_text="Strike Price", type='category', row=1, col=1)
    fig.update_yaxes(type='category', row=1, col=2)

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

    # Get strike values and ensure consistent ordering
    strike_vals = sorted(pivot_calls.index.tolist())
    pivot_calls = pivot_calls.reindex(strike_vals)
    pivot_puts = pivot_puts.reindex(strike_vals)

    # Format labels for display (categorical)
    strike_labels = [f'${s:.0f}' for s in strike_vals]
    exp_labels = [str(e)[:10] for e in pivot_calls.columns.tolist()]

    # Check if data has meaningful values
    has_data = pivot_calls.values.max() > 0 or pivot_puts.values.max() > 0

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Volume', 'Put Volume'),
        shared_yaxes=True,
        horizontal_spacing=0.15
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Blues',
            name='Calls',
            colorbar=dict(title='Vol', x=0.42, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Volume: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Oranges',
            name='Puts',
            colorbar=dict(title='Vol', x=1.02, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Volume: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=2
    )

    # Build title with data status warning
    title_text = f'{symbol} Options Chain - Volume'
    if not has_data:
        title_text += '<br><span style="color:orange;font-size:12px;">⚠️ No volume data - Market may be closed</span>'

    fig.update_layout(
        title=dict(text=title_text, x=0.5, xanchor='center'),
        height=650,
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=80, r=100, t=80, b=80)
    )

    # Update axes - categorical type for proper cell rendering
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=1)
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=2)
    fig.update_yaxes(title_text="Strike Price", type='category', row=1, col=1)
    fig.update_yaxes(type='category', row=1, col=2)

    return fig


def _create_liquidity_heatmap(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create liquidity heatmap (based on bid-ask spread)"""
    # Calculate bid-ask spread percentage
    df = df.copy()
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

    # Get strike values and ensure consistent ordering
    strike_vals = sorted(pivot_calls.index.tolist())
    pivot_calls = pivot_calls.reindex(strike_vals)
    pivot_puts = pivot_puts.reindex(strike_vals)

    # Format labels for display (categorical)
    strike_labels = [f'${s:.0f}' for s in strike_vals]
    exp_labels = [str(e)[:10] for e in pivot_calls.columns.tolist()]

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=('Call Liquidity (Lower % = Better)', 'Put Liquidity'),
        shared_yaxes=True,
        horizontal_spacing=0.15
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='RdYlGn_r',  # Reverse: red=high spread (bad), green=low spread (good)
            name='Calls',
            colorbar=dict(title='Spread %', x=0.42, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Spread: %{z:.1f}%<extra></extra>'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='RdYlGn_r',
            name='Puts',
            colorbar=dict(title='Spread %', x=1.02, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Spread: %{z:.1f}%<extra></extra>'
        ),
        row=1, col=2
    )

    fig.update_layout(
        title=dict(text=f'{symbol} Options Chain - Liquidity (Bid-Ask Spread %)', x=0.5, xanchor='center'),
        height=650,
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=80, r=100, t=80, b=80)
    )

    # Update axes - categorical type for proper cell rendering
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=1)
    fig.update_xaxes(title_text="Expiration Date", type='category', tickangle=45, row=1, col=2)
    fig.update_yaxes(title_text="Strike Price", type='category', row=1, col=1)
    fig.update_yaxes(type='category', row=1, col=2)

    return fig


def _create_iv_surface(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create 3D IV surface plot"""
    # Average call and put IV
    df = df.copy()
    df['avg_iv'] = (df['call_iv'] + df['put_iv']) / 2

    # Create 3D surface
    pivot = df.pivot_table(
        values='avg_iv',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    # Format labels
    strike_labels = [f'${s:.0f}' for s in pivot.index.tolist()]
    exp_labels = [str(e)[:10] for e in pivot.columns.tolist()]

    # Check if IV data exists
    has_data = pivot.values.max() > 0.01

    fig = go.Figure(data=[go.Surface(
        z=pivot.values,
        x=list(range(len(pivot.columns))),
        y=pivot.index,
        colorscale='Viridis',
        hovertemplate='Strike: $%{y:.0f}<br>IV: %{z:.1f}%<extra></extra>'
    )])

    title_text = f'{symbol} Implied Volatility Surface'
    if not has_data:
        title_text += '<br><span style="color:orange;font-size:12px;">⚠️ Limited IV data - Market may be closed</span>'

    fig.update_layout(
        title=dict(text=title_text, x=0.5, xanchor='center'),
        scene=dict(
            xaxis_title='Expiration',
            xaxis=dict(
                ticktext=exp_labels,
                tickvals=list(range(len(exp_labels))),
                tickangle=45
            ),
            yaxis_title='Strike Price ($)',
            zaxis_title='Implied Volatility (%)'
        ),
        height=700,
        paper_bgcolor='white'
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

    # Get strike values and ensure consistent ordering
    strike_vals = sorted(pivot_calls.index.tolist())
    pivot_calls = pivot_calls.reindex(strike_vals)
    pivot_puts = pivot_puts.reindex(strike_vals)

    # Format labels for display (categorical)
    strike_labels = [f'${s:.0f}' for s in strike_vals]
    exp_labels = [str(e)[:10] for e in pivot_calls.columns.tolist()]

    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=(f'Call {greek.capitalize()}', f'Put {greek.capitalize()}'),
        shared_yaxes=True,
        horizontal_spacing=0.15
    )

    # Check if data has meaningful values (not all zeros)
    calls_max = abs(pivot_calls.values).max() if pivot_calls.size > 0 else 0
    puts_max = abs(pivot_puts.values).max() if pivot_puts.size > 0 else 0
    has_data = calls_max > 0.001 or puts_max > 0.001

    fig.add_trace(
        go.Heatmap(
            z=pivot_calls.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='RdBu',
            name='Calls',
            colorbar=dict(title=greek.capitalize(), x=0.42, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>' + greek.capitalize() + ': %{z:.4f}<extra></extra>'
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Heatmap(
            z=pivot_puts.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='RdBu',
            name='Puts',
            colorbar=dict(title=greek.capitalize(), x=1.02, len=0.8),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>' + greek.capitalize() + ': %{z:.4f}<extra></extra>'
        ),
        row=1, col=2
    )

    # Build title with data status warning
    title_text = f'{symbol} Options Chain - {greek.capitalize()}'
    if not has_data:
        title_text += '<br><span style="color:orange;font-size:12px;">⚠️ Limited data - Market may be closed or Greeks unavailable</span>'

    fig.update_layout(
        title=dict(text=title_text, x=0.5, xanchor='center'),
        height=650,
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=80, r=100, t=80, b=80)
    )

    # Update axes - categorical type for proper cell rendering
    fig.update_xaxes(title_text='Expiration Date', type='category', tickangle=45, row=1, col=1)
    fig.update_xaxes(title_text='Expiration Date', type='category', tickangle=45, row=1, col=2)
    fig.update_yaxes(title_text='Strike Price', type='category', row=1, col=1)
    fig.update_yaxes(type='category', row=1, col=2)

    return fig


def _create_dashboard_view(df: pd.DataFrame, symbol: str) -> go.Figure:
    """Create multi-panel dashboard view"""
    from plotly.subplots import make_subplots

    # Ensure we have required columns
    for col in ['call_open_interest', 'call_volume', 'call_iv', 'call_delta']:
        if col not in df.columns:
            df[col] = 0

    # OI pivot
    pivot_oi = df.pivot_table(
        values='call_open_interest',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    # Volume pivot
    pivot_vol = df.pivot_table(
        values='call_volume',
        index='strike',
        columns='expiration',
        aggfunc='sum',
        fill_value=0
    )

    # IV pivot
    pivot_iv = df.pivot_table(
        values='call_iv',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    # Delta pivot
    pivot_delta = df.pivot_table(
        values='call_delta',
        index='strike',
        columns='expiration',
        aggfunc='mean',
        fill_value=0
    )

    # Check if pivot is empty
    if pivot_oi.empty or len(pivot_oi.index) == 0:
        logger.warning(f"No data to display for {symbol} dashboard")
        # Return empty figure with message
        fig = go.Figure()
        fig.add_annotation(
            text=f"No options data available for {symbol}",
            xref="paper", yref="paper",
            x=0.5, y=0.5, showarrow=False,
            font=dict(size=20, color="orange")
        )
        fig.update_layout(height=650, paper_bgcolor='white')
        return fig

    # Get strike values and expiration labels
    strike_vals = sorted(pivot_oi.index.tolist())  # Ensure sorted for proper display
    exp_labels = [str(e)[:10] for e in pivot_oi.columns.tolist()]
    num_exps = len(exp_labels)
    num_strikes = len(strike_vals)

    # Format strike labels for display
    strike_labels = [f'${s:.0f}' for s in strike_vals]

    logger.info(f"Dashboard: {num_strikes} strikes x {num_exps} expirations")
    logger.info(f"Strike range: ${min(strike_vals):.0f} - ${max(strike_vals):.0f}")

    # Reindex pivots to ensure consistent ordering
    pivot_oi = pivot_oi.reindex(strike_vals)
    pivot_vol = pivot_vol.reindex(strike_vals)
    pivot_iv = pivot_iv.reindex(strike_vals)
    pivot_delta = pivot_delta.reindex(strike_vals)

    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=('Open Interest (Calls)', 'Volume (Calls)', 'Implied Volatility', 'Delta'),
        vertical_spacing=0.15,
        horizontal_spacing=0.12
    )

    # Use categorical x (expiration) and y (strike) axes for heatmaps
    # This ensures proper cell rendering regardless of numeric spacing

    # OI Heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_oi.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Greens',
            showscale=True,
            colorbar=dict(title='OI', x=0.45, y=0.8, len=0.3),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>OI: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=1
    )

    # Volume Heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_vol.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Blues',
            showscale=True,
            colorbar=dict(title='Vol', x=1.02, y=0.8, len=0.3),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Volume: %{z:,.0f}<extra></extra>'
        ),
        row=1, col=2
    )

    # IV Heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_iv.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='Viridis',
            showscale=True,
            colorbar=dict(title='IV%', x=0.45, y=0.2, len=0.3),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>IV: %{z:.1f}%<extra></extra>'
        ),
        row=2, col=1
    )

    # Delta Heatmap
    fig.add_trace(
        go.Heatmap(
            z=pivot_delta.values.tolist(),
            x=exp_labels,
            y=strike_labels,
            colorscale='RdBu',
            showscale=True,
            colorbar=dict(title='Delta', x=1.02, y=0.2, len=0.3),
            hovertemplate='Strike: %{y}<br>Exp: %{x}<br>Delta: %{z:.3f}<extra></extra>'
        ),
        row=2, col=2
    )

    # Check if we have meaningful data
    has_oi = pivot_oi.values.max() > 0
    has_vol = pivot_vol.values.max() > 0
    has_iv = pivot_iv.values.max() > 0.01
    has_delta = abs(pivot_delta.values).max() > 0.001

    title_text = f'{symbol} Options Chain Dashboard'
    if not (has_oi and has_vol and has_iv and has_delta):
        title_text += '<br><span style="color:orange;font-size:11px;">⚠️ Some data limited - Market may be closed</span>'

    fig.update_layout(
        title=dict(text=title_text, x=0.5, xanchor='center', font=dict(size=16)),
        height=650,
        width=None,  # Auto width
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin=dict(l=80, r=100, t=80, b=80)
    )

    # Update axes - using categorical data, so set type explicitly
    for row in [1, 2]:
        for col in [1, 2]:
            fig.update_xaxes(
                type='category',
                tickangle=45,
                tickfont=dict(size=8),
                row=row, col=col
            )
            fig.update_yaxes(
                type='category',
                tickfont=dict(size=8),
                row=row, col=col
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
