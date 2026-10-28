"""
Tests for the page interaction tools: content_snapshot, content_click,
content_type and content_press_key

These stub the WebSocket server, so they check the request each tool builds and
how it renders the extension's answer. The events the content script dispatches
are exercised against real Firefox, where a stub could not tell a click that
opens a mousedown menu from one that does not.
"""

import base64

import pytest

import test_imports  # Automatic path setup
from server.mcp_tools import FoxMCPTools, UPLOAD_CHUNK_BYTES
from fastmcp.exceptions import ToolError


class StubWebSocketServer:
    """Records the request and replays a canned response"""

    def __init__(self):
        self.sent_request = None
        self.response = {"type": "response", "data": {}}

    async def send_request_and_wait(self, request):
        self.sent_request = request
        return self.response


@pytest.fixture
def stub_server():
    return StubWebSocketServer()


@pytest.fixture
def call_tool(stub_server):
    tools = FoxMCPTools(stub_server)

    async def call(name, **kwargs):
        tool = await tools.mcp.get_tool(name)
        try:
            return await tool.fn(**kwargs)
        except ToolError as error:  # what the client sees as the error text
            return str(error)

    return call


class TestSnapshot:

    @pytest.mark.asyncio
    async def test_lists_elements_under_the_page_title(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {
            "title": "Form", "url": "https://example.org/form",
            "snapshot": '[r1] input "Name" (type=text)\n[r2] button "Save"'}}

        result = await call_tool("content_snapshot", tab_id=4)

        assert stub_server.sent_request["action"] == "content.snapshot"
        assert stub_server.sent_request["data"] == {"tabId": 4, "maxElements": 300}
        assert result.startswith("Snapshot of Form (https://example.org/form):")
        assert '[r2] button "Save"' in result


class TestClick:

    @pytest.mark.asyncio
    async def test_sends_the_ref(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {"clicked": '[r2] button "Save"'}}

        result = await call_tool("content_click", tab_id=4, ref="r2")

        assert stub_server.sent_request["action"] == "content.click"
        assert stub_server.sent_request["data"] == {
            "tabId": 4, "ref": "r2", "selector": None, "doubleClick": False, "rightClick": False}
        assert result == 'Clicked [r2] button "Save"'

    @pytest.mark.asyncio
    async def test_double_click(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {"clicked": '[r9] row "Characters"'}}

        result = await call_tool("content_click", tab_id=4, ref="r9", double_click=True)

        assert stub_server.sent_request["data"]["doubleClick"] is True
        assert result == 'Double-clicked [r9] row "Characters"'

    @pytest.mark.asyncio
    async def test_requires_a_target(self, call_tool, stub_server):
        with pytest.raises(ValueError, match="ref"):
            await call_tool("content_click", tab_id=4)
        assert stub_server.sent_request is None

    @pytest.mark.asyncio
    async def test_reports_a_stale_ref(self, call_tool, stub_server):
        stub_server.response = {"type": "error", "data": {
            "message": "No element with ref r9; take a new snapshot"}}

        result = await call_tool("content_click", tab_id=4, ref="r9")

        assert result == "Failed to click: No element with ref r9; take a new snapshot"


class TestType:

    @pytest.mark.asyncio
    async def test_replaces_by_default_and_can_submit(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {"typed": '[r1] input "Dor"'}}

        await call_tool("content_type", tab_id=4, selector="#name", text="Dor", submit=True)

        assert stub_server.sent_request["action"] == "content.type"
        assert stub_server.sent_request["data"] == {
            "tabId": 4, "ref": None, "selector": "#name",
            "text": "Dor", "clear": True, "submit": True, "commit": False}


class TestPressKey:

    @pytest.mark.asyncio
    async def test_targets_the_focused_element_when_none_given(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {"pressed": "Escape"}}

        result = await call_tool("content_press_key", tab_id=4, key="Escape")

        assert stub_server.sent_request["action"] == "content.press_key"
        assert stub_server.sent_request["data"] == {
            "tabId": 4, "key": "Escape", "ref": None, "selector": None}
        assert result == "Pressed Escape"


class RecordingWebSocketServer:
    """Records every request; answers capture and finish the way the extension does"""

    def __init__(self):
        self.requests = []

    async def send_request_and_wait(self, request):
        self.requests.append(request)
        if request["action"] == "content.capture_file_input":
            return {"type": "response", "data": {"ref": "r7", "multiple": False, "directory": True}}
        if request["action"] == "content.upload_finish":
            return {"type": "response", "data": {
                "count": 2, "paths": ["Assets/a.txt", "Assets/sub/b.bin"], "target": "[r7] input"}}
        return {"type": "response", "data": {"ok": True}}

    def actions(self):
        return [r["action"] for r in self.requests]


class TestUploadFiles:

    @pytest.fixture
    def recorder(self):
        return RecordingWebSocketServer()

    @pytest.fixture
    def upload(self, recorder):
        tools = FoxMCPTools(recorder)

        async def call(**kwargs):
            tool = await tools.mcp.get_tool("content_upload_files")
            try:
                return await tool.fn(**kwargs)
            except ToolError as error:  # what the client sees as the error text
                return str(error)

        return call

    @pytest.fixture
    def folder(self, tmp_path):
        root = tmp_path / "Assets"
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("alpha")
        (root / "sub" / "b.bin").write_bytes(b"\x00" * (UPLOAD_CHUNK_BYTES + 10))
        return root

    @pytest.mark.asyncio
    async def test_folder_files_keep_paths_under_the_folder_name(self, upload, recorder, folder):
        await upload(tab_id=4, path=str(folder), ref="r2")

        begin = recorder.requests[0]
        assert begin["action"] == "content.upload_begin"
        assert [(f["name"], f["relativePath"]) for f in begin["data"]["files"]] == [
            ("a.txt", "Assets/a.txt"), ("b.bin", "Assets/sub/b.bin")]

    @pytest.mark.asyncio
    async def test_large_files_are_split_into_chunks(self, upload, recorder, folder):
        await upload(tab_id=4, path=str(folder), ref="r2")

        chunks = [r["data"] for r in recorder.requests if r["action"] == "content.upload_chunk"]
        assert [c["fileIndex"] for c in chunks] == [0, 1, 1]
        sizes = [len(base64.b64decode(c["data"])) for c in chunks]
        assert sizes == [5, UPLOAD_CHUNK_BYTES, 10]
        finish = recorder.requests[-1]
        assert finish["action"] == "content.upload_finish"
        assert finish["data"]["ref"] == "r2"

    @pytest.mark.asyncio
    async def test_trigger_fills_the_input_it_opens(self, upload, recorder, folder):
        result = await upload(tab_id=4, path=str(folder), trigger_ref="r5")

        assert recorder.actions()[0] == "content.capture_file_input"
        assert recorder.requests[0]["data"]["triggerRef"] == "r5"
        assert recorder.requests[-1]["data"]["ref"] == "r7"
        assert result.startswith("Set 2 file(s) on [r7] input")

    @pytest.mark.asyncio
    async def test_single_file_has_no_relative_path(self, upload, recorder, folder):
        await upload(tab_id=4, path=str(folder / "a.txt"), selector="#plain")

        assert recorder.requests[0]["data"]["files"] == [
            {"name": "a.txt", "relativePath": "", "type": "text/plain"}]

    @pytest.mark.asyncio
    async def test_files_carry_their_mime_type(self, upload, recorder, tmp_path):
        # Miro's image upload never finished for a file with an empty type
        image = tmp_path / "shot.png"
        image.write_bytes(b"not really a png")
        await upload(tab_id=4, path=str(image), selector="#plain")

        assert recorder.requests[0]["data"]["files"][0]["type"] == "image/png"

    @pytest.mark.asyncio
    async def test_missing_path_sends_nothing(self, upload, recorder, tmp_path):
        result = await upload(tab_id=4, path=str(tmp_path / "nope"), ref="r2")

        assert result.endswith("does not exist")
        assert recorder.requests == []


class TestFailuresAreErrors:
    """A failed step reaches the client as an MCP error, not as a result it
    would take for success"""

    @pytest.mark.asyncio
    async def test_a_failed_type_raises(self, stub_server):
        stub_server.response = {"type": "error", "error": "Only part of the text arrived"}
        tool = await FoxMCPTools(stub_server).mcp.get_tool("content_type")

        with pytest.raises(ToolError, match="Failed to type: Only part of the text arrived"):
            await tool.fn(tab_id=4, text="hello", selector="#t")

    @pytest.mark.asyncio
    async def test_a_successful_type_returns(self, stub_server):
        stub_server.response = {"type": "response", "data": {"typed": '[r1] input "Name"'}}
        tool = await FoxMCPTools(stub_server).mcp.get_tool("content_type")

        assert await tool.fn(tab_id=4, text="hello", selector="#t") == 'Typed into [r1] input "Name"'

    @pytest.mark.asyncio
    @pytest.mark.parametrize("emptied, says", [
        (True, "the field emptied, so it likely took the text as an entry"),
        (False, "the text is still in the field"),
    ])
    async def test_commit_says_whether_the_field_took_the_text(self, stub_server, emptied, says):
        # Chip inputs accept an address only when focus leaves them, and empty the field
        stub_server.response = {"type": "response",
                                 "data": {"typed": '[r1] input ""', "committed": True, "emptied": emptied}}
        tool = await FoxMCPTools(stub_server).mcp.get_tool("content_type")

        answer = await tool.fn(tab_id=4, text="a@example.org", selector="#t", commit=True)
        assert answer == f'Typed into [r1] input "", and left it; {says}'
        assert stub_server.sent_request["data"]["commit"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data, says", [
        # A <select> holds a choice, not text that could become an entry
        ({"typed": '[r1] select "FR"', "committed": True, "emptied": None}, ", and left it"),
        # An add-on from before commit ignores it and leaves the field focused
        ({"typed": '[r1] select "FR"'}, ", but did not leave it: the add-on is too old for commit, update it"),
    ])
    async def test_commit_on_a_select_or_an_old_add_on(self, stub_server, data, says):
        stub_server.response = {"type": "response", "data": data}
        tool = await FoxMCPTools(stub_server).mcp.get_tool("content_type")

        answer = await tool.fn(tab_id=4, text="FR", selector="#t", commit=True)
        assert answer == f'Typed into [r1] select "FR"{says}'


class TestRealInputOnly:
    """With --real-input-only and no real input, nothing scripted reaches the page"""

    @pytest.fixture
    def strict(self, stub_server):
        stub_server.real_input_only = True
        return stub_server

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name, kwargs, action", [
        ("content_click", {"ref": "r2"}, "click"),
        ("content_click", {"x": 10, "y": 20}, "click"),
        ("content_hover", {"ref": "r2"}, "hover"),
        ("content_drag", {"from_ref": "r1", "to_ref": "r2"}, "drag"),
        ("content_type", {"ref": "r1", "text": "hi"}, "type"),
        ("content_press_key", {"key": "Enter"}, "press Enter"),
    ])
    async def test_refuses_the_scripted_fallback(self, call_tool, strict, name, kwargs, action):
        result = await call_tool(name, tab_id=4, **kwargs)

        assert result.startswith(f"Did not {action}: the server runs with real input only")
        assert strict.sent_request is None

    @pytest.mark.asyncio
    async def test_scripted_events_still_go_out_when_off(self, call_tool, stub_server):
        stub_server.response = {"type": "response", "data": {"clicked": '[r2] button "Save"'}}

        result = await call_tool("content_click", tab_id=4, ref="r2")

        assert stub_server.sent_request["action"] == "content.click"
        assert result == 'Clicked [r2] button "Save"'
