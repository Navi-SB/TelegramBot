"""A ~60-line raw-ASGI JSON endpoint helper.

No Starlette: four single-method routes don't justify a framework, it keeps
cold start near 200ms — which matters against a sub-second webhook ack budget —
and tests just `await app(scope, receive, send)`.

ASGI rather than BaseHTTPRequestHandler because the latter is synchronous,
forcing asyncio.run() per invocation, which kills any module-level
httpx.AsyncClient (bound to a dead loop) and pays a fresh TLS handshake to
api.telegram.org on every call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl

from .logging import log_exception


@dataclass
class Req:
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    query: dict[str, str] = field(default_factory=dict)
    method: str = "POST"

    def json(self) -> Any:
        return json.loads(self.body or b"{}")


@dataclass
class Resp:
    status: int = 200
    body: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    # Raw text response instead of JSON. Meta's webhook verification compares
    # the echoed hub.challenge byte-for-byte, so it must not be JSON-encoded.
    text: str | None = None


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


async def _respond(send, status: int, payload: dict[str, Any], text: str | None = None) -> None:
    if text is not None:
        data, ctype = text.encode(), b"text/plain; charset=utf-8"
    else:
        data, ctype = json.dumps(payload).encode(), b"application/json"
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", ctype)],
    })
    await send({"type": "http.response.body", "body": data})


def json_endpoint(
    fn: Callable[[Req], Awaitable[Resp]], methods: tuple[str, ...] = ("POST",)
):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["method"] not in methods:
            return await _respond(send, 405, {"ok": False})

        qs = scope.get("query_string", b"").decode()
        req = Req(
            headers={k.decode().lower(): v.decode() for k, v in scope["headers"]},
            body=await _read_body(receive),
            # parse_qsl so %-encoded values (hub.challenge and friends) decode.
            query=dict(parse_qsl(qs)),
            method=scope["method"],
        )
        try:
            res = await fn(req)
        except Exception as exc:  # noqa: BLE001 — the outermost boundary
            log_exception("handler_failed", exc)
            res = Resp(500, {"ok": False})
        await _respond(send, res.status, res.body, res.text)

    return app
