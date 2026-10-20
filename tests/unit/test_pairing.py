"""
Tests for pairing an add-on with the server through the shared token

Both sides prove they hold the token with an HMAC over the other's nonce; the
token itself never goes over the socket.
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

import test_imports  # Automatic path setup
from server.server import FoxMCPServer, ensure_pairing_token, pairing_proof

TOKEN = "0123456789abcdef0123456789abcdef"


def test_the_token_file_is_created_once_and_then_reused(tmp_path):
    path = str(tmp_path / ".foxmcp" / "pairing-token")
    first = ensure_pairing_token(path)

    assert len(first) == 32
    assert ensure_pairing_token(path) == first


def connected(token=TOKEN):
    server = FoxMCPServer(host="localhost", port=8765, pairing_token=token)
    socket = Mock()
    socket.send = AsyncMock()
    server._add_browser(socket)
    return server, socket


async def say(server, socket, message_type, data):
    await server.handle_extension_message(json.dumps({"id": "x", "type": message_type, "data": data}), socket)
    await asyncio.sleep(0)  # let the ack's send task run


def sent_ack(socket):
    return json.loads(socket.send.await_args.args[0])


@pytest.mark.asyncio
async def test_a_right_proof_pairs_the_browser():
    server, socket = connected()
    await say(server, socket, "hello", {"instance_id": "tb1", "app": "thunderbird", "client_nonce": "c1"})

    ack = sent_ack(socket)
    assert ack["type"] == "hello_ack"
    assert ack["data"]["proof"] == pairing_proof(TOKEN, "server", "c1")
    assert not server.browsers["tb1"].paired

    await say(server, socket, "auth", {"proof": pairing_proof(TOKEN, "client", ack["data"]["server_nonce"])})
    assert server.browsers["tb1"].paired


@pytest.mark.asyncio
async def test_a_wrong_proof_does_not_pair_and_cannot_be_retried():
    server, socket = connected()
    await say(server, socket, "hello", {"instance_id": "tb1", "client_nonce": "c1"})
    nonce = sent_ack(socket)["data"]["server_nonce"]

    await say(server, socket, "auth", {"proof": pairing_proof("another token", "client", nonce)})
    assert not server.browsers["tb1"].paired

    await say(server, socket, "auth", {"proof": pairing_proof(TOKEN, "client", nonce)})
    assert not server.browsers["tb1"].paired  # the nonce was spent


@pytest.mark.asyncio
async def test_an_add_on_without_a_nonce_is_left_unpaired_and_not_answered():
    server, socket = connected()
    await say(server, socket, "hello", {"instance_id": "ff1", "name": "Work"})

    socket.send.assert_not_awaited()
    assert not server.browsers["ff1"].paired


@pytest.mark.asyncio
async def test_a_server_without_a_token_says_so():
    server, socket = connected(token=None)
    await say(server, socket, "hello", {"instance_id": "tb1", "client_nonce": "c1"})

    assert sent_ack(socket)["data"] == {"proof": None}
