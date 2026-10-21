"""
Tests for Thunderbird as a connected browser: the app it reports in hello, and
the thunderbird_capture_window tool

The Thunderbird add-on itself is checked by hand against a real Thunderbird;
these stub the socket and the server.
"""

import base64
from datetime import datetime
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


@pytest.mark.asyncio
async def test_snapshot_lists_refs_labels_and_where_they_are():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"total": 2, "elements": [
        {"ref": "r1", "role": "toolbarbutton", "label": "Get Messages", "x": 40, "y": 20}]}}
    result = await call(server, "thunderbird_snapshot", max_elements=1)

    assert server.sent_request["data"] == {"maxElements": 1}
    assert 'r1 toolbarbutton "Get Messages" at 40,20' in result
    assert "first 1 of 2" in result


@pytest.mark.asyncio
async def test_click_needs_a_ref_or_text():
    server = StubServer("thunderbird")

    assert "Give a ref" in await call(server, "thunderbird_click")
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_click_sends_the_target_and_says_what_it_clicked():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"role": "treeitem", "label": "Inbox"}}
    result = await call(server, "thunderbird_click", text="Inbox", double_click=True, status="Opening the inbox")

    assert server.sent_request["action"] == "thunderbird.click"
    assert server.sent_request["data"] == {"ref": None, "text": "Inbox", "doubleClick": True, "rightClick": False,
                                           "status": "Opening the inbox"}
    assert result == 'Double-clicked treeitem "Inbox"'


@pytest.mark.asyncio
async def test_click_passes_on_the_add_on_refusal():
    server = StubServer("thunderbird")
    server.response = {"type": "error", "data": {"message": "Clicking in Thunderbird is off."}}

    assert "Failed to click in Thunderbird: Clicking in Thunderbird is off." in await call(
        server, "thunderbird_click", ref="r1")


@pytest.mark.asyncio
async def test_update_addon_refuses_a_path_that_is_not_an_xpi(tmp_path):
    server = StubServer("thunderbird")
    text_file = tmp_path / "notes.txt"
    text_file.write_text("x")

    assert "is not an .xpi file" in await call(server, "thunderbird_update_addon", path=str(text_file))
    assert "is not an .xpi file" in await call(server, "thunderbird_update_addon", path=str(tmp_path / "gone.xpi"))
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_update_addon_sends_the_absolute_path_and_reports_versions(tmp_path):
    server = StubServer("thunderbird")
    xpi = tmp_path / "smart-inbox-0.2.1.xpi"
    xpi.write_bytes(b"PK")
    server.response = {"type": "response", "data": {"id": "smart-inbox@dorzairi.dev", "name": "Smart Inbox",
                                                    "from": "0.2.0", "to": "0.2.1"}}
    result = await call(server, "thunderbird_update_addon", path=str(xpi))

    assert server.sent_request["data"] == {"path": str(xpi)}
    assert result == "Updated Smart Inbox (smart-inbox@dorzairi.dev) from 0.2.0 to 0.2.1"


@pytest.mark.asyncio
async def test_updating_the_bridge_itself_says_it_restarts(tmp_path):
    server = StubServer("thunderbird")
    xpi = tmp_path / "bridge.xpi"
    xpi.write_bytes(b"PK")
    server.response = {"type": "response", "data": {"id": "tbmcp@dorzairi.dev", "name": "MCP Bridge for Thunderbird",
                                                    "from": "0.6.0", "to": "0.7.0", "restarting": True}}

    assert "restarting and reconnects shortly" in await call(server, "thunderbird_update_addon", path=str(xpi))


@pytest.mark.asyncio
async def test_type_sends_the_value_and_reports_what_the_field_holds():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"label": "Search", "value": "Rappi"}}
    result = await call(server, "thunderbird_type", value="Rappi", text="Search", submit=True)

    assert server.sent_request["action"] == "thunderbird.type"
    assert server.sent_request["data"] == {"ref": None, "text": "Search", "value": "Rappi", "clear": True,
                                           "submit": True, "status": ""}
    assert result == 'Typed "Rappi" into "Search" and pressed Enter'


@pytest.mark.asyncio
async def test_keys_that_trash_mail_are_refused_without_asking():
    server = StubServer("thunderbird")

    for key in ("Delete", "Backspace"):
        assert "is not allowed" in await call(server, "thunderbird_press_key", key=key)
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_space_is_sent_as_the_space_character():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"key": " ", "on": "Inbox"}}
    await call(server, "thunderbird_press_key", key="Space")

    assert server.sent_request["data"]["key"] == " "


@pytest.mark.asyncio
async def test_console_lists_messages_with_level_and_source():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"total": 3, "messages": [
        {"time": "2026-10-20T13:05:12.000Z", "level": "error", "text": "x is undefined",
         "source": "moz-extension://abc/inbox.js:12"}]}}
    result = await call(server, "thunderbird_console", contains="inbox.js", errors_only=True, limit=1)

    assert server.sent_request["data"] == {"sinceMs": 600000, "contains": "inbox.js", "errorsOnly": True, "limit": 1}
    local = datetime.fromisoformat("2026-10-20T13:05:12+00:00").astimezone()
    assert f"{local:%H:%M:%S} error: x is undefined  (moz-extension://abc/inbox.js:12)" in result
    assert "newest 1 of 3" in result


@pytest.mark.asyncio
async def test_an_empty_console_says_so():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"total": 0, "messages": []}}

    assert await call(server, "thunderbird_console", since_minutes=5) == "No console messages in the last 5 minutes"


@pytest.mark.asyncio
async def test_click_at_a_point_scales_it_to_the_window():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"role": "browser", "label": ""}}
    result = await call(server, "thunderbird_click", x=400, y=200, screenshot_scale=0.8)

    assert server.sent_request["data"]["x"] == 500
    assert server.sent_request["data"]["y"] == 250
    assert server.sent_request["data"]["hover"] is False
    assert result == "Clicked at 400,200 (over browser)"


@pytest.mark.asyncio
async def test_click_at_a_point_needs_both_coordinates():
    server = StubServer("thunderbird")

    assert "Give both x and y" in await call(server, "thunderbird_click", x=10)
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_hover_only_moves_the_mouse():
    server = StubServer("thunderbird")
    server.response = {"type": "response", "data": {"role": "browser", "label": ""}}

    assert (await call(server, "thunderbird_click", x=10, y=20, hover=True)).startswith("Moved the mouse at 10,20")
