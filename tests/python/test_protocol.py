"""Executed locally: framing, cancellation and real Python subprocess RPC."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from insitu.protocol import JsonRpcPeer, RpcProtocolError, frame, read_stream_message
from insitu.types import Location

ROOT = Path(__file__).resolve().parents[1]


async def read_bytes(payload):
    reader = asyncio.StreamReader(limit=8192)
    reader.feed_data(payload)
    reader.feed_eof()
    return await read_stream_message(reader)


@pytest.mark.parametrize("payload", [b"{}", '"🦀"'.encode(), b"null", b'"line\\n"'])
async def test_framing_uses_byte_length(payload):
    assert await read_bytes(frame(payload)) == payload


@pytest.mark.parametrize(
    "payload",
    [
        b"\r\n",
        b"Content-Length: -1\r\n\r\n",
        b"Content-Length: nope\r\n\r\n",
        b"Content-Length: 16777217\r\n\r\n",
        b"Content-Length: 5\r\n\r\n{}",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"Broken\r\n\r\n",
        b"Content-Length: 2\r\n",
        b"X: " + b"x" * 9000 + b"\r\n\r\n",
    ],
)
async def test_malformed_frames_fail(payload):
    with pytest.raises(RpcProtocolError):
        await read_bytes(payload)


async def test_clean_eof():
    assert await read_bytes(b"") == b""


async def test_case_insensitive_header():
    assert await read_bytes(b"content-length: 2\n\n{}") == b"{}"


def test_oversized_write_rejected():
    with pytest.raises(RpcProtocolError):
        frame(b"x" * (16 * 1024 * 1024 + 1))


@pytest.fixture
async def extension_peer(tmp_path):
    env = dict(
        os.environ, PYTHONPATH=str(ROOT.parent / "python"), PYTHONDONTWRITEBYTECODE="1"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "fixtures/extension/extension.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    async def read():
        return await read_stream_message(process.stdout)

    async def write(data):
        process.stdin.write(data)
        await process.stdin.drain()

    peer = JsonRpcPeer(read, write)

    async def resource(params):
        assert params["resource"].startswith("fs:file:")
        return {
            "value": "payload 🦀",
            "dependencies": [params["resource"], "index:documents", params["resource"]],
        }

    peer.handle("host.resource.get", resource)
    await peer.start()
    initialized = await peer.request(
        "insitu.initialize",
        {"protocol": 1, "root": str(tmp_path), "extension": "conformance"},
        timeout=5,
    )
    yield peer, initialized
    try:
        await peer.request("insitu.shutdown", timeout=2)
    finally:
        process.stdin.close()
        await peer.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()
        stderr = await process.stderr.read()
        assert process.returncode == 0, stderr.decode(errors="replace")


def invocation(name, *, options=None, value=None):
    return {
        "name": name,
        "options": options or {},
        "location": Location.from_path("notes.md").model_dump(mode="json"),
        "value": value,
        "trace": [],
    }


async def test_handshake_schemas(extension_peer):
    _, initialized = extension_peer
    assert initialized["protocol"] == 1
    assert initialized["name"] == "conformance"
    assert set(initialized["capabilities"]) == {
        "resource",
        "renderer",
        "transform",
        "validator",
    }
    assert "params" in initialized["capabilities"]["transform"]["greeting"]


async def test_typed_transform(extension_peer):
    peer, _ = extension_peer
    result = await peer.request(
        "insitu.transform", invocation("greeting", options={"name": "reader"})
    )
    assert result["content"] == "Hello, reader!"


async def test_host_callback_and_dependencies(extension_peer):
    peer, _ = extension_peer
    result = await peer.request(
        "insitu.transform", invocation("read-file", options={"path": "snippet.txt"})
    )
    assert result["content"] == "payload 🦀"
    assert result["dependencies"] == ["fs:file:snippet.txt", "index:documents"]


async def test_resource_renderer_validator(extension_peer):
    peer, _ = extension_peer
    resource = await peer.request("insitu.resource", invocation("numbers"))
    assert resource["value"] == [1, 2, 3]
    rendered = await peer.request(
        "insitu.render", invocation("join", value=resource["value"])
    )
    assert rendered["content"] == "1,2,3"
    report = await peer.request("insitu.validate", invocation("notice"))
    assert report["diagnostics"][0]["severity"] == "info"


async def test_concurrent_requests(extension_peer):
    peer, _ = extension_peer
    results = await asyncio.gather(
        *(
            peer.request(
                "insitu.transform", invocation("greeting", options={"name": str(i)})
            )
            for i in range(32)
        )
    )
    assert [value["content"] for value in results] == [
        f"Hello, {i}!" for i in range(32)
    ]


async def test_validation_and_unknown_method_errors(extension_peer):
    peer, _ = extension_peer
    with pytest.raises(RpcProtocolError):
        await peer.request("insitu.render", invocation("join", value={"not": "a list"}))
    with pytest.raises(RpcProtocolError, match="unknown method"):
        await peer.request("insitu.typo", {})


async def test_timeout_removes_pending():
    queue = asyncio.Queue()

    async def read():
        return await queue.get()

    async def write(data):
        pass

    peer = JsonRpcPeer(read, write)
    await peer.start()
    with pytest.raises(TimeoutError):
        await peer.request("never", timeout=0.005)
    assert peer._pending == {}
    await peer.close()


async def test_closed_peer_fails_pending():
    queue = asyncio.Queue()

    async def read():
        return await queue.get()

    async def write(data):
        await queue.put(b"")

    peer = JsonRpcPeer(read, write)
    await peer.start()
    with pytest.raises(RpcProtocolError, match="closed"):
        await peer.request("anything")
    await peer.close()
