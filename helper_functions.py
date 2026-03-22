import pandas as pd
from datetime import datetime

def safe_float(value, default=0.0):
    """Safely convert value to float, handling currency symbols and percentages"""
    try:
        if pd.isna(value) or value is None or value == '':
            return default
        # If it's already a number, return it
        if isinstance(value, (int, float)):
            return float(value)
        # Convert to string and strip currency/percentage symbols
        str_val = str(value).strip()
        str_val = str_val.replace('$', '').replace('%', '').replace(',', '').strip()
        if str_val == '' or str_val == '-':
            return default
        return float(str_val)
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    """Safely convert value to int"""
    try:
        if pd.isna(value) or value is None or value == '':
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default

def safe_date(value, default=None):
    """Safely convert value to date"""
    try:
        if pd.isna(value) or value is None or value == '':
            return default
        if isinstance(value, str):
            return pd.to_datetime(value).date()
        return value
    except (ValueError, TypeError):
        return default

def safe_date_update(row, date_value):
    """Safely update a row's date field"""
    try:
        safe_date_val = safe_date(date_value)
        if safe_date_val:
            return safe_date_val
        # If safe_date returns None/default, try to get existing value
        return row.get('Exp Date', safe_date_val)
    except (ValueError, TypeError, KeyError):
        return row.get('Exp Date', None)

# Cache management functions
import json
import time
from pathlib import Path

# Cache TTL constants (in hours)
SCANNER_CACHE_HOURS = 2   # Scanner cache expires after 2 hours
LEAPS_CACHE_HOURS = 24    # LEAPS cache expires after 24 hours

def save_cached_scanner(data):
    """Save scanner opportunities to cache with timestamp"""
    try:
        cache_file = Path("cache_files") / "simple_scanner_cache.json"
        cache_data = {
            'timestamp': time.time(),
            'data': data
        }
        with open(cache_file, 'w') as f:
            json.dump(cache_data, f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save scanner cache: {e}")

def load_cached_scanner(max_age_hours=None):
    """Load scanner opportunities from cache if not expired

    Args:
        max_age_hours: Maximum cache age in hours. Defaults to SCANNER_CACHE_HOURS (2 hours)

    Returns:
        Cached data if valid and not expired, None otherwise
    """
    try:
        cache_file = Path("cache_files") / "simple_scanner_cache.json"
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)

            # Check for new format with timestamp
            if isinstance(cache_data, dict) and 'timestamp' in cache_data:
                cache_age_hours = (time.time() - cache_data['timestamp']) / 3600
                ttl = max_age_hours if max_age_hours is not None else SCANNER_CACHE_HOURS

                if cache_age_hours > ttl:
                    print(f"Scanner cache expired ({cache_age_hours:.1f}h old, TTL={ttl}h)")
                    return None

                data = cache_data['data']
            else:
                # Legacy format without timestamp - treat as expired
                print("Scanner cache in legacy format (no timestamp), treating as expired")
                return None

            # Fix: Parse support_resistance if it's a string
            if data:
                for tile in data:
                    if 'suggestions' in tile:
                        for opp in tile['suggestions']:
                            if 'support_resistance' in opp and isinstance(opp['support_resistance'], str):
                                try:
                                    # Parse the string representation back to dict
                                    opp['support_resistance'] = eval(opp['support_resistance'])
                                except:
                                    opp['support_resistance'] = {}

            return data
    except Exception as e:
        print(f"Failed to load scanner cache: {e}")
    return None  # Return None to trigger fresh scan

def save_cached_leaps(data, LEAPS_CACHE_FILE=None):
    """Save LEAPS opportunities to cache with timestamp"""
    try:
        cache_file = Path("cache_files") / (LEAPS_CACHE_FILE or "leaps_cache.json")
        cache_data = {
            'timestamp': time.time(),
            'data': data
        }
        with open(cache_file, 'w') as f:
            json.dump(cache_data, f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save leaps cache: {e}")

def load_cached_leaps(LEAPS_CACHE_FILE=None, max_age_hours=None):
    """Load LEAPS opportunities from cache if not expired

    Args:
        LEAPS_CACHE_FILE: Custom cache filename
        max_age_hours: Maximum cache age in hours. Defaults to LEAPS_CACHE_HOURS (24 hours)

    Returns:
        Cached data if valid and not expired, None otherwise
    """
    try:
        cache_file = Path("cache_files") / (LEAPS_CACHE_FILE or "leaps_cache.json")
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)

            # Check for new format with timestamp
            if isinstance(cache_data, dict) and 'timestamp' in cache_data:
                cache_age_hours = (time.time() - cache_data['timestamp']) / 3600
                ttl = max_age_hours if max_age_hours is not None else LEAPS_CACHE_HOURS

                if cache_age_hours > ttl:
                    print(f"LEAPS cache expired ({cache_age_hours:.1f}h old, TTL={ttl}h)")
                    return None

                return cache_data['data']
            else:
                # Legacy format without timestamp - treat as expired
                print("LEAPS cache in legacy format (no timestamp), treating as expired")
                return None
    except Exception as e:
        print(f"Failed to load leaps cache: {e}")
    return None  # Return None to trigger fresh scan

def save_sr_cache(data):
    """Save support/resistance cache"""
    try:
        cache_file = Path("cache_files") / "support_resistance_cache.json"
        with open(cache_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save SR cache: {e}")

def load_sr_cache():
    """Load support/resistance cache"""
    try:
        cache_file = Path("cache_files") / "support_resistance_cache.json"
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to load SR cache: {e}")
    return {}

def calculate_trade_score(opportunity):
    """
    Calculate a composite trade score (0-100) blending quantitative heuristics
    with Grok's AI probability when available.

    Weights:
      - Grok AI probability:  40 pts  (if available, else heuristics expand)
      - Premium quality:      15 pts
      - Delta sweet spot:     15 pts
      - IV environment:       15 pts
      - DTE sweet spot:       15 pts
    """
    try:
        score = 0.0

        # --- Grok AI probability (40% of score when present) ---
        grok_prob = safe_float(opportunity.get('grok_profit_prob', 0))
        has_grok = grok_prob > 0
        if has_grok:
            # Scale 0-1 probability to 0-40 points
            score += grok_prob * 40

        # --- Heuristic factors (60 pts with Grok, 100 pts without) ---
        # If Grok score is missing, heuristic weight expands to fill 100
        weight = 1.0 if has_grok else (100 / 60)

        # Premium quality (0-15 pts scaled)
        premium = safe_float(opportunity.get('premium', 0))
        strike = safe_float(opportunity.get('strike', 0))
        if strike > 0 and premium > 0:
            # Annualized return on capital = (premium/strike) * (365/DTE) * 100
            dte = max(safe_int(opportunity.get('dte', 30)), 1)
            ann_return = (premium / strike) * (365 / dte) * 100
            prem_pts = min(ann_return / 3.0, 15)  # 45%+ annualized = full marks
        elif premium > 5:
            prem_pts = 15
        elif premium > 2:
            prem_pts = 10
        else:
            prem_pts = 5
        score += prem_pts * weight

        # Delta sweet spot (0-15 pts)
        delta = abs(safe_float(opportunity.get('delta', 0)))
        if 0.15 <= delta <= 0.30:
            delta_pts = 15  # ideal CSP range
        elif 0.10 <= delta <= 0.40:
            delta_pts = 12
        elif delta <= 0.50:
            delta_pts = 8
        else:
            delta_pts = 3
        score += delta_pts * weight

        # IV environment (0-15 pts)
        iv = safe_float(opportunity.get('iv', 30))
        if iv > 50:
            iv_pts = 15  # rich premium
        elif iv > 35:
            iv_pts = 12
        elif iv > 25:
            iv_pts = 9
        else:
            iv_pts = 5
        score += iv_pts * weight

        # DTE sweet spot (0-15 pts)
        dte = safe_int(opportunity.get('dte', 0))
        if 21 <= dte <= 45:
            dte_pts = 15  # optimal theta decay
        elif 14 <= dte <= 60:
            dte_pts = 12
        elif 7 <= dte <= 90:
            dte_pts = 8
        else:
            dte_pts = 3
        score += dte_pts * weight

        opportunity['trade_score'] = int(min(round(score), 100))
        return opportunity['trade_score']

    except Exception as e:
        print(f"Error calculating trade score: {e}")
        return 0