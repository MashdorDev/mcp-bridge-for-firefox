"""
Tests for the paste guard, click by visible text and where screenshots go

The guard keeps the person's own clipboard off pages: a paste goes through
only while the clipboard still holds what the agent copied. The clipboard is
stubbed by its change stamp (Windows' sequence number, or the add-on's hash).
"""

import base64
import os

import pytest

import test_imports  # Automatic path setup
from server import mcp_tools
from server.mcp_tools import FoxMCPTools, SCREENSHOT_DIR
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image


class StubWebSocketServer:
    """Answers every request with a canned response, and the add-on's
    clipboard hash with the stub clipboard's stamp"""

    def __init__(self, clipboard):
        self.requests = []
        self.response = {"type": "response", "data": {}}
        self.clipboard = clipboard

    async def send_request_and_wait(self, request):
        self.requests.append(request)
        if request.get("action") == "content.clipboard_fingerprint":
            return {"type": "response", "data": {"fingerprint": self.clipboard["stamp"]}}
        if request.get("action") == "content.press_key" and request["data"]["key"].lower().endswith("+c"):
            self.clipboard["stamp"] += self.clipboard["copy_changes"]
        return self.response


@pytest.fixture
def clipboard(monkeypatch):
    state = {"stamp": 1, "copy_changes": 1, "idle": 3600}
    monkeypatch.setattr(mcp_tools, "clipboard_sequence", lambda: state["stamp"], raising=False)
    monkeypatch.setattr(mcp_tools, "seconds_idle", lambda: state["idle"], raising=False)
    monkeypatch.setattr(mcp_tools, "COPY_WAIT_S", 0.3)
    return state


@pytest.fixture
def server(clipboard):
    return StubWebSocketServer(clipboard)


@pytest.fixture
def call_tool(server):
    tools = FoxMCPTools(server)

    async def call(name, **kwargs):
        tool = await tools.mcp.get_tool(name)
        try:
            return await tool.fn(**kwargs)
        except ToolError as error:
            return str(error)

    return call


def sent(server, action):
    return [r for r in server.requests if r.get("action") == action]


class TestPasteGuard:

    @pytest.mark.asyncio
    async def test_a_copy_while_the_person_is_at_the_computer_may_be_theirs(self, monkeypatch, call_tool, clipboard):
        monkeypatch.setattr(mcp_tools, "WINDOW_INPUT", True)
        clipboard["idle"] = 0
        result = await call_tool("content_press_key", tab_id=4, key="Control+c")
        assert "isn't taken as the agent's copy" in result
        assert (await call_tool("content_press_key", tab_id=4, key="Control+v")).startswith("Failed to paste")

    @pytest.mark.asyncio
    async def test_a_copy_while_the_person_is_away_is_the_agents(self, monkeypatch, call_tool):
        monkeypatch.setattr(mcp_tools, "WINDOW_INPUT", True)
        assert "clipboard now holds the copy" in await call_tool("content_press_key", tab_id=4, key="Control+c")
        assert not (await call_tool("content_press_key", tab_id=4, key="Control+v")).startswith("Failed to paste")

    @pytest.mark.asyncio
    async def test_the_persons_clipboard_is_not_pasted(self, call_tool, server):
        result = await call_tool("content_press_key", tab_id=4, key="Control+v")
        assert result.startswith("Failed to paste")
        assert not sent(server, "content.press_key")

    @pytest.mark.asyncio
    async def test_what_the_agent_copied_is_pasted(self, call_tool, server):
        copied = await call_tool("content_press_key", tab_id=4, key="Control+c")
        assert "The clipboard now holds the copy" in copied
        pasted = await call_tool("content_press_key", tab_id=4, key="Control+v")
        assert not pasted.startswith("Failed")
        assert sent(server, "content.press_key")[-1]["data"]["key"] == "Control+v"

    @pytest.mark.asyncio
    async def test_a_copy_that_changed_nothing_says_so_and_cannot_be_pasted(self, call_tool, clipboard):
        clipboard["copy_changes"] = 0
        copied = await call_tool("content_press_key", tab_id=4, key="Control+c")
        assert "Nothing was copied" in copied
        assert (await call_tool("content_press_key", tab_id=4, key="Control+v")).startswith("Failed to paste")

    @pytest.mark.asyncio
    async def test_the_person_copying_something_after_the_agent_blocks_the_paste(self, call_tool, clipboard):
        await call_tool("content_press_key", tab_id=4, key="Control+c")
        clipboard["stamp"] += 1  # the person copied a message meanwhile
        assert (await call_tool("content_paste", tab_id=4)).startswith("Failed to paste")

    @pytest.mark.asyncio
    async def test_the_person_can_ask_for_their_own_clipboard(self, call_tool, server):
        result = await call_tool("content_press_key", tab_id=4, key="Shift+Insert", allow_clipboard=True)
        assert not result.startswith("Failed")


class TestClickByText:

    @pytest.mark.asyncio
    async def test_the_visible_text_is_sent_as_the_target(self, call_tool, server):
        server.response = {"type": "response", "data": {
            "clicked": '[r3] button "Send"', "effect": "closed a menu or dialog"}}
        result = await call_tool("content_click", tab_id=4, text="Send")
        request = sent(server, "content.click")[-1]
        assert request["data"]["targetName"] == "Send"
        assert result == 'Clicked [r3] button "Send": closed a menu or dialog'


class TestScreenshots:

    @pytest.mark.asyncio
    async def test_a_bare_file_name_goes_to_the_temporary_folder(self, call_tool, server):
        png = base64.b64encode(b"\x89PNG fake").decode()
        server.response = {"type": "response", "data": {"dataUrl": f"data:image/png;base64,{png}", "format": "png"}}
        result = await call_tool("tabs_capture_screenshot", filename="shot.png")
        saved = os.path.join(SCREENSHOT_DIR, "shot.png")
        assert saved in result
        assert open(saved, "rb").read() == b"\x89PNG fake"

    @pytest.mark.asyncio
    async def test_without_a_file_name_the_image_comes_back(self, call_tool, server):
        png = base64.b64encode(b"\x89PNG fake").decode()
        server.response = {"type": "response", "data": {"dataUrl": f"data:image/png;base64,{png}", "format": "png"}}
        result = await call_tool("tabs_capture_screenshot")
        assert isinstance(result, Image)

    @pytest.mark.asyncio
    async def test_by_default_the_last_tab_used_is_captured(self, call_tool, server):
        """Not the focused window, which may be another app the person is in"""
        server.last_agent_tab = lambda: 42
        png = base64.b64encode(b"\x89PNG fake").decode()
        server.response = {"type": "response",
                           "data": {"dataUrl": f"data:image/png;base64,{png}", "format": "png", "tabId": 42, "windowId": 7}}
        result = await call_tool("tabs_capture_screenshot", filename="tab.png")
        sent = server.requests[-1]["data"]
        assert sent["tabId"] == 42 and sent["tabIdOptional"] is True
        assert "(tab 42, window 7," in result

    @pytest.mark.asyncio
    async def test_a_named_window_or_tab_wins_over_the_default(self, call_tool, server):
        server.last_agent_tab = lambda: 42
        png = base64.b64encode(b"\x89PNG fake").decode()
        server.response = {"type": "response", "data": {"dataUrl": f"data:image/png;base64,{png}", "format": "png"}}
        await call_tool("tabs_capture_screenshot", window_id=3)
        assert "tabId" not in server.requests[-1]["data"]
        await call_tool("tabs_capture_screenshot", tab_id=9)
        sent = server.requests[-1]["data"]
        assert sent["tabId"] == 9 and "tabIdOptional" not in sent
