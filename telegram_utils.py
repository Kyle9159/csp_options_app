# telegram_utils.py — Centralized Telegram alert utility
# All bots route through here. Each module uses: from telegram_utils import send_alert

import os
from telegram import Bot as telegram_bot

CHAT_ID = 7972059629

# Map bot_name → env var holding that bot's token
_BOT_ENV_VARS: dict[str, str] = {
    "scanner": "SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN",
    "leaps": "SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN",   # leaps shares scanner token
    "cc_bot": "COVERED_CALL_TELEGRAM_TOKEN",
    "spread_scanner": "SPREAD_SCANNER_TELEGRAM_TOKEN",
    "pmcc_scanner": "PMCC_SCANNER_TELEGRAM_TOKEN",
    "dashboard": "PAPER_TRADE_MONITOR_TELEGRAM_TOKEN",
}

_bots: dict = {}


def get_bot(bot_name: str):
    """Get or lazily create a Bot instance by name. Returns None if token not set."""
    if bot_name in _bots:
        return _bots[bot_name]
    env_var = _BOT_ENV_VARS.get(bot_name)
    token = os.getenv(env_var) if env_var else None
    _bots[bot_name] = telegram_bot(token=token) if token else None
    return _bots[bot_name]


async def send_alert(bot_name: str, message: str) -> None:
    """Send a Telegram message via the named bot. Logs and swallows errors."""
    bot = get_bot(bot_name)
    if not bot:
        print(f"   (Telegram disabled for bot={bot_name})")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, disable_web_page_preview=True)
        print(f"   Telegram [{bot_name}] sent.")
    except Exception as e:
        print(f"   Telegram [{bot_name}] failed: {e}")
