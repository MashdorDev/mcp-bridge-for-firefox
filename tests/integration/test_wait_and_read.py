#!/usr/bin/env python3
"""
content_wait_for and content_read against a stand-in for the add-on

Licensed under the MIT License - see LICENSE file for details
"""

import asyncio
import json

import pytest
import pytest_asyncio

import test_imports  # Automatic path setup
from server import mcp_tools
from server.server import FoxMCPServer
from port_coordinator import get_port_by_type
from test_config import connect_as_extension
from mcp_client_harness import DirectMCPTestClient


@pytest_asyncio.fixture
async def server():
    server = FoxMCPServer(host="localhost", port=get_port_by_type('test_individual'),
                          mcp_port=get_port_by_type('test_mcp_individual'), start_mcp=False)
    server_task = asyncio.create_task(server.start_server())
    await asyncio.sleep(0.5)
    yield server
    await server.shutdown(server_task)


async def answering(ws, answers):
    """Answer each request with the next of answers, the last one repeating"""
    seen = []
    async for raw in ws:
        message = json.loads(raw)
        if message.get("type") != "request":
            continue
        seen.append(message)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        kind = "error" if "message" in answer else "response"
        await ws.send(json.dumps({"id": message["id"], "type": kind, "action": message["action"], "data": answer}))


async def call(server, tool, **arguments):
    client = DirectMCPTestClient(server.mcp_tools)
    await client.connect()
    return str((await client.call_tool(tool, arguments)).get("content"))


PAGE = {"title": "Inbox", "url": "https://example.org/"}


@pytest.mark.asyncio
async def test_it_keeps_looking_across_a_navigation_until_the_thing_shows(server):
    async with connect_as_extension(f"ws://localhost:{server.port}") as ws:
        answers = [{"met": False, **PAGE}, {"message": "Could not establish connection"}, {"met": True, **PAGE}]
        task = asyncio.create_task(answering(ws, answers))
        answer = await call(server, "content_wait_for", tab_id=1, selector="#done")
        task.cancel()
    assert 'selector "#done" is there after' in answer and "Inbox" in answer


@pytest.mark.asyncio
async def test_a_timeout_says_what_the_page_is_now(server):
    async with connect_as_extension(f"ws://localhost:{server.port}") as ws:
        task = asyncio.create_task(answering(ws, [{"met": False, **PAGE}]))
        answer = await call(server, "content_wait_for", tab_id=1, text="Saved", timeout=0.3)
        task.cancel()
    assert 'Timed out' in answer and 'text "Saved"' in answer and "https://example.org/" in answer


@pytest.mark.asyncio
async def test_a_refusal_is_not_waited_out(server, monkeypatch):
    monkeypatch.setattr(mcp_tools, 'WAIT_POLL_S', 60)
    refusal = "The person using this Firefox has paused the agent. They can resume it"
    async with connect_as_extension(f"ws://localhost:{server.port}") as ws:
        task = asyncio.create_task(answering(ws, [{"message": refusal}]))
        answer = await asyncio.wait_for(call(server, "content_wait_for", tab_id=1, title="x", timeout=60), 5)
        task.cancel()
    assert "Failed to wait" in answer and "paused" in answer


@pytest.mark.asyncio
async def test_a_bad_selector_is_not_waited_out(server):
    async with connect_as_extension(f"ws://localhost:{server.port}") as ws:
        task = asyncio.create_task(answering(ws, [{"invalid": "not a valid selector"}]))
        answer = await asyncio.wait_for(call(server, "content_wait_for", tab_id=1, selector="##", timeout=60), 5)
        task.cancel()
    assert "Failed to wait: not a valid selector" in answer


@pytest.mark.asyncio
async def test_it_needs_exactly_one_thing_to_wait_for(server):
    async with connect_as_extension(f"ws://localhost:{server.port}"):
        answer = await call(server, "content_wait_for", tab_id=1, selector="#a", text="b")
    assert "Give one of" in answer


@pytest.mark.asyncio
async def test_read_cuts_long_text_and_says_so(server):
    async with connect_as_extension(f"ws://localhost:{server.port}") as ws:
        task = asyncio.create_task(answering(ws, [{"target": "div", "text": "x" * 50}]))
        answer = await call(server, "content_read", tab_id=1, selector="#panel", max_length=10)
        task.cancel()
    assert "x" * 10 + "\\n" in answer.replace("\n", "\\n") and "Cut at 10 of 50" in answer
