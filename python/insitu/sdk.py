"""The original decorator-oriented Python API, backed by the v1 process protocol."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter

from insitu.protocol import JsonRpcPeer, read_stream_message
from insitu.types import (
    CapabilityKind,
    Diagnostic,
    InitializeRequest,
    InitializeResult,
    InvocationRequest,
    InvocationResult,
    Location,
    ResourceRead,
)


@dataclass(slots=True)
class Context:
    root: Path
    location: Location
    _peer: JsonRpcPeer
    dependencies: list[str]
    trace: tuple[str, ...]

    async def resource(self, resource: str) -> JsonValue:
        raw = await self._peer.request(
            "host.resource.get",
            {
                "resource": resource,
                "location": self.location.model_dump(mode="json"),
                "trace": list(self.trace),
            },
        )
        read = ResourceRead.model_validate(raw)
        self.dependencies.extend(read.dependencies)
        return read.value


@dataclass(frozen=True, slots=True)
class _Registration:
    params: TypeAdapter
    result: TypeAdapter
    handler: Callable
    value: TypeAdapter | None = None

    def schema(self) -> dict[str, JsonValue]:
        schema = {
            "params": self.params.json_schema(),
            "result": self.result.json_schema(),
        }
        if self.value is not None:
            schema["value"] = self.value.json_schema()
        return schema


_METHODS = {
    "insitu.transform": CapabilityKind.TRANSFORM,
    "insitu.resource": CapabilityKind.RESOURCE,
    "insitu.render": CapabilityKind.RENDERER,
    "insitu.validate": CapabilityKind.VALIDATOR,
}


def _stdin_reader(loop: asyncio.AbstractEventLoop) -> asyncio.StreamReader:
    # connect_read_pipe is not supported by Windows' default Proactor loop.
    # A daemon blocking reader bridges stdin on every supported platform.
    reader = asyncio.StreamReader(limit=8192)

    def pump() -> None:
        try:
            while data := os.read(sys.stdin.fileno(), 65536):
                loop.call_soon_threadsafe(reader.feed_data, data)
            loop.call_soon_threadsafe(reader.feed_eof)
        except (RuntimeError, OSError):
            try:
                loop.call_soon_threadsafe(reader.feed_eof)
            except RuntimeError:
                pass

    threading.Thread(target=pump, name="insitu-stdin", daemon=True).start()
    return reader


async def _write_stdout(data: bytes) -> None:
    def emit() -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    await asyncio.to_thread(emit)


class Extension:
    """Register typed capabilities, then serve them over stdin/stdout."""

    def __init__(self, *, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._root = Path.cwd()
        self._capabilities: dict[CapabilityKind, dict[str, _Registration]] = {
            kind: {} for kind in CapabilityKind
        }

    def transform(
        self, name: str, *, params: Any = dict[str, JsonValue], result: Any = str
    ) -> Callable:
        return self._register(CapabilityKind.TRANSFORM, name, params, result)

    def resource(
        self, name: str, *, params: Any = dict[str, JsonValue], result: Any = JsonValue
    ) -> Callable:
        return self._register(CapabilityKind.RESOURCE, name, params, result)

    def renderer(
        self,
        name: str,
        *,
        value: Any = JsonValue,
        params: Any = dict[str, JsonValue],
        result: Any = str,
    ) -> Callable:
        return self._register(
            CapabilityKind.RENDERER, name, params, result, value=value
        )

    def validator(self, name: str, *, params: Any = dict[str, JsonValue]) -> Callable:
        return self._register(
            CapabilityKind.VALIDATOR, name, params, tuple[Diagnostic, ...]
        )

    def run(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        reader = _stdin_reader(asyncio.get_running_loop())

        async def read() -> bytes:
            return await read_stream_message(reader)

        peer = JsonRpcPeer(read, _write_stdout)
        stop = asyncio.Event()

        async def shutdown(_raw: JsonValue | None) -> None:
            return None

        peer.handle("insitu.initialize", self._initialize)
        peer.handle("insitu.shutdown", shutdown, after_response=stop.set)
        for method, kind in _METHODS.items():
            peer.handle(method, lambda raw, kind=kind: self._invoke(peer, kind, raw))
        await peer.start()
        closed = asyncio.create_task(peer.wait_closed())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, pending = await asyncio.wait(
                {closed, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            await peer.close()

    async def _initialize(self, raw: JsonValue | None) -> JsonValue:
        request = InitializeRequest.model_validate(raw)
        self._root = Path(request.root).resolve()
        capabilities = {
            kind.value: {name: entry.schema() for name, entry in entries.items()}
            for kind, entries in self._capabilities.items()
        }
        return InitializeResult(
            name=self.name, version=self.version, capabilities=capabilities
        ).model_dump(mode="json")

    def _register(
        self,
        kind: CapabilityKind,
        name: str,
        params: Any,
        result: Any,
        *,
        value: Any | None = None,
    ) -> Callable:
        def register(handler: Callable) -> Callable:
            entries = self._capabilities[kind]
            if name in entries:
                raise ValueError(f"duplicate {kind.value}: {name}")
            entries[name] = _Registration(
                TypeAdapter(params),
                TypeAdapter(result),
                handler,
                TypeAdapter(value) if value is not None else None,
            )
            return handler

        return register

    async def _invoke(
        self, peer: JsonRpcPeer, kind: CapabilityKind, raw: JsonValue | None
    ) -> JsonValue:
        request = InvocationRequest.model_validate(raw)
        entry = self._capabilities[kind][request.name]
        params = entry.params.validate_python(request.options)
        context = Context(self._root, request.location, peer, [], request.trace)
        args = (params, context)
        if entry.value is not None:
            args = (entry.value.validate_python(request.value), params, context)
        value = entry.handler(*args)
        if inspect.isawaitable(value):
            value = await value
        validated = entry.result.validate_python(value)
        dumped = entry.result.dump_python(validated, mode="json")
        dependencies = tuple(dict.fromkeys(context.dependencies))
        if kind is CapabilityKind.RESOURCE:
            result = InvocationResult(value=dumped, dependencies=dependencies)
        elif kind is CapabilityKind.VALIDATOR:
            result = InvocationResult(
                diagnostics=tuple(Diagnostic.model_validate(item) for item in dumped),
                dependencies=dependencies,
            )
        else:
            result = InvocationResult(content=str(validated), dependencies=dependencies)
        return result.model_dump(mode="json")
