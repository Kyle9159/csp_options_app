"""
Centralized configuration for the options trading dashboard.
Uses Pydantic for validation and type safety.
Includes Windows emoji support.
"""

import sys
import io

# FIX WINDOWS EMOJI ENCODING FIRST - before any logging or output
if sys.platform == 'win32':
    # Ensure stdout and stderr use UTF-8 encoding
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7 fallback
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# NOW continue with existing Pydantic imports
import os
import logging
from pathlib import Path
from typing import Optional
from pydantic import validator
from pydantic_settings import BaseSettings
import dotenv

# Load environment variables
dotenv.load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

class SchwabConfig(BaseSettings):
    """Schwab API configuration"""
    api_key: str
    app_secret: str
    redirect_uri: str = "https://127.0.0.1:8182"
    token_path: str = "cache_files/schwab_token.json"
    paper_trading: bool = True
    paper_account_id: Optional[str] = None
    live_account_id: Optional[str] = None

    @property
    def account_id(self) -> str:
        """Get the appropriate account ID based on trading mode"""
        if self.paper_trading:
            if not self.paper_account_id:
                raise ValueError("PAPER_ACCOUNT_ID must be set when paper_trading=True")
            return self.paper_account_id
        else:
            if not self.live_account_id:
                raise ValueError("LIVE_ACCOUNT_ID must be set when paper_trading=False")
            return self.live_account_id

    class Config:
        env_prefix = "SCHWAB_"

class GrokConfig(BaseSettings):
    """Grok AI API configuration"""
    api_key: str
    endpoint: str = "https://api.x.ai/v1/chat/completions"

    class Config:
        env_prefix = "XAI_"

class GoogleSheetsConfig(BaseSettings):
    """Google Sheets configuration"""
    sheet_id: str

    class Config:
        env_prefix = "GOOGLE_"

class TelegramConfig(BaseSettings):
    """Telegram bot configuration"""
    token: str
    chat_id: str

    class Config:
        env_prefix = "TELEGRAM_"

class TradingConfig(BaseSettings):
    """Trading parameters configuration"""
    tier_1_iv_min: float = 30.0
    tier_1_delta_max: float = 0.40
    tier_2_iv_min: float = 32.0
    tier_2_delta_max: float = 0.35
    tier_3_iv_min: float = 40.0
    tier_3_delta_max: float = 0.30
    tier_3_vol_surge_min: float = 2.0

    # Account balance settings
    account_balance_live: float = 1000.0
    account_balance_paper: float = 100000.0

    # Risk management
    daily_loss_limit_live: float = -0.20
    daily_loss_limit_paper: float = -0.30
    rr_ratio_live: float = 3.0
    rr_ratio_paper: float = 2.0

    # Position sizing
    atr_multiplier_live: float = 1.8
    atr_multiplier_paper: float = 2.5
    max_concurrent_live: int = 3
    max_concurrent_paper: int = 8

    # Exit targets
    trailing_stop_pct_live: float = 0.35
    trailing_stop_pct_paper: float = 0.60
    partial_close_pct_live: float = 0.50
    partial_close_pct_paper: float = 0.00

    # Filters
    vix_spike_exit_live: float = 38.0
    vix_spike_exit_paper: float = 60.0
    ev_threshold_live: float = 0.38
    ev_threshold_paper: float = 0.20
    vix_cap_live: float = 34.0
    vix_cap_paper: float = 70.0

    # Capital allocation
    max_capital_per_trade: float = 30000.0
    wheel_capital: float = 100000.0

class CacheConfig(BaseSettings):
    """Cache configuration"""
    base_dir: Path = Path("cache_files")
    scanner_cache_hours: int = 24
    leaps_cache_hours: int = 24
    sr_cache_hours: int = 168  # 1 week
    grok_cache_hours: int = 1

class AppConfig(BaseSettings):
    """Main application configuration"""
    schwab: SchwabConfig
    grok: GrokConfig
    google_sheets: GoogleSheetsConfig
    telegram: TelegramConfig
    trading: TradingConfig
    cache: CacheConfig

    # Derived paths
    dashboard_file: Path = Path("trading_dashboard.html")

    @validator('schwab', pre=True)
    def validate_schwab_config(cls, v):
        """Ensure Schwab config has required fields"""
        if isinstance(v, dict):
            api_key = os.getenv('SCHWAB_API_KEY')
            app_secret = os.getenv('SCHWAB_APP_SECRET')
            if not api_key or not app_secret:
                raise ValueError("SCHWAB_API_KEY and SCHWAB_APP_SECRET environment variables are required")
        return v

    @validator('grok', pre=True)
    def validate_grok_config(cls, v):
        """Ensure Grok config has required fields"""
        if isinstance(v, dict):
            api_key = os.getenv('XAI_API_KEY')
            if not api_key:
                raise ValueError("XAI_API_KEY environment variable is required")
        return v

# Global configuration instance
try:
    config = AppConfig(
        schwab=SchwabConfig(),
        grok=GrokConfig(),
        google_sheets=GoogleSheetsConfig(sheet_id=os.getenv('GOOGLE_SHEET_ID', '')),
        telegram=TelegramConfig(),
        trading=TradingConfig(),
        cache=CacheConfig()
    )
except Exception as e:
    logger.error(f"Configuration error: {e}")
    # Provide minimal fallback config for development
    config = None

def get_config() -> AppConfig:
    """Get the global configuration instance"""
    if config is None:
        raise RuntimeError("Configuration not properly initialized. Check environment variables.")
    return config