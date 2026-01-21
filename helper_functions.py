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
from pathlib import Path

def save_cached_scanner(data):
    """Save scanner opportunities to cache"""
    try:
        cache_file = Path("cache_files") / "simple_scanner_cache.json"
        with open(cache_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save scanner cache: {e}")

def load_cached_scanner():
    """Load scanner opportunities from cache"""
    try:
        cache_file = Path("cache_files") / "simple_scanner_cache.json"
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                data = json.load(f)

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
    """Save LEAPS opportunities to cache"""
    try:
        cache_file = Path("cache_files") / (LEAPS_CACHE_FILE or "leaps_cache.json")
        with open(cache_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save leaps cache: {e}")

def load_cached_leaps(LEAPS_CACHE_FILE=None):
    """Load LEAPS opportunities from cache"""
    try:
        cache_file = Path("cache_files") / (LEAPS_CACHE_FILE or "leaps_cache.json")
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
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
    """Calculate a trade score for an opportunity"""
    try:
        # Simple scoring based on common factors
        score = 0

        # Premium score (higher premium = better)
        premium = safe_float(opportunity.get('premium', 0))
        if premium > 5:
            score += 20
        elif premium > 2:
            score += 10

        # Delta score (moderate delta preferred for covered calls)
        delta = abs(safe_float(opportunity.get('delta', 0)))
        if 0.2 <= delta <= 0.4:
            score += 15
        elif 0.1 <= delta <= 0.6:
            score += 10

        # IV score (higher IV = more opportunity)
        iv = safe_float(opportunity.get('iv', 30))
        if iv > 40:
            score += 15
        elif iv > 25:
            score += 10

        # DTE score (prefer 30-90 days)
        dte = safe_int(opportunity.get('dte', 0))
        if 30 <= dte <= 90:
            score += 15
        elif 15 <= dte <= 120:
            score += 10

        return min(score, 100)  # Cap at 100

    except Exception as e:
        print(f"Error calculating trade score: {e}")
        return 0