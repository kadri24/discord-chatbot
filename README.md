# discord-chatbot

A minimal Discord bot that turns your server into a ChatGPT-style chat with
any OpenAI-compatible LLM API — local servers like llama.cpp, LM Studio or
Ollama, or hosted providers. No commands, no dashboard: you just chat.

- Every channel inside the **`chats`** category is one conversation with its
  own persistent memory.
- The **`new-chat`** channel is the entry point: send a message there and that
  channel becomes the conversation, with a fresh `new-chat` created for the
  next one.
- Several people can talk in one channel — the bot knows who said what and
  keeps the same personality for everyone.
- The most recently used conversation floats to the top of the category.
- Conversations are stored in SQLite and survive restarts.

## Requirements

- Python **3.10 or newer**
- A Discord bot account (created in the
  [Developer Portal](https://discord.com/developers/applications))
- An OpenAI-compatible chat API with streaming (llama.cpp server, LM Studio,
  Ollama, OpenAI, …)

## Setup

### 1. Install

```sh
git clone https://github.com/kadri24/discord-chatbot.git
cd discord-chatbot
python -m venv venv
venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
```

### 2. Create the Discord bot

1. In the [Developer Portal](https://discord.com/developers/applications),
   create an application and add a **Bot** to it.
2. Enable **MESSAGE CONTENT INTENT** (Bot → Privileged Gateway Intents) —
   required, without it the bot cannot read messages.
3. Invite it to your server with permission to view/send messages and manage
   channels (it creates, moves and renames the chat channels):

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=3088&scope=bot
   ```

4. Enable **Developer Mode** in Discord (Settings → Advanced), then
   right-click your server icon → **Copy Server ID**.

### 3. Configure

Nothing to copy by hand — the manager window creates both `config.json` and
`.env` on first start and shows you exactly what is missing:

```sh
python main.py
```

Fill in the fields and click **Save**.

**`.env`** — your bot token (Portal → Your App → Bot → Reset Token):

```
DISCORD_TOKEN=your-bot-token
```

**`config.json`**:

| Option | What to put there |
| --- | --- |
| `server_id` | The server ID you copied — **required** |
| `api_url` | Base URL of the API, e.g. `http://localhost:8080/v1` — **required** |
| `api_key` | API key (any non-empty value if your server needs none) — **required** |
| `model` | Model name the API expects — **required** |
| `stream_edit_interval` | Seconds between Discord edits while streaming (default `5`) |
| `category_name` | Category holding conversations (default `chats`) |
| `new_chat_name` | Entry channel name (default `new-chat`) |
| `database_path` | SQLite file location (default `data/chat.db`) |
| `system_prompt` | Optional: replace the built-in persona with your own |

Then hit **Start bot**. Both files are gitignored, so your token and IDs
never reach GitHub. The bot also creates the `chats` category and the
`new-chat` channel by themselves — no channel IDs need to be configured.

## Run

### Manager window (default)

```sh
python main.py
```

A small desktop window to edit the configuration, start and stop the bot,
watch its console output, and reset conversations with a click. On first
start it creates `config.json` and `.env` for you and points you at whatever
is still missing. `python gui.py` opens the same window; add `--debug` for
WebView devtools. If the GUI can't start (no display), it falls back to
console mode.

### Console

For headless machines or when you prefer the terminal:

```sh
python main.py --console
```

Type anything in `new-chat` to start: the channel gets a generated title and
every further message continues that conversation.

Wipe everything the bot owns (all conversation channels + stored messages)
and keep running:

```sh
python main.py reset              # opens the window and resets
python main.py --console reset    # headless: reset, then run in the console
```

## Good to know

- Responses stream into the channel and are edited as they arrive; long
  answers continue across multiple messages.
- When something fails, the bot explains it in the channel (e.g. *"could not
  reach the LLM API — the api_url might be incorrect or the server isn't
  running"*) and keeps running; technical details land in the console.
- The bot's identity and writing voice live in `DEFAULT_SYSTEM_PROMPT` in
  `config.py` — set `system_prompt` in `config.json` to give it your own.

## License

[MIT-0](LICENSE) (MIT No Attribution) — free to use, modify and share; no
attribution required.
