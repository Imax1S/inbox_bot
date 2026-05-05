"""One-shot Telethon login helper.

Run once to produce a reusable session file for read-only channel ingestion::

    python -m src.scripts.telethon_login

Reads `TELETHON_API_ID`, `TELETHON_API_HASH`, and `TELETHON_SESSION_PATH`
from the environment (via `.env`). The script prompts for your phone, the
login code, and (if enabled) the 2FA password — Telethon stores the resulting
session at the configured path.

The bot process itself never calls `client.start()` and never reads from any
channel outside the `telegram_channels` allowlist.
"""

from __future__ import annotations

import sys

from ..config import load_config


def main() -> int:
    config = load_config()
    tele = config.telethon
    if tele.api_id <= 0 or not tele.api_hash:
        print(
            "TELETHON_API_ID / TELETHON_API_HASH are not set. "
            "Create an app at https://my.telegram.org and put the values into .env.",
            file=sys.stderr,
        )
        return 1

    tele.session_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from telethon.sync import TelegramClient  # type: ignore[import-not-found]
    except ImportError:
        print(
            "telethon is not installed. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    print(f"Session will be written to: {tele.session_path}")
    with TelegramClient(str(tele.session_path), tele.api_id, tele.api_hash) as client:
        me = client.get_me()
        print(f"Logged in as: {me.first_name} (@{me.username})")
    print("Done. You can now start the bot normally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
