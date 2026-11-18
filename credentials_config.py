"""
Default cloud credentials and security configuration for Renko ATR Pro Bot.
Ensures the cloud instance on Render stays connected to Capital.com 24/7 across all devices,
and protects administration with master password authentication.
"""
import os

DEFAULT_CREDENTIALS = {
    "CAPITAL_API_KEY": "wXYLSXYqoS9KbGh8",
    "CAPITAL_IDENTIFIER": "nileshmandape2026@gmail.com",
    "CAPITAL_PASSWORD": "Nilesh@@2026",
    "CAPITAL_ENV": "demo",
    "TELEGRAM_BOT_TOKEN": "9278187220:AAE51RaRhFHVWmXxCMNCOwA8BK21vsacwbA",
    "TELEGRAM_CHAT_ID": "1096576760",
    "DASHBOARD_PASSWORD": "1978",
    "VIEW_ONLY_PUBLIC": "1",
    "AUTO_START_WATCHER": "1",
}


def apply_default_credentials():
    """Apply default credentials if not already supplied via OS environment variables."""
    for k, v in DEFAULT_CREDENTIALS.items():
        if v and not os.environ.get(k):
            os.environ[k] = str(v)
