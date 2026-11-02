"""
What heavy single-page apps do, against real Firefox, with no site in sight

Blackboard Ultra and Teams broke the same ways: panels that fill in late or never,
modals stuck invisible over the page, lists that draw a few of their rows, routes
that change after the click returns, and clicks that report success while nothing
happens. Each page here is that behaviour on its own.
"""

import asyncio
import re

import pytest
import pytest_asyncio

import test_imports  # Automatic path setup
from mcp_client_harness import DirectMCPTestClient


LOAD_TIMEOUT = 20.0


class Page:
    def __init__(self, client, tab_id):
        self.client = client
        self.tab_id = tab_id

    async def tool(self, name, **args):
        result = await self.client.call_tool(name, {"tab_id": self.tab_id, **args})
        assert result.get("success", False), f"{name} should succeed: {result}"
        return str(result.get("content", ""))

    async def build(self, html, script=""):
        """Set the page's own DOM and handlers: page_realm, so they are what the page itself runs"""
        escaped = html.replace("\\", "\\\\").replace("`", "\\`")
        await self.tool("content_execute_script", page_realm=True, code=f"document.body.innerHTML = `{escaped}`;\n{script}\nreturn 1")


@pytest_asyncio.fixture
async def page(server_with_extension):
    client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
    await client.connect()
    created = await client.call_tool("tabs_create", {"url": "https://example.org/heavy", "active": True})
    tab_id = int(re.search(r'ID (\d+)', str(created.get("content"))).group(1))
    try:
        deadline = asyncio.get_running_loop().time() + LOAD_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            state = await client.call_tool("content_execute_script", {
                "tab_id": tab_id, "code": "location.pathname + ' ' + document.readyState"})
            if "/heavy complete" in str(state.get("content")):
                break
            await asyncio.sleep(0.5)
        yield Page(client, tab_id)
    finally:
        await client.call_tool("tabs_close", {"tab_id": tab_id})
        await client.disconnect()


@pytest.mark.asyncio
async def test_a_script_left_waiting_by_a_navigation_says_so_at_once(page):
    """It used to wait out the 30 s limit and call the page hung"""
    waiting = asyncio.create_task(page.client.call_tool("content_execute_script", {
        "tab_id": page.tab_id, "code": "await new Promise(r => setTimeout(r, 25000)); return 'finished'"}))
    await asyncio.sleep(1)
    await page.tool("navigation_go_to_url", url="https://example.org/heavy-next")
    answer = await asyncio.wait_for(waiting, 20)
    assert "navigated away" in str(answer), answer


@pytest.mark.asyncio
async def test_a_jump_within_the_page_leaves_a_waiting_script_running(page):
    said = await page.tool("content_execute_script", code=(
        "history.pushState({}, '', '/heavy/jump'); location.hash = 'x';"
        " await new Promise(r => setTimeout(r, 2500)); return 'finished'"))
    assert "finished" in said, said


@pytest.mark.asyncio
async def test_a_script_that_navigates_as_its_last_act_still_answers(page):
    said = await page.tool("content_execute_script", code=(
        "setTimeout(() => { location.href = 'https://example.org/heavy-after' }, 0); return 'answered'"))
    assert "answered" in said, said
