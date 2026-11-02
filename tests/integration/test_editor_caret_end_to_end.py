"""
Where text added to a rich editor goes, against real Firefox

A caret on the editor element itself, after its last paragraph, is not where a
person's caret would be. ProseMirror typed there makes a paragraph per piece, and
Draft.js throws and stops drawing, so the page shows other text than the editor
holds while the tool reports success.
"""

import asyncio
import re

import pytest

import test_imports  # Automatic path setup
from mcp_client_harness import DirectMCPTestClient


LOAD_TIMEOUT = 20.0
EDITOR = ("document.body.innerHTML = '<div id=\"ed\" contenteditable><p>one</p><p>two</p></div>';"
          " return document.getElementById('ed').childElementCount")
READ = ("const ed = document.getElementById('ed'), caret = getSelection().anchorNode;"
        " return [ed.childElementCount, ed.lastElementChild.textContent,"
        " caret.nodeType === Node.TEXT_NODE ? 'in text' : 'on ' + caret.nodeName].join('|')")


async def call(client, name, args):
    result = await client.call_tool(name, args)
    assert result.get("success", False), f"{name} should succeed: {result}"
    return result.get("content", "")


async def editor_tab(client):
    created = await call(client, "tabs_create", {"url": "https://example.org/caret", "active": True})
    tab_id = int(re.search(r'ID (\d+)', created).group(1))
    deadline = asyncio.get_running_loop().time() + LOAD_TIMEOUT
    state = ""
    while asyncio.get_running_loop().time() < deadline:
        state = (await client.call_tool("content_execute_script", {
            "tab_id": tab_id, "code": "location.pathname + ' ' + document.readyState"})).get("content", "")
        if "/caret complete" in state:
            await call(client, "content_execute_script", {"tab_id": tab_id, "code": EDITOR})
            return tab_id
        await asyncio.sleep(0.5)
    raise AssertionError(f"The page did not load within {LOAD_TIMEOUT}s, last state: {state}")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["#ed", "#ed p"])
async def test_added_text_goes_at_the_end_of_the_last_paragraph(server_with_extension, target):
    """Whichever part of the editor is named, as a ref or selector can name one paragraph"""
    client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
    await client.connect()
    tab_id = None
    try:
        tab_id = await editor_tab(client)
        await call(client, "content_type", {"tab_id": tab_id, "selector": target, "text": "more", "clear": False})
        state = await call(client, "content_execute_script", {"tab_id": tab_id, "code": READ})
        assert "2|twomore|in text" in state, f"Expected two paragraphs, the text in the last, the caret in it: {state}"
    finally:
        if tab_id is not None:
            await client.call_tool("tabs_close", {"tab_id": tab_id})
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("html,expected", [
    ("<p>one</p><p><br></p>", "<p>one</p><p>more"),
    ("\\n<p>two</p>\\n", "<p>twomore</p>"),
    ("<p>hi <span contenteditable=false>@Dor</span></p>", "@Dor</span>more"),
    ("<p>one</p><p>&nbsp;</p>", "<p>one</p><p>"),
    ("<p>hi <img alt=x></p>", "<img alt=\"x\">more"),
    ("<p></p>", "<p>more</p>"),
])
async def test_added_text_goes_where_a_person_would_type_last(server_with_extension, html, expected):
    """A blank last line, whitespace from formatted source, and a mention that takes no caret"""
    client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
    await client.connect()
    tab_id = None
    try:
        tab_id = await editor_tab(client)
        await call(client, "content_execute_script", {"tab_id": tab_id, "code": f"document.getElementById('ed').innerHTML = '{html}'; return 1"})
        await call(client, "content_type", {"tab_id": tab_id, "selector": "#ed", "text": "more", "clear": False})
        state = await call(client, "content_execute_script", {"tab_id": tab_id, "code": "return document.getElementById('ed').innerHTML"})
        assert expected in state, state
    finally:
        if tab_id is not None:
            await client.call_tool("tabs_close", {"tab_id": tab_id})
        await client.disconnect()
