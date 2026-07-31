"""a1mobile 'close-the-loop' MCP client.

The counterparty we don't control: a restaurant reservation sandbox plus the
telephony tools that produce judge-visible side effects.

Streamable-HTTP MCP: one ``initialize`` handshake yields an ``mcp-session-id``
that every later request must carry. Responses come back as SSE frames, so we
strip the ``data:`` prefix and take the first JSON object.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from loguru import logger

_DEFAULT_MCP = "https://hack.a1mobile.com/mcp/"
_DEFAULT_REST = "https://hack.a1mobile.com"

_PROTOCOL_VERSION = "2025-06-18"


class CounterpartyError(RuntimeError):
    """The counterparty refused or failed the operation."""


class Counterparty:
    """Thin MCP client. One instance per call session."""

    def __init__(self, team_key: str | None = None, url: str | None = None) -> None:
        # Read the environment here, not at import time: callers routinely load
        # their .env after importing this module, and a silently-empty team key
        # gets you empty slot lists rather than an error.
        self._team_key = team_key or os.getenv("A1_TEAM_KEY", "")
        self._url = url or os.getenv("A1_MCP", _DEFAULT_MCP)
        self._rest = os.getenv("A1_BASE", _DEFAULT_REST)
        if not self._team_key:
            raise CounterpartyError("A1_TEAM_KEY is not set — load your .env before constructing")
        self._session_id: str | None = None
        self._client = httpx.AsyncClient(timeout=20.0)

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Team-Key": self._team_key,
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any]:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                return json.loads(line)
        raise CounterpartyError(f"no JSON in MCP response: {body[:300]}")

    async def connect(self) -> None:
        """Handshake once and cache the session id."""
        if self._session_id:
            return
        resp = await self._client.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "hypergravity", "version": "0.1"},
                },
            },
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")
        if not self._session_id:
            raise CounterpartyError("MCP did not return a session id")
        # Required by the spec before tools/call is accepted.
        await self._client.post(
            self._url,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        logger.info(f"counterparty connected (session {self._session_id[:8]}…)")

    async def _call(self, tool: str, **args: Any) -> dict[str, Any]:
        await self.connect()
        resp = await self._client.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"team_key": self._team_key, **args}},
            },
        )
        resp.raise_for_status()
        payload = self._parse_sse(resp.text)

        if "error" in payload:
            raise CounterpartyError(f"{tool}: {payload['error']}")

        result = payload.get("result", {})

        # MCP reports a *tool* failure with isError on the result, not as a
        # JSON-RPC error. Ignoring it meant a refused SMS came back looking
        # exactly like a delivered one, and the ledger recorded evidence for a
        # text that never went.
        if result.get("isError"):
            detail = " ".join(
                b.get("text", "") for b in result.get("content", [])
            ).strip()
            raise CounterpartyError(f"{tool}: {detail[:200] or 'refused'}")

        if structured := result.get("structuredContent"):
            # Keep the prose alongside the structured body: callers sniff _text
            # for refusals that arrive as a 200 with an explanation.
            prose = " ".join(b.get("text", "") for b in result.get("content", []))
            return {**structured, "_text": prose.strip()} if prose.strip() else structured
        for block in result.get("content", []):
            text = block.get("text", "")
            if text.strip().startswith("{"):
                return json.loads(text)
        # A tool that reports failure in prose still needs to reach the gate.
        return {"_text": " ".join(b.get("text", "") for b in result.get("content", []))}

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the counterparty's world ----------------------------------------

    async def get_availability(self) -> list[dict[str, Any]]:
        """Slots the restaurant will admit to having. Some are genuinely taken."""
        return (await self._call("get_availability")).get("slots", [])

    async def create_booking(
        self,
        name: str,
        party_size: int,
        time_slot: str,
        phone: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """Attempt the booking. Raises nothing on refusal — the caller inspects."""
        return await self._call(
            "create_booking",
            name=name,
            party_size=party_size,
            time_slot=time_slot,
            phone=phone,
            notes=notes,
        )

    async def list_bookings(self) -> list[dict[str, Any]]:
        """INDEPENDENT read-back over REST — deliberately not the MCP tool that
        created the row, and not the create call's own response.

        This is the channel the verification gate trusts. ``create_booking``
        telling us it worked is the counterparty's claim; a row appearing here
        is evidence.
        """
        resp = await self._client.get(
            f"{self._rest}/api/bookings", headers={"X-Team-Key": self._team_key}
        )
        resp.raise_for_status()
        return resp.json().get("bookings", [])

    async def confirm_booking_landed(
        self, booking_id: int | str, time_slot: str, party_size: int
    ) -> dict[str, Any] | None:
        """Re-read the bookings list and return the matching row, or None.

        Matching on more than the id: a row whose slot or party size disagrees
        with what we asked for is not the booking we think we made.
        """
        for row in await self.list_bookings():
            if str(row.get("id")) != str(booking_id):
                continue
            if row.get("time_slot") != time_slot:
                logger.warning(f"booking {booking_id} slot mismatch: {row.get('time_slot')} != {time_slot}")
                return None
            if int(row.get("party_size", -1)) != int(party_size):
                logger.warning(f"booking {booking_id} party mismatch")
                return None
            return row
        return None

    # -- side effects the judges can see ----------------------------------

    async def send_confirmation_sms(self, to: str, body: str) -> dict[str, Any]:
        return await self._call("send_confirmation_sms", to=to, body=body)

    async def request_number_verification(self, phone: str) -> dict[str, Any]:
        return await self._call("request_number_verification", phone=phone)

    async def confirm_number_verification(self, phone: str, code: str) -> dict[str, Any]:
        return await self._call("confirm_number_verification", phone=phone, code=code)

    async def point_number(self, webhook_url: str) -> dict[str, Any]:
        return await self._call("point_number", webhook_url=webhook_url)


def sms_delivered(resp: dict) -> bool:
    """Did the text actually go?

    Proven by an identifier the service returned, never by the absence of a
    known error word — the field those words arrive in is missing entirely on
    the structured-response path, so the old check passed for every refusal.
    """
    if not isinstance(resp, dict):
        return False
    prose = str(resp.get("_text", "")).lower()
    if any(bad in prose for bad in ("error", "not allowed", "failed", "undeliverable")):
        return False
    if resp.get("error") or resp.get("success") is False:
        return False
    return bool(resp.get("message_id") or resp.get("sid") or resp.get("sent") is True)
