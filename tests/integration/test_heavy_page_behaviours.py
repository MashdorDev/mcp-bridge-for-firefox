"""
What heavy single-page apps do, against real Firefox, with no site in sight

Blackboard Ultra and Teams broke the same ways: panels that fill in late or never,
modals stuck invisible over the page, lists that draw a few of their rows, routes
that change after the click returns, and clicks that report success while nothing
happens. Each page here is that behaviour on its own.
"""

import asyncio
import re
import struct

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


ROWS = "<div role=listbox><div id=dead role=option>Dead row</div><div id=pick role=option aria-selected=false>Pick row</div><div id=pane_row role=option>Pane row</div></div><div id=pane>old chat</div>"
ROW_HANDLERS = """
document.getElementById('pick').onclick = e => e.target.setAttribute('aria-selected', 'true');
document.getElementById('pane_row').onclick = () => { document.getElementById('pane').textContent = 'new chat' };
"""


@pytest.mark.asyncio
async def test_a_click_says_what_it_did_to_a_list_row(page):
    await page.build(ROWS, ROW_HANDLERS)
    assert "no change seen" in await page.tool("content_click", selector="#dead")
    assert "it is now selected" in await page.tool("content_click", selector="#pick")
    assert "content changed" in await page.tool("content_click", selector="#pane_row")


@pytest.mark.asyncio
async def test_a_label_around_a_hidden_checkbox_reports_the_toggle(page):
    await page.build("<label id=toggle style='display:block;width:80px;height:30px'>Dark<input type=checkbox style='display:none'></label>")
    assert "it is now checked" in await page.tool("content_click", selector="#toggle")


@pytest.mark.asyncio
async def test_an_attribute_on_the_root_element_counts_as_a_change(page):
    await page.build("<button id=theme>Theme</button>", "document.getElementById('theme').onclick = () => document.documentElement.setAttribute('data-theme', 'dark');")
    assert "content changed" in await page.tool("content_click", selector="#theme")


@pytest.mark.asyncio
async def test_a_modal_stuck_invisible_is_named_when_it_takes_a_click(page):
    await page.build(
        "<button id=save onclick=\"document.title='saved'\">Save</button>"
        "<div id=stuck role=dialog aria-modal=true style='position:fixed;inset:0;opacity:0;background:#000'>"
        "<p>Modify the limit?</p><button>Continue</button></div>")
    clicked = await page.tool("content_click", selector="#save")
    assert "covered by an invisible" in clicked, clicked
    dialogs = await page.tool("content_dialogs")
    assert "invisible" in dialogs and "takes the page's clicks" in dialogs and "Continue" in dialogs, dialogs


@pytest.mark.asyncio
async def test_a_panel_that_fills_in_late_is_told_from_a_filled_one(page):
    await page.build(
        "<aside id=panel role=dialog aria-label=Details style='position:fixed;right:0;top:0;width:300px;height:300px;background:#eee'>"
        "<button aria-label=Close>x</button></aside>",
        "setTimeout(() => { document.getElementById('panel').insertAdjacentHTML('beforeend', '<p>Loaded details</p>') }, 1500);")
    assert "never have loaded" in await page.tool("content_dialogs")
    await page.tool("content_wait_for", text="Loaded details", timeout=10)
    assert "never have loaded" not in await page.tool("content_dialogs")


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [
    "<h2>Delete this file?</h2><button>Delete</button><button>Cancel</button>",
    "<h2>Saved</h2><button>OK</button>",
    "<h2>March</h2>" + "".join(f"<button>{day}</button>" for day in range(1, 8)),
    "<a href='/one'>One</a><a href='/two'>Two</a>",
    "<h2>Status</h2><svg width=20 height=20></svg><button aria-label=Close>x</button>",
    "<h2>Card</h2><my-card></my-card><button aria-label=Close>x</button>",
    "<h2>Uploading your file</h2><button>Cancel</button>",
])
async def test_a_complete_dialog_is_not_called_unfilled(page, inside):
    await page.build(f"<div role=dialog aria-label=Box style='position:fixed;left:0;top:0;width:300px;height:300px'>{inside}</div>")
    assert "never have loaded" not in await page.tool("content_dialogs")


@pytest.mark.asyncio
async def test_a_list_that_draws_few_rows_says_how_many_it_has(page):
    await page.build("<div role=grid aria-rowcount=500 aria-label=Submissions style='height:90px;overflow:auto'>"
                     + "".join(f"<div role=row><span role=gridcell>Row {n}</span></div>" for n in range(3)) + "</div>")
    snapshot = await page.tool("content_snapshot")
    assert "has 500 rows, 3 drawn" in snapshot, snapshot


@pytest.mark.asyncio
async def test_a_route_that_changes_after_the_click_can_be_waited_for(page):
    await page.build("<a id=go href='#'>Open</a><main id=main>first view</main>", """
document.getElementById('go').onclick = event => {
  event.preventDefault();
  setTimeout(() => { history.pushState({}, '', '/heavy/next'); document.getElementById('main').textContent = 'second view' }, 1500);
};
""")
    await page.tool("content_click", selector="#go")
    assert "first view" in await page.tool("content_read", selector="#main")
    await page.tool("content_wait_for", text="second view", timeout=10)
    assert "/heavy/next" in await page.tool("content_execute_script", code="location.pathname")


@pytest.mark.asyncio
async def test_a_saved_screenshot_says_its_scale_when_it_has_one(page, tmp_path):
    path = tmp_path / "shot.png"
    said = await page.tool("tabs_capture_screenshot", filename=str(path))
    width = struct.unpack(">I", path.read_bytes()[16:20])[0]
    css_width = float(await page.tool("content_execute_script", code="innerWidth"))
    match = re.search(r"([\d.]+) image pixels per CSS pixel", said)
    ratio = float(match.group(1)) if match else 1
    assert abs(width - css_width * ratio) <= ratio + 1, f"{width} wide for {css_width} CSS pixels: {said}"


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


@pytest.mark.asyncio
async def test_a_dialog_that_is_itself_a_custom_element_is_not_called_unfilled(page):
    await page.build("<my-panel role=dialog aria-label=Details style='display:block;position:fixed;left:0;top:0;width:300px;height:300px'></my-panel>")
    assert "never have loaded" not in await page.tool("content_dialogs")
