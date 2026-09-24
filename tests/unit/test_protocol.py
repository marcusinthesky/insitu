"""JSON-RPC lifecycle tests."""

import pytest

from insitu.protocol import JsonRpcPeer, RpcProtocolError

pytestmark = [pytest.mark.unit, pytest.mark.protocol]


async def test_remote_close_fails_requests() -> None:
    async def read() -> bytes:
        return b""

    async def write(_data: bytes) -> None:
        return None

    peer = JsonRpcPeer(read, write)
    await peer.start()
    await peer.wait_closed()
    with pytest.raises(RpcProtocolError, match="closed"):
        await peer.request("example")
    await peer.close()
