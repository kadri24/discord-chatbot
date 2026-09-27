"""Entry point.

Usage:
    python main.py          # start the bot
    python main.py reset    # delete all conversations (Discord + SQLite), then start
"""

from __future__ import annotations

import logging
import sys

import discord

import database as db
from bot import ChatBot
from config import load_config

log = logging.getLogger("main")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep the console readable: the LLM SDK's internal retry chatter and the
    # "voice will NOT be supported" warning (this bot is text-only) are noise.
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("discord.utils").setLevel(logging.ERROR)
    reset_mode = "reset" in sys.argv[1:]
    config = load_config()
    db.init(config.database_path)
    log.info(
        "Starting bot — model: %s | API: %s | server: %s%s",
        config.model,
        config.api_url,
        config.server_id,
        " | RESET MODE" if reset_mode else "",
    )
    bot = ChatBot(config, reset_mode=reset_mode)
    try:
        bot.run(config.discord_token)
    except discord.LoginFailure:
        raise SystemExit(
            "Discord rejected the bot token — the DISCORD_TOKEN in .env is wrong "
            "or was reset.\nFix: regenerate the token in the Discord Developer "
            "Portal (Your App → Bot → Reset Token), update .env, and start again."
        )
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Discord refused the connection because required privileged intents "
            "are disabled for this bot.\nFix: enable MESSAGE CONTENT INTENT in the "
            "Discord Developer Portal (Your App → Bot → Privileged Gateway "
            "Intents) and start again."
        )


if __name__ == "__main__":
    main()
