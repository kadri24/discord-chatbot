# discord-llm

A minimal Discord bot that uses **Discord itself as the interface** to an
OpenAI-compatible LLM API. There are no commands, no dashboard and no special
syntax — you just chat.

- Every text channel inside the **`chats`** category is one independent
  conversation with its own persistent context window.
- The **`new-chat`** channel (outside the category) is the entry point: the
  first message you send there turns that channel into a new conversation.
- Multiple users can chat in the same channel; they share that conversation's
  context and the model is told who said what.

## Architecture

```
Discord (py-cord)                LLM API (OpenAI-compatible)
┌─────────────────────────┐      ┌──────────────────────┐
│ new-chat ── message ──► │      │                      │
│   │ convert channel,    │      │  streaming chat      │
│   │ create replacement  │ ───► │  + title generation  │
│ chats/…  ── messages ─► │ ◄─── │  (async, concurrent) │
│   │ per-channel context │      └──────────────────────┘
│   │ streamed via edits  │      ┌──────────────────────┐
└─────────────────────────┘      │ SQLite (data/chat.db)│
                                 └──────────────────────┘
```

| File | Responsibility |
| --- | --- |
| `main.py` | Entry point; parses the optional `reset` argument |
| `bot.py` | Discord events, structure repair, conversation handling, streaming edits |
| `llm.py` | Isolated async OpenAI-compatible client (streaming + titles) |
| `database.py` | SQLite persistence (conversations + messages) |
| `config.py` | Loads/validates `.env` and `config.json` |
| `config.json` | All settings (server, API, model, names) — created from `config.example.json` |
| `.env` | `DISCORD_TOKEN` only — created from `.env.example` |
| `data/chat.db` | SQLite database, created automatically on first run |

## Requirements

- Python 3.10 or newer
- A Discord bot account
- An OpenAI-compatible chat completions endpoint with streaming support
  (llama.cpp server, LM Studio, Ollama's OpenAI endpoint, OpenAI itself, …)

## Installation

```sh
python -m venv venv
venv\Scripts\activate        # Windows   (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt
```

## Discord bot setup

1. Create an application at the
   [Discord Developer Portal](https://discord.com/developers/applications)
   and add a **bot** to it.
2. Enable the privileged **MESSAGE CONTENT INTENT** for the bot
   (Bot → Privileged Gateway Intents). This is required — without it the bot
   cannot read messages. No other privileged intents are needed.
3. Invite the bot to your private server with these permissions:
   - View Channels (`1024`)
   - Send Messages (`2048`)
   - Manage Channels (`16`) — needed to create/move/rename/delete channels

   Quick invite link: `https://discord.com/api/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=3088&scope=bot`
4. Enable **Developer Mode** in Discord (Settings → Advanced) and copy your
   **server ID** (right-click the server icon → Copy Server ID).

## Configuration

Copy the two templates and fill in your values. Both real files are
gitignored, so your token and IDs never reach GitHub:

```sh
cp .env.example .env                   # Windows cmd: copy .env.example .env
cp config.example.json config.json
```

**`.env`** (the only secret here):

```
DISCORD_TOKEN=your-bot-token
```

**`config.json`** (your copy of `config.example.json` with values filled in):

| Option | Description |
| --- | --- |
| `server_id` | Your Discord server (guild) ID — **required** |
| `api_url` | OpenAI-compatible base URL, e.g. `http://localhost:8080/v1` — **required** |
| `api_key` | API key for the endpoint (any non-empty value if none is needed) — **required** |
| `model` | Model name sent to the API (e.g. the GGUF model loaded in llama.cpp/LM Studio, `qwen2.5:7b` for Ollama, `gpt-4o-mini` for OpenAI) — **required** |
| `stream_edit_interval` | Seconds between Discord edits while streaming (default `5`) |
| `category_name` | Category holding conversations (default `chats`) |
| `new_chat_name` | Entry channel name (default `new-chat`) |
| `database_path` | SQLite file location (default `data/chat.db`) |
| `system_prompt` | Optional override of the built-in system prompt (identity + writing voice — see [Personality](#personality)) |

On startup the bot finds the `chats` category and the `new-chat` channel in
the configured server by name and creates either of them if missing. No
channel IDs ever need to be configured.

## Running

```sh
python main.py
```

Reset everything the bot owns (deletes **all** conversation channels in the
`chats` category **and** all stored conversations/messages, repairs the
structure, then keeps running normally):

```sh
python main.py reset
```

## How new conversations work

1. Type any message in `new-chat`.
2. That exact channel becomes the conversation: it is moved into `chats`,
   temporarily renamed to its Discord channel ID (the conversation ID), and a
   fresh `new-chat` is created immediately — before any LLM call is made.
3. The bot concurrently starts two LLM requests: one for a short title
   (~3–4 words, e.g. `1-plus-1-answer`) and one for the actual response.
   When the title is ready the channel is renamed; if title generation fails,
   the channel simply keeps its numeric ID as its name.
4. Every further message in that channel is sent to the LLM automatically.
   Bot/webhook messages are ignored; only text is used (attachments are
   ignored for now).
5. **ChatGPT-style ordering**: the most recently active conversation jumps to
   the top of the `chats` category, so the sidebar always shows your newest
   chat first. Channels that are already on top are not touched, and the
   relative order of all other channels is preserved.

## Persistence

All conversations and messages are stored in SQLite (`data/chat.db`), so
history survives restarts. Each stored message keeps the Discord user ID,
username, content, role, timestamp, and conversation/channel ID. Messages
whose channel was deleted on Discord are removed at startup (orphans).
Deleting a conversation channel on Discord deletes its stored conversation.

## Streaming behavior

- Every generation first sends `Thinking...` into the channel.
- As soon as answer content arrives, that **same message** is edited with the
  partial response; edits are throttled to one per `stream_edit_interval`
  seconds (default 5). The final edit happens immediately when generation
  completes.
- Responses longer than one Discord message automatically continue in
  additional messages, splitting at Markdown-friendly (newline) boundaries.
  Nothing is truncated.
- The bot never exposes model reasoning — only the final answer text.

## Cancellation

Only one generation runs per channel. If a new user message arrives while the
assistant is still generating, the previous request is cancelled, the partial
text already shown in Discord is kept (but excluded from future context as a
completed assistant message), the new message is stored, and a fresh
generation starts immediately. Different channels generate concurrently and do
not affect each other.

## Personality

The default system prompt (`DEFAULT_SYSTEM_PROMPT` in `config.py`) gives the
bot a general identity — an AI language model that honestly says it has no
web access — plus a full writing voice (making things click,
narrative-before-reference, opener rules, playfulness, emoji use, bullet
brevity), with formatting adapted to what Discord can actually render:

- kept: `**bold**`, `>` blockquotes, `*` bullets, `###` headers, ``` fenced
  code blocks with language tags, ```text diagrams, emoji
- replaced: markdown tables (not renderable on Discord) → compact
  `**item** — description` bullet lines; LaTeX math → plain unicode (x², ≈)

Every user message is labeled with the sender's Discord username so the model
knows who said what, and it treats everyone with the same personality. Want a
different persona? Set `system_prompt` in `config.json` to replace the
built-in one entirely.

## Error handling

If generation fails, the `Thinking...`/response message is edited to show
`An error occurred while generating the response.` and the exception is logged
to the console. The bot keeps running.

## Reset mode details

`python main.py reset` operates **only** on the structures this bot owns:

1. Deletes all text channels inside the `chats` category.
2. Clears all conversations/messages from SQLite.
3. Preserves (or recreates) the `chats` category.
4. Ensures exactly one `new-chat` channel exists outside the category.
5. Continues running the bot normally afterwards.

Unrelated Discord channels and categories are never touched.
