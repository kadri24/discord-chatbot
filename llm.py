"""Isolated client for the configured OpenAI-compatible chat API.

The Discord layer never talks to the API directly; changing provider, URL,
key or model only touches config.json.
"""

from __future__ import annotations

import re

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

TITLE_SYSTEM_PROMPT = (
    "Generate a short chat title for the user's message. Reply with ONLY the title text "
    "and nothing else: 3-4 words maximum, plain text, no quotes, no markdown "
    "(no **, #, ###, `, |), no emoji, no punctuation at the end, no explanation, "
    "no labels like 'Title:'. Capture what the conversation is about."
)

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class LLMError(Exception):
    """LLM API failure whose message is a plain-language explanation of what
    happened and what to check, safe to show to users in Discord."""


def _error_detail(exc: Exception) -> str:
    """One-line server-provided detail from an API exception (may be '')."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])
        else:
            detail = str(body)
    else:
        detail = getattr(exc, "message", None) or str(exc)
    return " ".join(detail.split())[:300]


def _friendly_api_error(exc: Exception) -> str:
    """Turn an LLM API exception into a plain-language explanation: what
    happened and what to do about it."""
    suffix = f" Server said: {_error_detail(exc)}"
    if isinstance(exc, APITimeoutError):
        return ("The LLM API did not answer in time — the server might be overloaded "
                "or still loading the model. Try again in a moment.")
    if isinstance(exc, APIConnectionError):
        return ("Could not reach the LLM API — the api_url in config.json might be "
                "incorrect, or the server isn't running. Check that the server is up "
                "and that api_url includes the version path (e.g. http://localhost:8080/v1).")
    if isinstance(exc, AuthenticationError):
        return ("The LLM API rejected the api_key from config.json (401). Fix the key "
                "— local servers usually accept any non-empty value — and try again.")
    if isinstance(exc, NotFoundError):
        return ("The LLM API returned 'not found' (404). Usually the model name in "
                "config.json doesn't exist on that server, or api_url is missing the "
                "version path (e.g. /v1)." + suffix)
    if isinstance(exc, PermissionDeniedError):
        return ("The LLM API denied the request (403) — the api_key may not have "
                "access to this model or endpoint." + suffix)
    if isinstance(exc, RateLimitError):
        return ("The LLM API is rate limiting requests (429). Wait a bit and try "
                "again, or check the quota and usage limits of the provider.")
    if isinstance(exc, BadRequestError):
        return ("The LLM API rejected the request as invalid (400). The model name in "
                "config.json might not exist on the server, or a parameter isn't "
                "supported." + suffix)
    if isinstance(exc, UnprocessableEntityError):
        return ("The LLM API could not process the request (422) — often a parameter "
                "or request the model/template can't handle." + suffix)
    if isinstance(exc, InternalServerError):
        return ("The LLM API failed internally (5xx) — the server may be overloaded "
                "or crashed. Check the server's logs and try again." + suffix)
    return "The LLM API request failed unexpectedly." + suffix


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
        try:
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
        except OpenAIError as exc:
            # Includes failures mid-stream (timeout/disconnect — the SDK wraps
            # those into APITimeoutError/APIConnectionError); the partial text
            # already shown in Discord stays untouched.
            raise LLMError(_friendly_api_error(exc)) from exc

    async def generate_title(self, user_message: str) -> str:
        """Generate a short conversation title for the first user message."""
        messages = [
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message[:1000]},
        ]
        try:
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
        except OpenAIError as exc:
            raise LLMError(_friendly_api_error(exc)) from exc
        if not response.choices:
            return ""
        content = response.choices[0].message.content or ""
        # strip reasoning blocks some models inline before the actual answer
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = content.split("<think>")[0]
        return content.strip()
