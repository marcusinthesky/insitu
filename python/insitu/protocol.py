"""Concurrent bidirectional JSON-RPC 2.0 over bounded Content-Length frames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from insitu.types import JsonValue

RpcHandler = Callable[[JsonValue | None], Awaitable[JsonValue | None]]
ReadMessage = Callable[[], Awaitable[bytes]]
WriteMessage = Callable[[bytes], Awaitable[None]]
_MAX_MESSAGE = 16 * 1024 * 1024
_MAX_HEADERS = 8192


class RpcProtocolError(RuntimeError):
    """A malformed message, a remote error, or a closed peer."""


def frame(payload: bytes) -> bytes:
    if len(payload) > _MAX_MESSAGE:
        raise RpcProtocolError("JSON-RPC message exceeds 16 MiB")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


async def read_stream_message(reader: asyncio.StreamReader) -> bytes:
    headers: dict[str, str] = {}
    consumed = 0
    while True:
        try:
            line = await reader.readline()
        except ValueError as error:
            raise RpcProtocolError("JSON-RPC header exceeds limit") from error
        if not line:
            if consumed:
                raise RpcProtocolError("truncated JSON-RPC headers")
            return b""
        consumed += len(line)
        if consumed > _MAX_HEADERS:
            raise RpcProtocolError("JSON-RPC headers exceed 8 KiB")
        if line in (b"\n", b"\r\n"):
            break
        try:
            name, separator, value = line.decode("ascii").partition(":")
        except UnicodeDecodeError as error:
            raise RpcProtocolError("non-ASCII JSON-RPC header") from error
        name = name.strip().lower()
        if not separator or name == "content-length" and name in headers:
            raise RpcProtocolError("malformed or duplicate JSON-RPC header")
        headers[name] = value.strip()
    try:
        length = int(headers["content-length"])
    except (KeyError, ValueError) as error:
        raise RpcProtocolError("missing or invalid Content-Length") from error
    if not 0 <= length <= _MAX_MESSAGE:
        raise RpcProtocolError("invalid JSON-RPC message length")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise RpcProtocolError("truncated JSON-RPC payload") from error


class JsonRpcPeer:
    """Routes responses independently, allowing callbacks during a request."""

    def __init__(self, read_message: ReadMessage, write_message: WriteMessage) -> None:
        self._read_message = read_message
        self._write_message = write_message
        self._handlers: dict[str, RpcHandler] = {}
        self._after_response: dict[str, Callable[[], None]] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed: RpcProtocolError | None = None

    def handle(
        self,
        method: str,
        handler: RpcHandler,
        *,
        after_response: Callable[[], None] | None = None,
    ) -> None:
        self._handlers[method] = handler
        if after_response is not None:
            self._after_response[method] = after_response

    async def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def wait_closed(self) -> None:
        if self._reader:
            await self._reader

    async def request(
        self, method: str, params: JsonValue | None = None, *, timeout: float = 30.0
    ) -> JsonValue | None:
        if self._closed:
            raise self._closed
        self._sequence += 1
        identifier = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(identifier, None)

    async def notify(self, method: str, params: JsonValue | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        current = asyncio.current_task()
        tasks = set(self._tasks)
        if self._reader:
            tasks.add(self._reader)
        tasks.discard(current)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(RpcProtocolError("RPC peer closed"))

    async def _send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise self._closed
        payload = json.dumps(
            message, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        framed = frame(payload)
        async with self._write_lock:
            await self._write_message(framed)

    def _fail_pending(self, error: RpcProtocolError) -> None:
        self._closed = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _read_loop(self) -> None:
        try:
            while payload := await self._read_message():
                try:
                    message = json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise RpcProtocolError("invalid JSON-RPC payload") from error
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RpcProtocolError("invalid JSON-RPC envelope")
                if "method" in message:
                    task = asyncio.create_task(self._dispatch(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                    continue
                future = self._pending.get(message.get("id"))
                if future is None or future.done():
                    continue
                if error := message.get("error"):
                    future.set_exception(
                        RpcProtocolError(str(error.get("message", error)))
                    )
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    future.set_exception(
                        RpcProtocolError("response has neither result nor error")
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - any reader failure must fail pending requests
            self._fail_pending(RpcProtocolError(str(error)))
        finally:
            if self._closed is None:
                self._fail_pending(RpcProtocolError("RPC peer closed"))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        identifier = message.get("id")
        handler = self._handlers.get(message["method"])
        if handler is None:
            response = {
                "error": {
                    "code": -32601,
                    "message": f"unknown method: {message['method']}",
                }
            }
        else:
            try:
                response = {"result": await handler(message.get("params"))}
            except Exception as error:  # noqa: BLE001 - handler failures become JSON-RPC errors
                response = {"error": {"code": -32000, "message": str(error)}}
        if identifier is not None:
            try:
                await self._send({"jsonrpc": "2.0", "id": identifier, **response})
            except (RpcProtocolError, BrokenPipeError, ConnectionError):
                pass
        if callback := self._after_response.get(message["method"]):
            callback()
