"""Isolated client for the configured OpenAI-compatible chat API.

The Discord layer never talks to the API directly; changing provider, URL,
key or model only touches config.json.
"""

from __future__ import annotations

import re

from openai import AsyncOpenAI, BadRequestError, UnprocessableEntityError

TITLE_SYSTEM_PROMPT = (
    "Generate a short chat title for the user's message. Reply with ONLY the title text "
    "and nothing else: 3-4 words maximum, plain text, no quotes, no markdown "
    "(no **, #, ###, `, |), no emoji, no punctuation at the end, no explanation, "
    "no labels like 'Title:'. Capture what the conversation is about."
)

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class _ThinkFilter:
    """Suppress inline <think>...</think> blocks some servers stream in content.

    Reasoning delivered via a separate field (e.g. delta.reasoning_content) is
    never read in the first place; this filter only guards against servers that
    inline it as normal content.
    """

    def __init__(self):
        self._inside = False
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        return self._drain(final=False)

    def finish(self) -> str:
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        out = []
        while True:
            if self._inside:
                idx = self._buf.find(_THINK_CLOSE)
                if idx == -1:
                    if final:
                        self._buf = ""  # unclosed think block: discard
                    break
                self._buf = self._buf[idx + len(_THINK_CLOSE):]
                self._inside = False
            else:
                idx = self._buf.find(_THINK_OPEN)
                if idx == -1:
                    if final:
                        out.append(self._buf)
                        self._buf = ""
                    else:
                        hold = len(_THINK_OPEN) - 1  # a tag may be split across chunks
                        if len(self._buf) > hold:
                            out.append(self._buf[:-hold])
                            self._buf = self._buf[-hold:]
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_THINK_OPEN):]
                self._inside = True
        return "".join(out)


class LLMClient:
    def __init__(self, config):
        self._model = config.model
        self._client = AsyncOpenAI(base_url=config.api_url, api_key=config.api_key)

    async def stream_chat(self, messages):
        """Yield normal answer content chunks as they arrive.

        Internal reasoning output is intentionally ignored — only the actual
        answer is ever yielded.
        """
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
        )
        try:
            think_filter = _ThinkFilter()
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    text = think_filter.feed(delta.content)
                    if text:
                        yield text
            tail = think_filter.finish()
            if tail:
                yield tail
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass

    async def generate_title(self, user_message: str) -> str:
        """Generate a short conversation title for the first user message."""
        messages = [
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message[:1000]},
        ]
        try:
            # Disable reasoning where the chat template supports it (fast titles);
            # reasoning models otherwise spend their whole budget thinking first.
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=200,
                temperature=0.4,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except (BadRequestError, UnprocessableEntityError):
            # Server rejected the hint (e.g. strict OpenAI-compatible APIs) —
            # retry without it, with a budget large enough to think first.
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=2000,
                temperature=0.4,
            )
        if not response.choices:
            return ""
        content = response.choices[0].message.content or ""
        # strip reasoning blocks some models inline before the actual answer
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = content.split("<think>")[0]
        return content.strip()
