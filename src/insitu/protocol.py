"""Bidirectional JSON-RPC 2.0 with LSP-style standard-I/O framing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from insitu.types import JsonValue

RpcHandler = Callable[[JsonValue | None], Awaitable[JsonValue | None]]
ReadMessage = Callable[[], Awaitable[bytes]]
WriteMessage = Callable[[bytes], Awaitable[None]]
_MAX_MESSAGE = 16 * 1024 * 1024


class RpcProtocolError(RuntimeError):
    """Raised for malformed or failed JSON-RPC messages."""


class JsonRpcPeer:
    """Small concurrent JSON-RPC peer with callbacks and cancellation."""

    def __init__(self, read_message: ReadMessage, write_message: WriteMessage) -> None:
        self._read_message = read_message
        self._write_message = write_message
        self._handlers: dict[str, RpcHandler] = {}
        self._pending: dict[int, asyncio.Future[JsonValue | None]] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed: RpcProtocolError | None = None

    def handle(self, method: str, handler: RpcHandler) -> None:
        """Register an inbound method handler."""
        self._handlers[method] = handler

    async def start(self) -> None:
        """Start reading messages."""
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def wait_closed(self) -> None:
        """Wait until the remote closes its output."""
        if self._reader is not None:
            await self._reader

    async def request(
        self,
        method: str,
        params: JsonValue | None = None,
        *,
        timeout: float = 30.0,
    ) -> JsonValue | None:
        """Send a request and await its typed JSON value."""
        if self._closed is not None:
            raise self._closed
        self._sequence += 1
        identifier = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        await self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(identifier, None)

    async def notify(self, method: str, params: JsonValue | None = None) -> None:
        """Send a notification without waiting for a response."""
        if self._closed is not None:
            raise self._closed
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        """Stop the reader and all in-flight handlers."""
        current = asyncio.current_task()
        tasks = set(self._tasks)
        if self._reader is not None:
            tasks.add(self._reader)
        for task in tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not current),
            return_exceptions=True,
        )
        self._fail_pending(RpcProtocolError("RPC peer closed"))

    async def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(
            message,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(payload) > _MAX_MESSAGE:
            raise RpcProtocolError("JSON-RPC message exceeds 16 MiB")
        async with self._write_lock:
            await self._write_message(frame(payload))

    async def _read_loop(self) -> None:
        try:
            while payload := await self._read_message():
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError as error:
                    raise RpcProtocolError("invalid JSON-RPC payload") from error
                if "method" in message:
                    task = asyncio.create_task(self._dispatch(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                    continue
                identifier = message.get("id")
                future = self._pending.get(identifier)
                if future is None or future.done():
                    continue
                if error := message.get("error"):
                    error_message = str(error.get("message", error))
                    future.set_exception(RpcProtocolError(error_message))
                else:
                    result = cast("JsonValue | None", message.get("result"))
                    future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(RpcProtocolError(str(error)))
            raise
        else:
            self._fail_pending(RpcProtocolError("RPC peer closed"))

    def _fail_pending(self, error: Exception) -> None:
        if isinstance(error, RpcProtocolError):
            self._closed = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        identifier = message.get("id")
        handler = self._handlers.get(method)
        if handler is None:
            if identifier is not None:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": -32601,
                            "message": f"unknown method: {method}",
                        },
                    }
                )
            return
        try:
            result = await handler(cast("JsonValue | None", message.get("params")))
            if identifier is not None:
                await self._send({"jsonrpc": "2.0", "id": identifier, "result": result})
        except Exception as error:  # noqa: BLE001
            if identifier is not None:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32000, "message": str(error)},
                    }
                )


def frame(payload: bytes) -> bytes:
    """Frame one JSON payload for standard-I/O transport."""
    return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload


async def read_stream_message(reader: asyncio.StreamReader) -> bytes:
    """Read one framed message from an asyncio stream."""
    length = await _read_headers(reader.readline)
    if length is None:
        return b""
    return await reader.readexactly(length)


async def _read_headers(
    read_line: Callable[[], Awaitable[bytes]],
) -> int | None:
    headers: dict[str, str] = {}
    while line := await read_line():
        if line in {b"\n", b"\r\n"}:
            return _content_length(headers)
        key, separator, value = line.decode("ascii").partition(":")
        if not separator:
            raise RpcProtocolError("malformed JSON-RPC header")
        headers[key.lower().strip()] = value.strip()
    return None


def _content_length(headers: dict[str, str]) -> int:
    try:
        length = int(headers["content-length"])
    except (KeyError, ValueError) as error:
        raise RpcProtocolError("missing or invalid Content-Length") from error
    if length < 0 or length > _MAX_MESSAGE:
        raise RpcProtocolError("invalid JSON-RPC message length")
    return length
