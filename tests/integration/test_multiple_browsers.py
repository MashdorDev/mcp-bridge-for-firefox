"""
Several Firefox profiles connected to one server at once

The server used to keep one connection and close it whenever another arrived.
Two profiles running the add-on then reconnected over each other every few
seconds, so a tab id from one call was gone by the next. Now every browser stays
connected, a newcomer never takes over, the chosen one stays chosen while it
is away, and browsers_select picks the one tools act on.
"""

import asyncio
import json

import pytest
import pytest_asyncio

import test_imports  # Automatic path setup
from server.server import FoxMCPServer
from port_coordinator import get_port_by_type
from test_config import connect_as_extension
from mcp_client_harness import DirectMCPTestClient


@pytest_asyncio.fixture
async def server():
    server = FoxMCPServer(
        host="localhost",
        port=get_port_by_type('test_individual'),
        mcp_port=get_port_by_type('test_mcp_individual'),
        start_mcp=False
    )
    server_task = asyncio.create_task(server.start_server())
    await asyncio.sleep(0.5)

    yield server

    await server.shutdown(server_task)


def uri(server):
    return f"ws://localhost:{server.port}"


async def hello(ws, instance_id, name, **more):
    await ws.send(json.dumps({"id": f"hello_{instance_id}", "type": "hello", "action": "browser.hello",
                              "data": {"instance_id": instance_id, "name": name, **more, "version": "1.4.3"}}))
    await asyncio.sleep(0.2)


async def received(ws, timeout=1.0):
    """The next message a connection gets, or None if nothing arrives"""
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except asyncio.TimeoutError:
        return None


async def ping(server):
    await server.send_to_extension({"id": "probe", "type": "request", "action": "ping", "data": {}})


@pytest.mark.asyncio
async def test_a_second_browser_does_not_take_over(server):
    """Both stay connected and the first stays active"""
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as other:
        await hello(main, "aaaa1111", "Main")
        await hello(other, "bbbb2222", "Lost Tribe")

        assert set(server.browsers) == {"aaaa1111", "bbbb2222"}
        assert server.active_browser == "aaaa1111"
        await ping(server)
        assert (await received(main))["id"] == "probe"
        assert await received(other, timeout=0.3) is None


@pytest.mark.asyncio
async def test_select_by_name_routes_requests(server):
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as other:
        await hello(main, "aaaa1111", "Main")
        await hello(other, "bbbb2222", "Lost Tribe")

        chosen = server.select_browser("lost tribe")
        assert chosen.key == "bbbb2222"
        await ping(server)
        assert (await received(other))["id"] == "probe"
        assert await received(main, timeout=0.3) is None

        server.select_browser("aaaa")  # the start of an id works too
        assert server.active_browser == "aaaa1111"


@pytest.mark.asyncio
async def test_an_unnamed_profile_is_named_from_its_id_not_its_order(server):
    async with connect_as_extension(uri(server)) as first, connect_as_extension(uri(server)) as second:
        await hello(first, "aaaa1111", "")
        await hello(second, "bbbb2222", "")
        await ping(server)
        assert sorted(b.name for b in server.browsers.values()) == ["Firefox aaaa", "Firefox bbbb"]


@pytest.mark.asyncio
async def test_an_agent_profile_is_preferred_until_one_is_chosen(server):
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as agent:
        await hello(main, "aaaa1111", "Main")
        assert server.active_browser == "aaaa1111"
        await hello(agent, "bbbb2222", "Agents", agent_profile=True)
        assert server.active_browser == "bbbb2222", "an agent profile should take over"
        server.select_browser("Main")
        await hello(agent, "bbbb2222", "Agents", agent_profile=True)
        assert server.active_browser == "aaaa1111", "a browser chosen by name stays chosen"


@pytest.mark.asyncio
async def test_ticking_agent_profile_on_a_connected_browser_takes_over(server):
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as agent:
        await hello(main, "aaaa1111", "Main")
        await hello(agent, "bbbb2222", "Agents")
        assert server.active_browser == "aaaa1111"
        await hello(agent, "bbbb2222", "Agents", agent_profile=True)
        assert server.active_browser == "bbbb2222"


@pytest.mark.asyncio
async def test_an_agent_profile_does_not_pull_work_out_of_a_browser_in_use(server):
    # Tab ids mean nothing in another profile
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as agent:
        await hello(main, "aaaa1111", "Main")
        server.note_agent_tab(None, 12)
        await hello(agent, "bbbb2222", "Agents", agent_profile=True)
        assert server.active_browser == "aaaa1111"


@pytest.mark.asyncio
async def test_two_agent_profiles_do_not_trade_places(server):
    async with connect_as_extension(uri(server)) as one, connect_as_extension(uri(server)) as two:
        await hello(one, "aaaa1111", "One", agent_profile=True)
        await hello(two, "bbbb2222", "Two", agent_profile=True)
        assert server.active_browser == "aaaa1111"
        await hello(two, "bbbb2222", "Two", agent_profile=True)
        assert server.active_browser == "aaaa1111"


@pytest.mark.asyncio
async def test_an_unknown_name_is_refused_with_the_choices(server):
    async with connect_as_extension(uri(server)) as main:
        await hello(main, "aaaa1111", "Main")
        with pytest.raises(ValueError, match='Connected: "Main"'):
            server.select_browser("Work")
        assert server.active_browser == "aaaa1111"


@pytest.mark.asyncio
async def test_the_chosen_browser_leaving_does_not_hand_over(server):
    """Tab ids of the chosen browser must never reach another one"""
    async with connect_as_extension(uri(server)) as other:
        await hello(other, "bbbb2222", "Main")
        async with connect_as_extension(uri(server)) as lost_tribe:
            await hello(lost_tribe, "aaaa1111", "Lost Tribe")
            server.select_browser("Lost Tribe")
        await asyncio.sleep(0.3)

        assert set(server.browsers) == {"bbbb2222"}
        assert server.active_browser == "aaaa1111"
        answer = await server.send_request_and_wait({"id": "nav", "type": "request", "action": "ping", "data": {}})
        assert 'The selected browser "Lost Tribe" is disconnected' in answer["error"]
        assert '"Main"' in answer["error"]
        assert await received(other, timeout=0.3) is None

        # The same profile coming back is acted on again
        async with connect_as_extension(uri(server)) as back:
            await hello(back, "aaaa1111", "Lost Tribe")
            assert server.active_browser == "aaaa1111"
            await ping(server)
            assert (await received(back))["id"] == "probe"


@pytest.mark.asyncio
async def test_another_profile_waits_while_the_chosen_one_is_away(server):
    async with connect_as_extension(uri(server)) as lost_tribe:
        await hello(lost_tribe, "aaaa1111", "Lost Tribe")
        server.select_browser("Lost Tribe")
    await asyncio.sleep(0.3)
    async with connect_as_extension(uri(server)) as main:
        await hello(main, "bbbb2222", "Main")
        assert server.active_browser == "aaaa1111"
        server.select_browser("Main")
        await ping(server)
        assert (await received(main))["id"] == "probe"


@pytest.mark.asyncio
async def test_a_browser_no_one_chose_is_replaced_on_an_empty_server(server):
    """One profile closed and another opened: nothing to confuse it with"""
    async with connect_as_extension(uri(server)) as first:
        await hello(first, "aaaa1111", "Main")
    await asyncio.sleep(0.3)
    async with connect_as_extension(uri(server)) as second:
        await hello(second, "bbbb2222", "Work")
        assert server.active_browser == "bbbb2222"


@pytest.mark.asyncio
async def test_a_profile_reconnecting_replaces_its_old_entry(server):
    """Same id, new socket: one entry, still selected"""
    async with connect_as_extension(uri(server)) as first:
        await hello(first, "aaaa1111", "Main")
        async with connect_as_extension(uri(server)) as second:
            await hello(second, "aaaa1111", "Main")
            await asyncio.sleep(0.3)

            assert list(server.browsers) == ["aaaa1111"]
            assert server.browsers["aaaa1111"].websocket is not None
            assert server.active_browser == "aaaa1111"
            await ping(server)
            assert (await received(second))["id"] == "probe"


@pytest.mark.asyncio
async def test_monitor_calls_reach_the_browser_that_runs_the_monitor(server):
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as other:
        await hello(main, "aaaa1111", "Main")
        await hello(other, "bbbb2222", "Lost Tribe")

        server.register_monitor("mon_1", "session-1")
        server.select_browser("Lost Tribe")
        await server.send_to_extension({"id": "list", "type": "request",
                                        "action": "requests.list_captured", "data": {"monitor_id": "mon_1"}})

        assert (await received(main))["id"] == "list"
        assert await received(other, timeout=0.3) is None


@pytest.mark.asyncio
async def test_browsers_list_tool_marks_the_active_one(server):
    async with connect_as_extension(uri(server)) as main, connect_as_extension(uri(server)) as other:
        await hello(main, "aaaa1111", "Main")
        await hello(other, "bbbb2222", "Lost Tribe")

        client = DirectMCPTestClient(server.mcp_tools)
        await client.connect()
        listing = str((await client.call_tool("browsers_list", {})).get("content"))
        assert "Connected browsers (2)" in listing
        assert "- Main (active): id aaaa1111" in listing

        answer = str((await client.call_tool("browsers_select", {"browser": "Lost Tribe"})).get("content"))
        assert "Now acting on Lost Tribe" in answer
        assert server.active_browser == "bbbb2222"


@pytest.mark.asyncio
async def test_browsers_list_waits_for_browsers_right_after_a_restart(server):
    # After /mcp restarts the server, add-ons take a few seconds to find it;
    # "No browser is connected" then sent the agent off to debug nothing
    client = DirectMCPTestClient(server.mcp_tools)
    await client.connect()

    async def arrive_late():
        await asyncio.sleep(1.0)
        ws = await connect_as_extension(uri(server))
        await hello(ws, "aaaa1111", "Main")
        return ws

    late = asyncio.create_task(arrive_late())
    listing = str((await client.call_tool("browsers_list", {})).get("content"))
    await (await late).close()
    assert "Connected browsers (1)" in listing
    assert "- Main (active): id aaaa1111" in listing, "listed before its hello, under a placeholder name"


@pytest.mark.asyncio
async def test_browsers_list_says_none_once_the_server_has_been_up_a_while(server):
    server.listening_since -= 60
    client = DirectMCPTestClient(server.mcp_tools)
    await client.connect()
    listing = str((await client.call_tool("browsers_list", {})).get("content"))
    assert listing.count("No browser is connected") == 1


@pytest.mark.asyncio
async def test_browsers_list_says_it_is_waiting_when_none_arrive_after_a_restart(server):
    client = DirectMCPTestClient(server.mcp_tools)
    await client.connect()
    listing = str((await client.call_tool("browsers_list", {})).get("content"))
    assert "Waiting for browsers to reconnect" in listing
    assert "No browser is connected" not in listing
