# discord-llm

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
git clone <your-repo-url>
cd discord-llm
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

Copy the two templates and fill in your values — both real files are
gitignored, so your token never reaches GitHub:

```sh
copy .env.example .env          # Windows  (macOS/Linux: cp .env.example .env)
copy config.example.json config.json
```

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

On startup the bot creates the `chats` category and the `new-chat` channel by
themselves — no channel IDs need to be configured.

## Run

```sh
python main.py
```

Type anything in `new-chat` to start: the channel gets a generated title and
every further message continues that conversation.

Wipe everything the bot owns (all conversation channels + stored messages)
and keep running:

```sh
python main.py reset
```

## Good to know

- Responses stream into the channel and are edited as they arrive; long
  answers continue across multiple messages.
- When something fails, the bot explains it in the channel (e.g. *"could not
  reach the LLM API — the api_url might be incorrect or the server isn't
  running"*) and keeps running; technical details land in the console.
- The bot's identity and writing voice live in `DEFAULT_SYSTEM_PROMPT` in
  `config.py` — set `system_prompt` in `config.json` to give it your own.
