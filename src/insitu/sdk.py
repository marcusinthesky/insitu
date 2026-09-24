"""Typed Python SDK for authoring process extensions."""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

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

Params = TypeVar("Params")
Result = TypeVar("Result")
Value = TypeVar("Value")
Handler = Callable[..., Any]
HandlerType = TypeVar("HandlerType", bound=Handler)


@dataclass(slots=True)
class Context:
    """Invocation context exposed to Python extensions."""

    root: Path
    location: Location
    _peer: JsonRpcPeer
    dependencies: list[str]
    trace: tuple[str, ...]

    async def resource(self, resource: str) -> JsonValue:
        """Read a permitted host resource and capture transitive dependencies."""
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
    kind: CapabilityKind
    params: TypeAdapter[Any]
    result: TypeAdapter[Any]
    handler: Handler
    value: TypeAdapter[Any] | None = None


class Extension:
    """FastAPI-like typed extension application."""

    def __init__(self, *, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._root = Path.cwd()
        self._capabilities: dict[CapabilityKind, dict[str, _Registration]] = {
            kind: {} for kind in CapabilityKind
        }

    def transform(
        self,
        name: str,
        *,
        params: type[Params] | Any = dict[str, JsonValue],
        result: type[Result] | Any = str,
    ) -> Callable[[HandlerType], HandlerType]:
        """Register a runtime-validated Markdown transform."""
        return cast(
            "Callable[[HandlerType], HandlerType]",
            self._register(CapabilityKind.TRANSFORM, name, params, result),
        )

    def resource(
        self,
        name: str,
        *,
        params: type[Params] | Any = dict[str, JsonValue],
        result: type[Result] | Any = JsonValue,
    ) -> Callable[[HandlerType], HandlerType]:
        """Register a runtime-validated resource provider."""
        return cast(
            "Callable[[HandlerType], HandlerType]",
            self._register(CapabilityKind.RESOURCE, name, params, result),
        )

    def renderer(
        self,
        name: str,
        *,
        value: type[Value] | Any = JsonValue,
        params: type[Params] | Any = dict[str, JsonValue],
        result: type[Result] | Any = str,
    ) -> Callable[[HandlerType], HandlerType]:
        """Register a typed value-to-Markdown renderer."""
        return cast(
            "Callable[[HandlerType], HandlerType]",
            self._register(
                CapabilityKind.RENDERER,
                name,
                params,
                result,
                value=value,
            ),
        )

    def validator(
        self,
        name: str,
        *,
        params: type[Params] | Any = dict[str, JsonValue],
    ) -> Callable[[HandlerType], HandlerType]:
        """Register a typed project validator."""
        return cast(
            "Callable[[HandlerType], HandlerType]",
            self._register(
                CapabilityKind.VALIDATOR,
                name,
                params,
                tuple[Diagnostic, ...],
            ),
        )

    def run(self) -> None:
        """Run the extension on standard input/output."""
        asyncio.run(self.serve())

    async def serve(self) -> None:
        """Serve framed JSON-RPC until shutdown or end-of-file."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol,
            sys.stdin.buffer,
        )

        async def read() -> bytes:
            return await read_stream_message(reader)

        async def write(data: bytes) -> None:
            def emit() -> None:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()

            await asyncio.to_thread(emit)

        peer = JsonRpcPeer(read, write)
        stop = asyncio.Event()

        async def initialize(raw: JsonValue | None) -> JsonValue:
            request = InitializeRequest.model_validate(raw)
            self._root = Path(request.root).resolve()
            capabilities: dict[str, JsonValue] = {}
            for kind, registrations in self._capabilities.items():
                capabilities[kind.value] = {
                    name: {
                        "params": registration.params.json_schema(),
                        "result": registration.result.json_schema(),
                        **(
                            {"value": registration.value.json_schema()}
                            if registration.value is not None
                            else {}
                        ),
                    }
                    for name, registration in registrations.items()
                }
            return cast(
                "JsonValue",
                InitializeResult(
                    name=self.name,
                    version=self.version,
                    capabilities=capabilities,
                ).model_dump(mode="json"),
            )

        async def shutdown(_params: JsonValue | None) -> JsonValue:
            asyncio.get_running_loop().call_soon(stop.set)
            return None

        peer.handle("insitu.initialize", initialize)
        peer.handle(
            "insitu.transform",
            lambda raw: self._invoke(peer, CapabilityKind.TRANSFORM, raw),
        )
        peer.handle(
            "insitu.resource",
            lambda raw: self._invoke(peer, CapabilityKind.RESOURCE, raw),
        )
        peer.handle(
            "insitu.render",
            lambda raw: self._invoke(peer, CapabilityKind.RENDERER, raw),
        )
        peer.handle(
            "insitu.validate",
            lambda raw: self._invoke(peer, CapabilityKind.VALIDATOR, raw),
        )
        peer.handle("insitu.shutdown", shutdown)
        await peer.start()
        closed = asyncio.create_task(peer.wait_closed())
        stopped = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {closed, stopped},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is closed and not task.cancelled():
                task.result()
        await peer.close()
        transport.close()

    def _register(
        self,
        kind: CapabilityKind,
        name: str,
        params: Any,
        result: Any,
        *,
        value: Any | None = None,
    ) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            capabilities = self._capabilities[kind]
            if name in capabilities:
                raise ValueError(f"duplicate {kind.value}: {name}")
            capabilities[name] = _Registration(
                kind=kind,
                params=TypeAdapter(params),
                result=TypeAdapter(result),
                value=TypeAdapter(value) if value is not None else None,
                handler=handler,
            )
            return handler

        return register

    async def _invoke(
        self,
        peer: JsonRpcPeer,
        kind: CapabilityKind,
        raw: JsonValue | None,
    ) -> JsonValue:
        request = InvocationRequest.model_validate(raw)
        registration = self._capabilities[kind][request.name]
        params = registration.params.validate_python(request.options)
        context = Context(
            root=self._root,
            location=request.location,
            _peer=peer,
            dependencies=[],
            trace=request.trace,
        )
        args = (params, context)
        if registration.value is not None:
            value = registration.value.validate_python(request.value)
            args = (value, params, context)
        result = registration.handler(*args)
        if inspect.isawaitable(result):
            result = await result
        validated = registration.result.validate_python(result)
        dumped = registration.result.dump_python(validated, mode="json")
        common = tuple(dict.fromkeys(context.dependencies))
        if kind is CapabilityKind.RESOURCE:
            response = InvocationResult(
                value=cast("JsonValue", dumped),
                dependencies=common,
            )
        elif kind is CapabilityKind.VALIDATOR:
            response = InvocationResult(
                diagnostics=tuple(Diagnostic.model_validate(item) for item in dumped),
                dependencies=common,
            )
        else:
            response = InvocationResult(
                content=str(validated),
                dependencies=common,
            )
        return cast("JsonValue", response.model_dump(mode="json"))
