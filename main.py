"""Entry point.

Usage:
    python main.py          # start the bot
    python main.py reset    # delete all conversations (Discord + SQLite), then start
"""

from __future__ import annotations

import logging
import sys

import database as db
from bot import ChatBot
from config import load_config

log = logging.getLogger("main")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
