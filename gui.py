"""Desktop manager for the bot (pywebview).

Usage:
    python gui.py            # open the manager window
    python gui.py --debug    # open it with WebView devtools enabled
    python main.py           # same window (default entry point)

The window edits config.json / .env and runs the bot as a child process
(`main.py --console`), streaming its console output into the log pane. The
GUI survives bot crashes; stopping the bot sends CTRL_BREAK so py-cord can
shut down cleanly.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

try:
    import webview
except ImportError as exc:  # main.py catches this and falls back to console
    raise ImportError("The GUI needs pywebview: pip install pywebview") from exc

from config import DEFAULT_CONFIG, ENV_TEMPLATE, load_config

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)  # main.py expects to run from the project directory
CONFIG_PATH = "config.json"
ENV_PATH = ".env"
CONFIG_KEYS = list(DEFAULT_CONFIG.keys())


class BotProcess:
    """Owns the child process running main.py and pumps its output to the UI."""

    def __init__(self, on_line, on_exit):
        self._on_line = on_line
        self._on_exit = on_exit
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, *args) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                # --console is essential: main.py defaults to the GUI, and the
                # child must be the bot itself, not another window.
                [sys.executable, os.path.join(APP_DIR, "main.py"), "--console", *args],
                cwd=APP_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def stop(self) -> None:
        """Ask the child to exit (KeyboardInterrupt on Windows); kill if stubborn."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except OSError:
            pass

        def _reap():
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        threading.Thread(target=_reap, daemon=True).start()

    def _pump(self):
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._on_line(line.rstrip("\r\n"))
        self._on_exit(proc.wait())
        with self._lock:
            if self._proc is proc:
                self._proc = None


# Python -> UI push. Module-level on purpose: pywebview walks every public
# attribute of the js_api object recursively when building the JS bridge, and
# a stored pywebview Window reaches native -> WinForms Form ->
# AccessibilityObject -> Bounds -> Empty -> Empty -> ... — an infinite .NET
# property chain that kills the bridge with RecursionError (issue #1815).
# Keep the window and the bot OUT of the Api object.

_WINDOW = None  # pywebview window, set in main()


def _push(js: str) -> None:
    """Run JS inside the window from any thread; skipped when it's closing."""
    if _WINDOW is not None:
        try:
            _WINDOW.evaluate_js(js)
        except Exception:
            pass  # window is closing or gone


def _push_line(line: str) -> None:
    _push(f"window.appendLog({json.dumps(line)});")


def _push_exit(code) -> None:
    _push(f"window.onBotExit({json.dumps(str(code))});")


_BOT = BotProcess(_push_line, _push_exit)


class Api:
    """Exposed to the frontend as window.pywebview.api.*

    Keep this class free of data attributes (see the note above _WINDOW):
    only the five JS-callable methods below belong here.
    """

    def _push_start(self) -> None:
        _push("window.setRunning(true);")

    # -- helpers -------------------------------------------------------------

    def _ensure_files(self) -> None:
        """Same idea as config.ensure_setup(), just silent (GUI has no console)."""
        if not os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=4)
                fh.write("\n")
        if not os.path.exists(ENV_PATH):
            with open(ENV_PATH, "w", encoding="utf-8") as fh:
                fh.write(ENV_TEMPLATE)

    def _validate(self) -> str | None:
        """None when the configuration is usable, else the fix-it checklist."""
        try:
            load_config(CONFIG_PATH)
        except SystemExit as exc:
            return str(exc)
        return None

    # -- JS -> Python API ------------------------------------------------------

    def get_state(self) -> dict:
        self._ensure_files()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            raw = {}
        token = ""
        try:
            with open(ENV_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DISCORD_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
        return {
            "config": {key: raw.get(key, "") for key in CONFIG_KEYS},
            "token": token,
            "running": _BOT.running,
        }

    def save_config(self, values: dict, token: str) -> dict:
        clean = {key: str(values.get(key, "")) for key in CONFIG_KEYS}
        try:
            clean["stream_edit_interval"] = float(clean["stream_edit_interval"])
        except (TypeError, ValueError):
            pass  # keep the raw value; load_config reports the problem
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=4)
            fh.write("\n")
        with open(ENV_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"DISCORD_TOKEN={token.strip()}\n")
        problem = self._validate()
        return {"ok": problem is None, "message": problem or "Saved."}

    def start_bot(self, reset: bool = False) -> dict:
        if _BOT.running:
            return {"ok": False, "message": "The bot is already running."}
        problem = self._validate()
        if problem:
            return {"ok": False, "message": problem}
        if not _BOT.start(*(["reset"] if reset else [])):
            return {"ok": False, "message": "The bot is already running."}
        self._push_start()
        return {"ok": True, "message": "Resetting conversations..." if reset else "Starting..."}

    def stop_bot(self) -> dict:
        if not _BOT.running:
            return {"ok": False, "message": "The bot is not running."}
        _BOT.stop()
        return {"ok": True, "message": "Stopping..."}

    def reset_bot(self) -> dict:
        """Stop the running bot, then start it in reset mode."""
        if _BOT.running:
            _BOT.stop()
            deadline = time.time() + 10
            while _BOT.running and time.time() < deadline:
                time.sleep(0.2)
        return self.start_bot(reset=True)


def main(reset_on_start: bool = False, debug: bool = False) -> None:
    global _WINDOW
    debug = debug or "--debug" in sys.argv[1:]
    api = Api()
    _WINDOW = webview.create_window(
        "discord-llm manager",
        os.path.join(APP_DIR, "gui_assets", "index.html"),
        js_api=api,
        width=1000,
        height=680,
        min_size=(840, 560),
        background_color="#0f1115",
    )
    if reset_on_start:
        # `python main.py reset` -> open the window and run the reset flow
        def _auto_reset():
            threading.Thread(target=api.reset_bot, daemon=True).start()

        _WINDOW.events.loaded += _auto_reset
    webview.start(debug=debug)


if __name__ == "__main__":
    main()
