"""Pipecat LLM service for the a1mobile hackathon model gateway.

The gateway speaks the OpenAI *Responses* shape but is a reduced dialect. It
rejects three things Pipecat's stock ``OpenAIResponsesHttpLLMService`` always
sends:

* ``stream``  — no server-sent events; one JSON body per call
* ``store``   — hardcoded to ``False`` upstream
* ``instructions`` — the system prompt must ride inside ``input`` instead

So we keep Pipecat's context/adapter machinery and replace only the transport
half of ``_process_context`` with a single request/response round trip.

Because there is no token streaming, time-to-first-audio is the *whole*
completion time. The system prompt is written to keep turns to one or two
sentences, which is what keeps this usable on a live call.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from loguru import logger
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.responses.llm import OpenAIResponsesHttpLLMService
from pipecat.services.settings import assert_given

# Params the gateway rejects outright.
_UNSUPPORTED = ("stream", "store", "include")

# Tool results are presented as observed fact rather than as a tool message,
# because this gateway drops the call_id that would otherwise bind them.
_OBSERVED = "TOOL RESULT (already executed — do not call it again): "

# Anything going into the context gets scrubbed first. A tool result carrying
# control characters or a wall of page text is enough for the gateway to reject
# the request — and because it stays in the history, it then rejects every turn
# after it, which the caller hears as the same apology over and over.
MAX_TOOL_RESULT = 1200
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Measured against the live gateway: 12 KB passes, 16 KB is refused with
# request_too_large. Sitting at 13 KB leaves headroom for the JSON envelope.
BODY_LIMIT = 13 * 1024


def _scrub(text: str) -> str:
    text = _CONTROL.sub(" ", text)
    text = text.replace(" ", " ").replace(" ", " ")
    if len(text) > MAX_TOOL_RESULT:
        text = text[:MAX_TOOL_RESULT] + " …[truncated]"
    return text


def flatten_input(items: list[Any]) -> list[dict[str, str]]:
    """Rewrite Responses-style history into the plain role messages the gateway allows.

    The gateway will *emit* ``function_call`` items but refuses to accept any
    typed item back — ``function_call``, ``function_call_output`` and even a
    message whose ``content`` is a list are all rejected as "Invalid input".
    Only ``{role, content:str}`` survives, where role is one of system / user /
    assistant / tool.

    So a tool round trip is replayed as prose: the call becomes an assistant
    line, the result becomes a tool line. The model correlates them by order,
    which is all it has here.
    """
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")

        if kind == "function_call":
            # Deliberately dropped. Replaying the call as prose makes the model
            # re-issue it: without a call_id there is nothing tying the result
            # back to the request, so it reads its own past call as an intention
            # it has not yet carried out.
            continue

        if kind == "function_call_output":
            output = item.get("output")
            if not isinstance(output, str):
                output = json.dumps(output)
            out.append({"role": "system", "content": f"{_OBSERVED}{_scrub(output)}"})
            continue

        role = item.get("role")
        if not role:
            continue
        if role not in ("system", "user", "assistant", "tool", "developer"):
            role = "user"
        if role == "developer":  # gateway has no developer role
            role = "system"

        content = item.get("content")
        if isinstance(content, list):
            # Flatten the block array the gateway will not take.
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
            )
        if content is None:
            content = ""
        out.append({"role": role, "content": str(content)})
    return out


class A1GatewayLLMService(OpenAIResponsesHttpLLMService):
    """Non-streaming Responses client aimed at the event's AI gateway."""

    def __init__(self, *, api_key: str, base_url: str, **kwargs):
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        self._gw_base = base_url.rstrip("/")
        self._gw_key = api_key
        self._http = httpx.AsyncClient(timeout=45.0)
        self._consecutive_failures = 0

    async def _apologise(self) -> None:
        """Say something different each time.

        Repeating one apology verbatim is how a stuck agent sounds — and it is
        what the caller actually heard when a poisoned context failed every turn.
        Varying it at least signals a real problem rather than a loop.
        """
        lines = [
            "Sorry — I didn't catch that. Could you say it again?",
            "I'm having trouble on my end. What was that?",
            "Something's not working properly here. Say that once more?",
            "I'm still struggling — it might be worth calling back in a moment.",
        ]
        idx = min(self._consecutive_failures - 1, len(lines) - 1)
        await self._push_llm_text(lines[idx])

    @staticmethod
    def _drop_last_exchange(messages: list[Any]) -> list[dict[str, str]]:
        """Remove the trailing tool results and the turn that produced them.

        A tool result the gateway won't accept — an AppleScript error full of
        control characters, say — sits in the history and fails every subsequent
        request. Dropping back to the last clean user turn recovers the call.
        """
        cut = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            item = messages[i]
            role = item.get("role") if isinstance(item, dict) else None
            content = str(item.get("content", "")) if isinstance(item, dict) else ""
            if role == "system" and content.startswith(_OBSERVED):
                cut = i
                continue
            if role == "user":
                break
            cut = min(cut, i)
        return list(messages[:cut]) if cut < len(messages) else list(messages)

    async def _gateway_post(self, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(
            f"{self._gw_base}/responses",
            headers={
                "Authorization": f"Bearer {self._gw_key}",
                "Content-Type": "application/json",
            },
            json=params,
        )
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"gateway rejected request: {body['error']}")
        resp.raise_for_status()
        return body

    @staticmethod
    def _clean_tools(tools: Any) -> Any:
        """Drop null-valued keys from each tool definition.

        Pipecat emits ``"strict": null`` to mean "unset". The gateway's validator
        treats a null there as a malformed definition and rejects the whole array
        with "tools must contain function definitions" — which points at the
        wrong thing entirely. Omitting the key, or sending a real boolean, is
        accepted.
        """
        if not isinstance(tools, list):
            return tools
        return [
            {k: v for k, v in tool.items() if v is not None} if isinstance(tool, dict) else tool
            for tool in tools
        ]

    def _sanitize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Strip what the gateway refuses; fold instructions into the input."""
        for key in _UNSUPPORTED:
            params.pop(key, None)

        if "tools" in params:
            params["tools"] = self._clean_tools(params["tools"])

        messages = flatten_input(list(params.get("input") or []))

        instructions = params.pop("instructions", None)
        if instructions:
            messages = [{"role": "system", "content": instructions}, *messages]

        params["input"] = self._fit_budget(messages, params)
        return params

    @staticmethod
    def _fit_budget(messages: list[dict[str, str]], params: dict[str, Any]) -> list[dict[str, str]]:
        """Drop the oldest turns until the whole request fits.

        The gateway rejects anything over roughly 14 KB — small enough that the
        system prompt and tool schemas alone take most of it. Without this, a
        long call grows past the ceiling and then *stays* past it: every
        subsequent turn fails identically and the caller hears the same apology
        until they hang up. Trimming is the difference between a conversation
        that forgets its beginning and one that dies.

        The first message (system prompt) and the last (what was just said) are
        never dropped.
        """
        overhead = len(json.dumps({k: v for k, v in params.items() if k != "input"}).encode())
        room = BODY_LIMIT - overhead

        def size(msgs: list[dict[str, str]]) -> int:
            return len(json.dumps(msgs).encode())

        if size(messages) <= room:
            return messages

        head, tail = messages[:1], messages[1:]
        dropped = 0
        while len(tail) > 1 and size(head + tail) > room:
            tail.pop(0)
            dropped += 1

        # Still too big with one turn left: truncate that turn rather than fail.
        if size(head + tail) > room and tail:
            spare = max(400, room - size(head) - 200)
            tail[-1] = {**tail[-1], "content": str(tail[-1].get("content", ""))[:spare]}

        logger.warning(f"context over budget — dropped {dropped} older turns")
        return head + tail

    async def _process_context(self, context: LLMContext) -> None:
        adapter = self.get_llm_adapter()
        invocation_params = adapter.get_llm_invocation_params(
            context, system_instruction=assert_given(self._settings.system_instruction)
        )
        params = self._sanitize(self._build_response_params(invocation_params))

        await self.start_ttfb_metrics()
        try:
            body = await self._gateway_post(params)
            self._consecutive_failures = 0
        except Exception as e:  # a dead gateway must not kill the call
            await self.stop_ttfb_metrics()
            self._consecutive_failures += 1
            logger.error(f"gateway call failed ({self._consecutive_failures}x): {e}")

            # A rejection is usually the *history*, not the request: one bad tool
            # result poisons every turn after it, and the caller hears the same
            # apology forever. Retry once on a pruned context before giving up.
            if self._consecutive_failures == 1:
                pruned = dict(params)
                pruned["input"] = self._drop_last_exchange(params.get("input") or [])
                if len(pruned["input"]) < len(params.get("input") or []):
                    try:
                        body = await self._gateway_post(pruned)
                        logger.warning("recovered by dropping the last exchange")
                        self._consecutive_failures = 0
                    except Exception:
                        await self._apologise()
                        return
                else:
                    await self._apologise()
                    return
            else:
                await self._apologise()
                return
        else:
            await self.stop_ttfb_metrics()

        function_calls: dict[str, dict[str, str]] = {}
        text_parts: list[str] = []

        for item in body.get("output") or []:
            kind = item.get("type")
            if kind == "function_call":
                function_calls[item.get("id") or item.get("call_id")] = {
                    "name": item.get("name", ""),
                    "call_id": item.get("call_id", ""),
                    "arguments": item.get("arguments") or "{}",
                }
            elif kind == "message":
                for block in item.get("content") or []:
                    if block.get("type") in ("output_text", "text") and block.get("text"):
                        text_parts.append(block["text"])

        if text_parts:
            # One shot rather than deltas — there is nothing to stream.
            await self._push_llm_text("".join(text_parts))

        if usage := body.get("usage"):
            in_details = usage.get("input_tokens_details") or {}
            out_details = usage.get("output_tokens_details") or {}
            await self.start_llm_usage_metrics(
                LLMTokenUsage(
                    prompt_tokens=usage.get("input_tokens") or 0,
                    completion_tokens=usage.get("output_tokens") or 0,
                    total_tokens=usage.get("total_tokens") or 0,
                    cache_read_input_tokens=in_details.get("cached_tokens") or 0,
                    reasoning_tokens=out_details.get("reasoning_tokens") or 0,
                )
            )

        self._full_model_name = body.get("model") or self._settings.model

        if function_calls:
            logger.debug(f"gateway requested tools: {[f['name'] for f in function_calls.values()]}")
            await self.run_function_calls(self._process_function_calls(context, function_calls))
