"""Discord layer of the bot.

Discord itself is the interface: sending any message in `new-chat` turns that
very channel into a conversation, and every channel inside the `chats`
category is an independent LLM conversation identified by its Discord
channel ID.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import discord

import database as db
from config import Config
from llm import LLMClient, LLMError

log = logging.getLogger(__name__)

THINKING_TEXT = "Thinking..."
ERROR_TEXT = ("An unexpected error occurred while generating the response — "
              "the cause is in the bot's console output.")
EMPTY_RESPONSE_TEXT = "*(no response)*"

STREAM_SPLIT_LIMIT = 1900  # split long responses below Discord's 2000-char limit


def sanitize_channel_name(title: str) -> str:
    """Convert an LLM-generated title into a valid Discord channel name."""
    text = title.strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9_-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text[:90].strip("-_")


def split_for_discord(text: str):
    """Split text into (first_part, rest) so first_part fits one Discord message.

    Prefers a newline boundary so Markdown blocks stay reasonably intact.
    """
    if len(text) <= STREAM_SPLIT_LIMIT:
        return text, ""
    cut = text.rfind("\n", 0, STREAM_SPLIT_LIMIT)
    if cut < STREAM_SPLIT_LIMIT // 2:
        cut = STREAM_SPLIT_LIMIT
    return text[:cut], text[cut:].lstrip("\n")


def build_llm_messages(system_prompt: str, history):
    """Build the LLM message list: system prompt + completed history.

    User messages are prefixed with the sender's username so the model can
    tell participants apart; assistant messages keep the plain assistant role.
    """
    messages = [{"role": "system", "content": system_prompt}]
    for row in history:
        if row["role"] == "user":
            prefix = f"{row['username']}: " if row["username"] else ""
            messages.append({"role": "user", "content": prefix + row["content"]})
        elif row["role"] == "assistant":
            messages.append({"role": "assistant", "content": row["content"]})
    return messages


class _Generation:
    """Handle for the streaming task belonging to one conversation channel."""

    __slots__ = ("task",)

    def __init__(self, task):
        self.task = task


class ChatBot(discord.Client):
    def __init__(self, config: Config, reset_mode: bool = False):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: must be enabled in the dev portal
        super().__init__(intents=intents)
        self.config = config
        self.reset_mode = reset_mode
        self.llm = LLMClient(config)
        self._category_id = None
        self._generations = {}    # channel_id -> _Generation (one per channel, max)
        self._title_tasks = {}    # channel_id -> title generation Task
        self._channel_locks = {}  # channel_id -> Lock (serializes message handling)
        self._setup_lock = None
        self._reorder_lock = None
        self._setup_done = False

    # -- lifecycle ---------------------------------------------------------

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if self._setup_lock is None:
            self._setup_lock = asyncio.Lock()
        async with self._setup_lock:
            if self._setup_done:
                return
            try:
                if self.reset_mode:
                    await self._perform_reset()
                    self.reset_mode = False
                await self._ensure_structure()
                self._setup_done = True
                log.info(
                    "Structure ready: category '%s', entry channel '%s'",
                    self.config.category_name,
                    self.config.new_chat_name,
                )
            except Exception:
                log.exception("Startup setup failed (will retry on reconnect)")

    async def close(self):
        log.info("Shutting down, cancelling active generations...")
        tasks = [gen.task for gen in self._generations.values()]
        tasks += list(self._title_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()
        db.close()

    # -- events --------------------------------------------------------------

    async def on_message(self, message):
        if message.author.bot or message.webhook_id:
            return
        if not message.content.strip():
            return  # no text (attachments/images/files are ignored for now)
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.guild is None or channel.guild.id != self.config.server_id:
            return
        if self._category_id is None:
            return  # structure not set up yet

        async with self._lock_for(channel.id):
            current = self.get_channel(channel.id) or channel
            if (
                current.name == self.config.new_chat_name
                and current.category_id != self._category_id
                and not db.conversation_exists(current.id)
            ):
                try:
                    await self._convert_new_chat(current)
                except Exception:
                    log.exception("Failed to start a conversation from new-chat")
                    return
                await self._handle_conversation_message(current, message, first_message=True)
            elif current.category_id == self._category_id:
                await self._handle_conversation_message(current, message)
            else:
                log.debug("Ignoring message in unrelated channel %s (%s)", current.id, current.name)

    async def on_guild_channel_delete(self, channel):
        if channel.guild is None or channel.guild.id != self.config.server_id:
            return

        # Conversation channel deleted -> drop its generation and stored data.
        if db.conversation_exists(channel.id):
            gen = self._generations.get(channel.id)
            if gen and not gen.task.done():
                gen.task.cancel()
                await asyncio.wait({gen.task})
            title_task = self._title_tasks.pop(channel.id, None)
            if title_task and not title_task.done():
                title_task.cancel()
            db.delete_conversation(channel.id)
            log.info("Conversation %s deleted (channel removed)", channel.id)

        # Required structure deleted -> repair.
        if isinstance(channel, discord.CategoryChannel) and channel.name.lower() == self.config.category_name.lower():
            await self._repair_category(channel.guild)
        elif (
            isinstance(channel, discord.TextChannel)
            and channel.name == self.config.new_chat_name
            and channel.category_id is None
        ):
            await self._ensure_single_new_chat(channel.guild)

    # -- Discord structure -------------------------------------------------

    def _get_guild(self):
        guild = self.get_guild(self.config.server_id)
        if guild is None:
            raise RuntimeError(
                f"Guild {self.config.server_id} not found — check server_id in config.json"
            )
        return guild

    def _find_category(self, guild):
        return discord.utils.find(
            lambda c: c.name.lower() == self.config.category_name.lower(), guild.categories
        )

    async def _ensure_structure(self):
        guild = self._get_guild()
        category = self._find_category(guild)
        if category is None:
            category = await guild.create_category(self.config.category_name, reason="llm chat structure")
            log.info("Created category '%s'", self.config.category_name)
        self._category_id = category.id
        await self._ensure_single_new_chat(guild)
        self._remove_orphaned_conversations(guild)

    async def _ensure_single_new_chat(self, guild):
        candidates = [
            c for c in guild.text_channels
            if c.name == self.config.new_chat_name and c.category_id != self._category_id
        ]
        if not candidates:
            await guild.create_text_channel(self.config.new_chat_name, reason="llm chat entry point")
            log.info("Created '%s' channel", self.config.new_chat_name)
            return
        for extra in candidates[1:]:
            log.warning("Deleting duplicate '%s' channel %s", self.config.new_chat_name, extra.id)
            try:
                await extra.delete(reason="duplicate new-chat channel")
            except discord.HTTPException:
                log.exception("Failed to delete duplicate new-chat channel %s", extra.id)

    def _remove_orphaned_conversations(self, guild):
        """Remove DB conversations whose Discord channel no longer exists."""
        known_ids = {c.id for c in guild.channels}
        for conversation_id in db.all_conversation_ids():
            if int(conversation_id) not in known_ids:
                db.delete_conversation(conversation_id)
                log.info("Removed orphaned conversation %s", conversation_id)

    async def _repair_category(self, guild):
        category = await guild.create_category(self.config.category_name, reason="repair llm chat structure")
        self._category_id = category.id
        log.warning("Recreated '%s' category", self.config.category_name)
        for channel in guild.text_channels:
            if db.conversation_exists(channel.id) and channel.category_id is None:
                try:
                    await channel.edit(category=category, reason="repair llm chat structure")
                except discord.HTTPException:
                    log.exception("Failed to move channel %s back into the category", channel.id)

    async def _perform_reset(self):
        """Delete every conversation channel and all stored conversation data."""
        guild = self._get_guild()
        category = self._find_category(guild)
        if category is not None:
            channels = list(category.text_channels)
            for channel in channels:
                try:
                    await channel.delete(reason="reset")
                except discord.HTTPException:
                    log.exception("Failed to delete channel %s during reset", channel.id)
            log.info("Reset: deleted %d conversation channel(s)", len(channels))
        db.delete_all_conversations()
        log.info("Reset: cleared all conversations from the database")

    # -- conversation handling ------------------------------------------------

    def _lock_for(self, channel_id):
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = self._channel_locks[channel_id] = asyncio.Lock()
        return lock

    async def _convert_new_chat(self, channel):
        """Turn the new-chat channel itself into the conversation channel."""
        db.create_conversation(channel.id)
        category = channel.guild.get_channel(self._category_id)
        if not isinstance(category, discord.CategoryChannel):
            category = self._find_category(channel.guild)
        if not isinstance(category, discord.CategoryChannel):
            db.delete_conversation(channel.id)  # avoid a half-created conversation
            raise RuntimeError("chats category not found — cannot start conversation")
        try:
            await channel.edit(name=str(channel.id), category=category, reason="start conversation")
        except discord.HTTPException:
            db.delete_conversation(channel.id)  # avoid a half-created conversation
            raise
        # Replacement new-chat, created before any LLM work starts.
        try:
            await channel.guild.create_text_channel(self.config.new_chat_name, reason="replacement new-chat")
        except discord.HTTPException:
            log.exception("Failed to create replacement new-chat channel; restart the bot to repair")

    async def _handle_conversation_message(self, channel, message, first_message=False):
        db.create_conversation(channel.id)  # no-op if it already exists
        db.add_message(
            channel.id,
            role="user",
            content=message.content,
            username=message.author.name,
            user_id=str(message.author.id),
        )
        # Only one generation per channel: cancel the running one first.
        gen = self._generations.get(channel.id)
        if gen and not gen.task.done():
            gen.task.cancel()
            await asyncio.wait({gen.task})  # let it record the interrupted state
        if first_message:
            self._spawn_title_task(channel, message.content)
        task = asyncio.create_task(self._run_generation(channel, channel.id))
        self._generations[channel.id] = _Generation(task)

        def _cleanup(_done_task, channel_id=channel.id, created_task=task):
            if self._generations.get(channel_id) is created_task:
                self._generations.pop(channel_id, None)

        task.add_done_callback(_cleanup)
        log.info("Generation started for channel %s", channel.id)
        # ChatGPT-style ordering: the active conversation floats to the top.
        await self._move_channel_to_top(channel)

    async def _move_channel_to_top(self, channel):
        """ChatGPT-style ordering: the most recently active conversation jumps
        to the top of the chats category. Skipped when already on top."""
        if self._reorder_lock is None:
            self._reorder_lock = asyncio.Lock()
        try:
            category = channel.guild.get_channel(self._category_id)
            if not isinstance(category, discord.CategoryChannel):
                return
            children = sorted(category.text_channels, key=lambda c: (c.position, c.id))
            if children and children[0].id == channel.id:
                return  # already the most recent — nothing to do
            async with self._reorder_lock:
                # py-cord turns this into a guild-wide text-channel position
                # normalization that preserves the relative order everywhere else.
                await channel.edit(position=0, reason="most recent chat moves to top")
        except discord.HTTPException:
            log.exception("Failed to move channel %s to the top of the category", channel.id)
        except Exception:
            log.exception("Unexpected error while reordering channel %s", channel.id)

    def _spawn_title_task(self, channel, first_message):
        """Title generation runs concurrently with the first response."""
        task = asyncio.create_task(self._generate_title(channel, first_message))
        self._title_tasks[channel.id] = task

        def _cleanup(_done_task, channel_id=channel.id):
            self._title_tasks.pop(channel_id, None)

        task.add_done_callback(_cleanup)

    async def _generate_title(self, channel, first_message):
        """Rename the channel to an LLM title; keep the channel ID on failure."""
        try:
            name = sanitize_channel_name(await self.llm.generate_title(first_message))
            if not name:
                return
            await channel.edit(name=name, reason="conversation title")
            log.info("Channel %s renamed to '%s'", channel.id, name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Title generation failed for channel %s — keeping channel id as name", channel.id)

    # -- streaming -----------------------------------------------------------

    async def _run_generation(self, channel, channel_id):
        """Stream an LLM response into the channel via throttled edits."""
        status_message = None
        current_message = None
        parts = []    # text already pushed to Discord
        buffer = ""   # text of the current Discord message not yet flushed
        recorded = False
        first_edit_done = False
        last_edit = time.monotonic()
        content_flushed = False  # True when current_message already shows `buffer`
        try:
            status_message = await channel.send(THINKING_TEXT)
            current_message = status_message
            history = db.get_completed_messages(channel_id)
            llm_messages = build_llm_messages(self.config.system_prompt, history)
            interval = self.config.stream_edit_interval
            async for delta in self.llm.stream_chat(llm_messages):
                buffer += delta
                content_flushed = False
                # Finalize the current message and continue in new ones when too long.
                while len(buffer) >= STREAM_SPLIT_LIMIT:
                    part, rest = split_for_discord(buffer)
                    if current_message is None:
                        await channel.send(part)
                    else:
                        await self._edit_message(current_message, part)
                        current_message = None
                    parts.append(part)
                    buffer = rest
                if buffer and current_message is None:
                    current_message = await channel.send(buffer)
                    content_flushed = True
                elif current_message is not None and buffer:
                    now = time.monotonic()
                    if not first_edit_done or now - last_edit >= interval:
                        await self._edit_message(current_message, buffer)
                        first_edit_done = True
                        last_edit = time.monotonic()
                        content_flushed = True
            # Stream finished: final flush (no artificial throttle delay here).
            if buffer and current_message is None:
                current_message = await channel.send(buffer)
                content_flushed = True
            full_text = "".join(parts) + buffer
            if full_text:
                # Record before the final edit so a late cancellation can't lose it.
                db.add_message(channel_id, role="assistant", content=full_text)
                recorded = True
                if buffer and current_message is not None and not content_flushed:
                    await self._edit_message(current_message, buffer)
            elif status_message is not None:
                await self._edit_message(status_message, EMPTY_RESPONSE_TEXT)
        except asyncio.CancelledError:
            partial = "".join(parts) + buffer
            if partial and not recorded:
                db.add_message(channel_id, role="assistant", content=partial, state="interrupted")
            raise  # displayed partial text stays in Discord untouched
        except Exception as exc:
            # LLMError carries a plain-language explanation (what happened and
            # what to check); anything else gets the generic text. The full
            # traceback always goes to the console log.
            friendly = str(exc) if isinstance(exc, LLMError) else ERROR_TEXT
            log.exception("LLM generation failed for channel %s", channel_id)
            if not recorded:
                partial = "".join(parts) + buffer
                if partial:
                    db.add_message(channel_id, role="assistant", content=partial, state="interrupted")
            await self._show_error(channel, current_message or status_message, friendly)

    async def _edit_message(self, message, content):
        try:
            await message.edit(content=content)
        except discord.NotFound:
            log.warning("Message %s was deleted while streaming", message.id)
        except discord.HTTPException:
            log.exception("Failed to edit message %s", message.id)

    async def _show_error(self, channel, target, text):
        """Show an error explanation in place of the status/partial message."""
        try:
            if target is not None:
                await target.edit(content=text)
            else:
                await channel.send(text)
        except discord.HTTPException:
            log.exception("Failed to show the error message in channel %s", channel.id)
