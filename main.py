"""Entry point.

Usage:
    python main.py                  # open the manager window
    python main.py reset            # open it and reset all conversations
    python main.py --console        # run the bot in the console (headless)
    python main.py --console reset  # console: reset conversations, then run
    python gui.py                   # open the manager window directly

The manager window (gui.py) edits config.json / .env and runs the bot as a
child process with --console. If the GUI cannot start (pywebview missing or
no display), main.py falls back to console mode automatically.
"""

from __future__ import annotations

import logging
import sys

import discord

import database as db
from bot import ChatBot
from config import load_config

log = logging.getLogger("main")


def run_console(reset_mode: bool) -> None:
    """Run the bot attached to this console (headless-friendly)."""
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


def run_gui(reset_on_start: bool, debug: bool) -> None:
    """Open the manager window. Raises if pywebview or a display is missing."""
    import gui  # local import: console mode must work without pywebview
    gui.main(reset_on_start=reset_on_start, debug=debug)


def main() -> None:
    args = sys.argv[1:]
    console = "--console" in args
    reset = "reset" in args
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Keep the console readable: the LLM SDK's internal retry chatter and the
    # "voice will NOT be supported" warning (this bot is text-only) are noise.
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("discord.utils").setLevel(logging.ERROR)

    if not console:
        try:
            run_gui(reset, debug="--debug" in args)
            return  # window closed normally
        except ImportError:
            print("pywebview is not installed — the GUI needs it: pip install pywebview")
            print("Falling back to console mode.\n")
        except Exception as exc:
            print(f"The GUI could not start ({exc}).")
            print("Falling back to console mode.\n")

    run_console(reset)


if __name__ == "__main__":
    main()
