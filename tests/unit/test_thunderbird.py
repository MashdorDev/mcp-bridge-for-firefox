"""
Tests for Thunderbird as a connected browser: the app it reports in hello, and
the thunderbird_capture_window tool

The Thunderbird add-on itself is checked by hand against a real Thunderbird;
these stub the socket and the server.
"""

import base64
from unittest.mock import Mock

import pytest

import test_imports  # Automatic path setup
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from server.mcp_tools import FoxMCPTools
from server.server import BrowserConnection, FoxMCPServer

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


class StubServer:
    def __init__(self, app):
        self.browsers = {"tb": BrowserConnection(key="tb", websocket=None, name="Mail", app=app, paired=True)}
        self.active_browser = "tb"
        self.sent_request = None
        self.response = {"type": "response", "data": {"dataUrl": PNG, "width": 800, "height": 600}}

    async def send_request_and_wait(self, request):
        self.sent_request = request
        return self.response


async def call(server, name, **kwargs):
    tool = await FoxMCPTools(server).mcp.get_tool(name)
    try:
        return await tool.fn(**kwargs)
    except ToolError as error:
        return str(error)


async def capture(server, **kwargs):
    return await call(server, "thunderbird_capture_window", **kwargs)


def test_hello_records_thunderbird_and_names_it():
    server = FoxMCPServer(host="localhost", port=8765)
    socket = Mock()
    server._add_browser(socket)
    server._handle_hello(socket, {"instance_id": "abcd1234", "app": "thunderbird", "version": "0.1.0"})

    browser = server.browsers["abcd1234"]
    assert browser.app == "thunderbird"
    assert browser.name.startswith("Thunderbird ")


def test_hello_without_app_is_firefox():
    server = FoxMCPServer(host="localhost", port=8765)
    socket = Mock()
    server._add_browser(socket)
    server._handle_hello(socket, {"instance_id": "abcd1234", "name": "Work"})

    assert server.browsers["abcd1234"].app == "firefox"
    assert server.browsers["abcd1234"].name == "Work"


@pytest.mark.asyncio
async def test_capture_returns_the_image():
    server = StubServer("thunderbird")
    result = await capture(server, scale=0.5)

    assert server.sent_request["action"] == "thunderbird.captureWindow"
    assert server.sent_request["data"] == {"scale": 0.5}
    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_capture_is_refused_on_firefox_without_asking_it():
    server = StubServer("firefox")
    result = await capture(server)

    assert server.sent_request is None
    assert "browsers_select a Thunderbird browser first" in result


@pytest.mark.asyncio
async def test_a_firefox_tool_is_refused_on_thunderbird_without_asking_it():
    server = StubServer("thunderbird")
    result = await call(server, "history_query", query="example.org")

    assert server.sent_request is None
    assert '"Mail" is Thunderbird' in result
    assert "browsers_select a Firefox browser first" in result


@pytest.mark.asyncio
async def test_browser_tools_work_whichever_app_is_active():
    server = StubServer("thunderbird")

    listed = await call(server, "browsers_list")
    assert "Mail [Thunderbird] (active)" in listed
    assert ", paired," in listed


@pytest.mark.asyncio
async def test_capture_reports_the_add_on_error():
    server = StubServer("thunderbird")
    server.response = {"type": "error", "data": {"message": "No Thunderbird main window is open"}}

    assert "No Thunderbird main window is open" in await capture(server)


@pytest.mark.asyncio
async def test_capture_is_refused_until_thunderbird_is_paired():
    server = StubServer("thunderbird")
    server.browsers["tb"].paired = False
    result = await capture(server)

    assert server.sent_request is None
    assert "not paired" in result
