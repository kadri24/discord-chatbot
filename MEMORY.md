# MEMORY.md

Notes for future AI/coding-agent sessions working in this repository. Read this
before changing anything, and keep it updated when behavior changes.

## What this project is

A minimal Discord bot (py-cord) that uses Discord itself as the interface to an
OpenAI-compatible LLM API. There are **no commands, no slash commands, no
dashboard, no per-user permissions**. Each text channel inside the `chats`
category is one independent conversation with its own context window, persisted
in SQLite. Users start conversations by typing in `new-chat` (outside the
category). The first message converts that exact channel into the conversation.

Run: `python main.py` — Reset: `python main.py reset`.

## Architecture / data flow

```
on_message ──► per-channel asyncio.Lock
                 ├─ channel named new-chat (outside chats category)?
                 │    └─ convert: SQLite row → move+rename channel to its ID →
                 │       create replacement new-chat → spawn title task +
                 │       generation task concurrently
                 └─ channel inside chats category?
                      └─ store user message → cancel running generation (if any),
                         wait for it → start new generation task
generation task: send "Thinking..." → stream from LLM → throttled edits
                 → record 'complete' assistant row (interrupted partials on cancel)
```

- `llm.py` is the only module that talks to the LLM API (AsyncOpenAI client,
  base_url/api_key/model from config).
- `database.py` is the only module that touches SQLite (sync sqlite3, tiny
  calls, guarded by a threading.Lock; safe since all calls run on the event
  loop thread).
- `bot.py` owns all Discord logic. `main.py` wires config + db + bot and
  handles the `reset` argv.

## File responsibilities

| File | Responsibility |
| --- | --- |
| `main.py` | Entry point; `reset` in argv → reset mode; logging setup; `bot.run(token)` |
| `bot.py` | py-cord `Client` subclass: events, structure ensure/repair/reset, conversation handling, streaming edits, `sanitize_channel_name`, `split_for_discord`, `build_llm_messages` |
| `llm.py` | `LLMClient.stream_chat(messages)` (async generator of content deltas, filters inline `<think>` blocks) + `generate_title(user_message)` (disables thinking via `chat_template_kwargs`, plain fallback) |
| `database.py` | Schema init, conversation/message CRUD, `get_completed_messages` (only `state='complete'`), orphan/delete helpers |
| `config.py` | `load_config()` → `Config` dataclass; validates; raises `SystemExit` with readable errors. Also holds `DEFAULT_SYSTEM_PROMPT` — the bot's default identity and writing voice, adapted to Discord's rendering |
| `config.json` | server_id, api_url, api_key, model, stream_edit_interval, category_name, new_chat_name, database_path, optional system_prompt |
| `.env` | `DISCORD_TOKEN` only (never put it in config.json) |
| `data/chat.db` | SQLite DB, auto-created by `database.init()` |

## Discord channel lifecycle

1. On ready (guarded by `_setup_done`, retried on reconnect): find/create the
   `chats` category (name matched case-insensitively), ensure **exactly one**
   `new-chat` text channel outside the category (duplicates are deleted),
   remove DB conversations whose channel no longer exists.
2. New conversation: a non-bot, non-webhook, non-empty text message in
   `new-chat` (outside category, no DB row yet) converts **that same channel**:
   - create SQLite conversation keyed by the Discord channel ID (the
     conversation ID — never a UUID),
   - `channel.edit(name=str(channel.id), category=chats)`,
   - immediately create a replacement `new-chat` outside the category,
   - then store the message and spawn `_generate_title` + `_run_generation`
     concurrently (response never waits for the title).
3. Title task: LLM call → `sanitize_channel_name` (lowercase, `-` for spaces,
   strip `[^a-z0-9_-]`, collapse dashes, max 90) → rename channel. Any failure
   just logs; the channel keeps its numeric ID as name.
4. Normal messages in category channels: stored, then generation (see below).
   A channel under the category without a DB row (created manually) gets one
   lazily on its first message. After the generation task is spawned, the
   channel is moved to the **top of the category** (ChatGPT-style recency
   ordering, `_move_channel_to_top`): skipped when already first, otherwise
   `channel.edit(position=0)` — py-cord turns that into a guild-wide
   text-channel bulk reorder that preserves the relative order everywhere
   else. Guarded by a single `_reorder_lock` so concurrent moves serialize.
5. `on_guild_channel_delete`: if the channel is a stored conversation → cancel
   its generation task, await it, cancel title task, delete DB rows. If the
   category was deleted → recreate it and move stored conversation channels
   back in. If `new-chat` was deleted → recreate it (unless another one
   exists outside the category).
6. Normal startup never deletes valid conversation channels or histories;
   reset mode does (see below).

## SQLite schema / behavior

```sql
conversations(channel_id TEXT PRIMARY KEY, created_at TEXT)
messages(id INTEGER PK AUTOINCREMENT, channel_id TEXT, role TEXT,
         username TEXT, user_id TEXT, content TEXT,
         state TEXT DEFAULT 'complete', created_at TEXT)
```

- `state` is `'complete'` or `'interrupted'`. **Only `'complete'` messages are
  ever fed to the LLM** (`get_completed_messages`). Interrupted partials are
  kept purely for bookkeeping.
- Message insert order = chronological order (autoincrement id).
- User messages are rendered to the LLM as `"username: content"`; assistant
  messages keep the plain `assistant` role.
- `delete_conversation` removes the conversation and its messages;
  `delete_all_conversations` is for reset.

## Streaming behavior

- Every generation sends `Thinking...` first (regardless of model), then edits
  that same message as soon as content arrives (first edit is immediate,
  replacing `Thinking...`), then at most one edit per
  `stream_edit_interval` seconds (default 5) — checked via `time.monotonic()`.
  The final flush edit happens immediately when the stream ends (no artificial
  5s wait).
- Long responses: the current message is finalized and the response continues
  in new messages whenever the buffer reaches 1900 chars
  (`STREAM_SPLIT_LIMIT`, Discord limit is 2000). `split_for_discord` prefers
  newline boundaries. The DB always stores the full concatenated text.
- Only `delta.content` is ever used — model reasoning is never exposed.
- Empty responses edit the status message to `*(no response)*`.
- `Thinking...` left over after a cancellation is intentional (partial output
  is kept, never deleted).

## Cancellation semantics

- One generation per channel, tracked in `self._generations[channel_id]`.
  Cancellation is strictly per channel — channels generate concurrently.
- New message in a channel: acquire that channel's lock → store message →
  `task.cancel()` → `await asyncio.wait({task})` (never raises; ensures the
  old task recorded its partial as `'interrupted'` before the new generation
  builds context) → start new task. Because each message is stored before
  generation starts, chronological order is always preserved.
- The assistant row is recorded as `'complete'` *before* the final Discord
  edit, so a cancellation in that tiny window cannot lose a finished answer
  (the `recorded` flag prevents double recording).
- The cancelled task's `except asyncio.CancelledError` records the partial
  (if any, if not already recorded) and re-raises; Discord messages are not
  touched further.

## Configuration

- `.env`: `DISCORD_TOKEN` (validated, required).
- `config.json` (all required unless noted): `server_id` (int-parsable),
  `api_url` (OpenAI-compatible base URL **including** `/v1`-style suffix),
  `api_key` (any non-empty value for local servers), `model`, plus optional
  `stream_edit_interval` (float > 0, default 5), `category_name` (default
  `chats`), `new_chat_name` (default `new-chat`; normalized to lowercase with
  hyphens because Discord lowercases text channel names), `database_path`
  (default `data/chat.db`), `system_prompt` (optional override; defaults to
  the built-in persona in `config.py`).
- Validation failures raise `SystemExit` listing every problem.

## Important invariants (do not break)

1. One `new-chat` entry point, always outside the `chats` category.
2. The first message converts that same channel; conversation ID = channel ID.
3. Replacement `new-chat` is created before any LLM work starts.
4. Titles are generated afterwards by the same model; failure keeps the ID.
5. Per-channel independent context; users in a channel share it; usernames
   identify speakers; no global context ever.
6. SQLite persists everything across restarts; startup only removes DB rows
   whose channel is gone.
7. Per-channel generation: new message cancels the active generation for that
   channel only; partial output is kept in Discord but stored as
   `'interrupted'` and never used as context.
8. Streamed edits throttled to `stream_edit_interval`; final edit immediate;
   long responses continue across multiple messages; never truncated.
9. No bot commands, no replies (`message.reply` is never used), no reasoning
   exposure, no context trimming/summarization/token counting.
10. Reset mode deletes only chatbot-owned structures: text channels in the
    category + all DB conversations, then repairs structure and keeps running.
11. The most recently active conversation channel is moved to the top of the
    `chats` category on every user message (ChatGPT-style recency ordering).

## Intentionally unsupported (do not add without being asked)

Slash commands, dashboards/web UIs, authentication, moderation, model
selectors, Docker, message edit/delete reconciliation, attachments/images as
LLM input, context trimming/summarization, per-user permissions, title
regeneration, voice/forum/thread support (threads are ignored by the
`isinstance(TextChannel)` check).

## Commands to run / verify

```sh
pip install -r requirements.txt
python -m py_compile main.py bot.py llm.py database.py config.py
python main.py          # normal start (needs .env + config.json filled in)
python main.py reset    # reset conversations, then start
```

Startup logs: `Logged in as ...`, `Structure ready: category 'chats', entry
channel 'new-chat'`, per-generation `Generation started for channel ...`,
title renames, and any exceptions.

## Gotchas

- The default system prompt (`DEFAULT_SYSTEM_PROMPT` in `config.py`) defines
  the bot's identity and writing voice. Identity rules: it introduces itself
  as an AI language model and honestly says it has no web access (so it never
  pretends to browse). Formatting is adapted to what Discord renders:
  - tables → compact `**item** — description` bullet lines
  - LaTeX/KaTeX → plain unicode math
  - bold/blockquotes/bullets/### headers/code fences/```text diagrams/emoji
    kept (all render on Discord)
  - a Discord context block tells the model that username prefixes identify
    speakers and that everyone gets the same personality (NAME USAGE rules)
- Channel titles come from `TITLE_SYSTEM_PROMPT` in `llm.py`; the
  channel-name sanitizer lowercases the result.

- py-cord **2.8.x** (`Client.run`) does no logging setup and takes no
  `log_handler` kwarg — `main.py` configures logging via `logging.basicConfig`.
  py-cord 2.8 requires Python >= 3.10; requirements.txt pins `py-cord>=2.8.0`.
- The bot needs the **MESSAGE CONTENT INTENT** enabled in the Discord dev
  portal, plus Manage Channels / View Channels / Send Messages permissions.
- py-cord is used as `discord` (`import discord`); events are plain method
  overrides on the `ChatBot` subclass; `on_guild_channel_delete` receives the
  channel object directly.
- Streaming keeps a `content_flushed` flag: the final edit is skipped when the
  current message already shows the finished buffer (avoids no-op edits), and
  the final edit is exempt from the interval throttle.
- Reasoning models: reasoning arriving as `delta.reasoning_content` /
  `message.reasoning_content` is never read; inline `<think>...</think>` in
  content is suppressed by `_ThinkFilter` in llm.py (stateful, handles tags
  split across chunks). Invariant: reasoning must never reach Discord.
- Title generation sends `extra_body={"chat_template_kwargs":
  {"enable_thinking": False}}` (llama.cpp-style chat templates ignore reasoning
  per request); if the server rejects it (BadRequest/Unprocessable), llm.py
  retries without it using `max_tokens=2000` so reasoning models can finish
  thinking before the title. Without one of these, reasoning-only budgets
  return empty content and the channel silently keeps its numeric name.
- If your LLM server's UI shows an OpenAI-compatible base URL, that is the one
  to put in config.json — it can differ from the server process's internal
  `--port`.
- `asyncio.Lock` objects are created lazily inside async contexts (never in
  `__init__`) to avoid loop-binding issues.
- `await asyncio.wait({task})` is used to await cancelled generation tasks —
  awaiting a cancelled task directly would raise `CancelledError` in the
  caller.
- `channel.edit(name=..., category=...)` both renames and moves a channel.
- The openai client's `base_url` must include the version path (e.g.
  `http://localhost:8080/v1` for llama.cpp, `http://localhost:1234/v1` for
  LM Studio).
- The first content edit intentionally bypasses the 5s throttle so
  `Thinking...` is replaced immediately; subsequent edits are throttled.
