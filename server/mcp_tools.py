#!/usr/bin/env python3
"""
MCP Tool definitions for FoxMCP server
These tools bridge browser functions through WebSocket to the Firefox extension

Copyright (c) 2024 FoxMCP Project
Licensed under the MIT License - see LICENSE file for details
"""

import asyncio
import contextvars
import functools
import inspect
import logging
import mimetypes
import re
import uuid
import os
import subprocess
import time
import json
import base64
import tempfile
from datetime import datetime
from typing import Annotated, Dict, Any, Optional, List, TypedDict, Union

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

try:
    from .bidi import LIKELY_PORTS, BidiSession, BidiUnavailable, listening_ports
    from .win_input import AVAILABLE as WINDOW_INPUT, WindowInput, WindowInputUnavailable, WindowNotShowing, clipboard_sequence
except ImportError:
    from bidi import LIKELY_PORTS, BidiSession, BidiUnavailable, listening_ports
    from win_input import AVAILABLE as WINDOW_INPUT, WindowInput, WindowInputUnavailable, WindowNotShowing, clipboard_sequence
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Longest a single script argument may appear in the log before being cut short.
#
# Arguments carry real content - a Bugzilla comment, a chat message - so the log
# records them: knowing that element-send-message.sh ran without knowing what it
# sent is not an audit trail. The cap keeps one long comment from burying the
# rest of the log, since start.sh writes it to a plain file that only truncates
# when the server restarts.
MAX_LOGGED_ARG_LENGTH = 120

# File bytes per upload message, about 700 KB once base64-encoded. Each chunk
# waits for the extension's answer, so a large file never queues up as one
# message several hundred megabytes long.
UPLOAD_CHUNK_BYTES = 512 * 1024

# The tool running and the status line its caller gave, read by
# send_request_and_wait so every request a tool sends carries them to the page.
AGENT_CALL = contextvars.ContextVar('agent_call', default=None)

# How a tool's text result reads when the tool failed
# Key combinations that copy to or paste from the system clipboard
COPY_KEYS = re.compile(r"^(control|ctrl|meta|command)\+(c|x|insert)$", re.IGNORECASE)
PASTE_KEYS = re.compile(r"^((control|ctrl|meta|command)(\+shift)?\+v|shift\+insert)$", re.IGNORECASE)
# How long an app may take to put a copy on the clipboard after the keys
COPY_WAIT_S = 1.5
# A key held with Control, Alt or Meta, which the browser may take for itself
MODIFIED_KEY = re.compile(r"(^|\+)(control|ctrl|alt|meta|command)\+", re.IGNORECASE)
# real_input's answer for a target only the scripted path can work
SCRIPTED_ONLY = object()
# Why this call has no real input, for the refusal to say which case it is
REAL_INPUT_MISS = contextvars.ContextVar("real_input_miss", default="")
# Firefox updates a window's title and a tab's visibility shortly after activating it
WINDOW_SETTLE_DELAYS = (0.2, 0.4, 0.8)

# Where a screenshot given a bare file name is saved, rather than wherever the
# server happens to run from (a client's project folder)
SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "mcp-bridge-for-firefox", "screenshots")

FAILURE_RESULT = re.compile(r"(Failed to|Unable to|Error |Unexpected error|Window \S+ not found)")

# The overlay label is one line; a status longer than this is cut.
MAX_STATUS_LENGTH = 80

STATUS_DESCRIPTION = (
    'A few words in the first person shown on the page while you work, e.g. '
    '"Reading the board". Call tabs_release when done with the tab.')
STATUS_TYPE = Annotated[Optional[str], Field(description=STATUS_DESCRIPTION)]


def format_script_args_for_log(args_list):
    """Render predefined-script arguments as a single line for the log

    Long arguments are cut to MAX_LOGGED_ARG_LENGTH with an ellipsis and their
    full length appended, so a truncated entry still says how much was elided.
    Returns "(no arguments)" for an empty list rather than an empty string,
    which would read as missing information in the log.
    """
    if not args_list:
        return "(no arguments)"

    rendered = []
    for arg in args_list:
        if len(arg) > MAX_LOGGED_ARG_LENGTH:
            rendered.append(f"{arg[:MAX_LOGGED_ARG_LENGTH]!r}...[{len(arg)} chars]")
        else:
            rendered.append(repr(arg))
    return ", ".join(rendered)


# Colours Firefox accepts for a tab group.
TAB_GROUP_COLORS = ("blue", "cyan", "grey", "green", "orange", "pink", "purple", "red", "yellow")


def parse_id(value, name):
    """A browser ID given as an int or a numeric string, as a non-negative int

    Raises ValueError naming the argument when the value is not one. A bool is
    refused even though Python counts it as an int, and so is a float, which int()
    would otherwise truncate to some other tab's ID.
    """
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError
        parsed = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {name} '{value}'. Must be a non-negative integer.")
    if parsed < 0:
        raise ValueError(f"Invalid {name} '{value}'. Must be a non-negative integer.")
    return parsed


def parse_tab_ids(tab_ids):
    """The tab IDs a tool's tab_ids argument names, as a non-empty list of ints

    tab_ids is one ID, a list of them, or a JSON string of either; the string form
    is how MCP clients that cannot send arrays pass a list, the same convention
    content_execute_predefined uses for its arguments. Raises ValueError with a
    message fit to return to the client.
    """
    if isinstance(tab_ids, str):
        try:
            tab_ids = json.loads(tab_ids)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid tab_ids '{tab_ids}'. Must be an integer or a JSON list of integers.")

    requested_ids = tab_ids if isinstance(tab_ids, list) else [tab_ids]
    if not requested_ids:
        raise ValueError("tab_ids is empty. Provide at least one tab ID.")
    return [parse_id(tab_id, "tab ID") for tab_id in requested_ids]


def parse_group_properties(title=None, color=None, collapsed=None):
    """The tab group properties a caller gave, keyed as Firefox names them

    Arguments left as None are omitted, so Firefox keeps the group's current value
    for them. An empty title is kept: it clears the title. Raises ValueError on a
    colour Firefox does not offer.
    """
    properties = {}
    if title is not None:
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        properties["title"] = title
    if color is not None:
        if color not in TAB_GROUP_COLORS:
            raise ValueError(f"Invalid color '{color}'. Must be one of: {', '.join(TAB_GROUP_COLORS)}.")
        properties["color"] = color
    if collapsed is not None:
        if not isinstance(collapsed, bool):
            raise ValueError("collapsed must be a boolean")
        properties["collapsed"] = collapsed
    return properties


class TabInfo(TypedDict):
    """Type definition for tab information from browser extension"""
    url: str
    id: int
    title: str
    active: bool
    windowId: int
    pinned: bool
    index: int

class TabsListResponse(TypedDict):
    """Type definition for tabs.list response from browser extension"""
    tabs: List[TabInfo]
    debug: Dict[str, Any]

class FoxMCPTools:
    """MCP tools that communicate with Firefox extension via WebSocket"""

    # The tool groups a user can turn off, mapped to the method that registers each.
    #
    # Every tool description an MCP client is offered sits in that client's context
    # for the whole session, whether or not it is ever called, so a user who never
    # touches bookmarks pays for them on every request. The keys are the names
    # --disable-tools accepts; docs/configuration.md lists what each group costs.
    #
    # This is also the only route by which a tool gets registered - every tool
    # definition goes through _tool while one of these methods is running - so a
    # new tool added to any _setup_* method below can be disabled with its group,
    # or enabled on its own, without further work.
    TOOL_GROUPS = {
        'windows': '_setup_window_tools',
        'tabs': '_setup_tab_tools',
        'history': '_setup_history_tools',
        'bookmarks': '_setup_bookmark_tools',
        'navigation': '_setup_navigation_tools',
        'content': '_setup_content_tools',
        'requests': '_setup_request_monitoring_tools',
        'debug': '_setup_debug_tools',
        'browsers': '_setup_browser_tools',
        'thunderbird': '_setup_thunderbird_tools',
        'mail': '_setup_mail_tools',
    }

    def __init__(self, websocket_server, disabled_groups=None, enabled_tools=None):
        """Initialize with reference to WebSocket server

        disabled_groups names tool groups to leave unregistered, so their
        descriptions never reach a client's context - see TOOL_GROUPS for the
        names. enabled_tools names individual tools to register anyway, for when
        one tool out of a disabled group is the one that is wanted: disabling
        'tabs' while enabling 'tabs_capture_screenshot' offers the screenshot
        without the nine tab tools around it.

        Raises ValueError on a name that is not a group, or an enabled_tools name
        that is not a tool: a typo that silently registered everything - or that
        silently dropped the one tool that was wanted - would defeat the point of
        the options.
        """
        self.websocket_server = websocket_server
        self.disabled_groups = self._validate_groups(disabled_groups)
        self.enabled_tools = set(enabled_tools or ())

        # The group each tool belongs to, filled in by _tool as the definitions run.
        #
        # It covers every tool that exists, including the ones left unregistered,
        # which is what lets an --enable-tools name that matches nothing be
        # reported as the typo it is.
        self.group_by_tool = {}
        self._registering_group = None

        self.mcp = FastMCP("MCP Bridge for Firefox")
        self._setup_tools()

    @classmethod
    def _validate_groups(cls, disabled_groups):
        """Check requested group names against TOOL_GROUPS, returning them as a set

        Raises ValueError naming both the unknown groups and the valid ones. The
        message reaches a user through server.py's --disable-tools, so it carries
        the whole list rather than telling them where to look it up.
        """
        groups = set(disabled_groups or ())
        unknown = sorted(groups - set(cls.TOOL_GROUPS))
        if unknown:
            raise ValueError(
                f"Unknown tool group(s): {', '.join(unknown)}. "
                f"Valid groups: {', '.join(sorted(cls.TOOL_GROUPS))}"
            )
        return groups

    def _setup_tools(self):
        """Set up all MCP tool definitions, minus any disabled group

        Every group's definitions run even when the group is disabled, because
        defining a tool is not registering it - _tool decides that, and needs to
        see each definition to record the name. Running them all is what builds
        the catalogue that _validate_tools checks --enable-tools against.
        """
        for group, setup_method in self.TOOL_GROUPS.items():
            self._registering_group = group
            getattr(self, setup_method)()
        self._registering_group = None

        self._validate_tools()
        self._log_disabled_groups()

    def _tool(self):
        """Return the decorator that registers one tool, in place of @self.mcp.tool()

        Every tool definition below goes through here, which is what gives
        --enable-tools its per-tool grip: this is the one place that knows both
        the group being set up and the name of the tool being defined. A tool
        from a disabled group is registered only if it was named individually.

        A tool that is not registered is still recorded in group_by_tool, and the
        undecorated function is handed back - the closure stays defined, and no
        MCP client ever hears about it.
        """
        group = self._registering_group

        def register(fn):
            self.group_by_tool[fn.__name__] = group
            if group in self.disabled_groups and fn.__name__ not in self.enabled_tools:
                return fn
            return self.mcp.tool()(self._with_status(self._failures_as_errors(self._for_app(group, fn))))

        return register

    # Which app answers each group's actions. The Thunderbird add-on answers
    # only its own; groups not listed here are Firefox's, and ANY_APP_GROUPS
    # never reach an add-on in a way that differs between the two.
    APP_OF_GROUP = {'thunderbird': 'thunderbird', 'mail': 'thunderbird'}
    ANY_APP_GROUPS = {'browsers', 'debug'}

    def _for_app(self, group, fn):
        """Refuse a tool when the active browser is the wrong app for it

        Every tool list is fixed for the life of a server, so a Firefox tool
        stays offered while Thunderbird is active. Refusing here, before any
        request goes out, says why and what to do instead of letting the
        add-on answer an action it does not know.
        """
        if group in self.ANY_APP_GROUPS:
            return fn
        needed = self.APP_OF_GROUP.get(group, 'firefox')

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            server = self.websocket_server
            browser = getattr(server, 'browsers', {}).get(getattr(server, 'active_browser', None))
            if browser is not None and browser.app != needed:
                raise ToolError(f'"{browser.name}" is {browser.app.title()}, and {fn.__name__} needs '
                                f'{needed.title()}. browsers_select a {needed.title()} browser first.')
            # Thunderbird reaches mail, so it only works once paired. The add-on
            # refuses too; this says why without a round trip.
            if browser is not None and needed == 'thunderbird' and not browser.paired:
                raise ToolError(f'"{browser.name}" is not paired with this server. The add-on reads '
                                f'~/.foxmcp/pairing-token itself; its options page says why it could not pair.')
            return await fn(*args, **kwargs)
        return wrapper

    def _failures_as_errors(self, fn):
        """Report a tool's failure as an MCP error rather than as a result

        Tools say what went wrong in plain text ("Failed to type: ..."), and a
        client took that for success, so an agent moved on after a step that
        never happened. A result that starts like a failure is raised instead,
        and the client sees isError with the same text.
        """
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            if isinstance(result, str) and FAILURE_RESULT.match(result):
                raise ToolError(result)
            return result
        return wrapper

    def _with_status(self, fn):
        """Give a tool the optional `status` argument shown to the person watching

        The agent says in a few words what it is doing ("Reading the board"), and
        the line appears on the overlay of the tab it is working in. It travels
        with every request the tool sends, through AGENT_CALL, so no tool has to
        pass it on itself. A tool called without one still shows a label: the
        extension makes one up from the action ("Reading this page").

        The tab a tool works in is also recorded against the MCP session that
        called it, so the tab can be released when that session ends.
        """
        signature = inspect.signature(fn)
        # Only tools that act in a tab have a page to show the line on
        if 'status' in signature.parameters or not ('tab_id' in signature.parameters or fn.__name__ == 'tabs_create'):
            return fn

        @functools.wraps(fn)
        async def wrapper(*args, status: Optional[str] = None, **kwargs):
            call = {'tool': fn.__name__}
            if isinstance(status, str) and status.strip():
                call['status'] = status.strip()[:MAX_STATUS_LENGTH]
            note_agent_tab = getattr(self.websocket_server, 'note_agent_tab', None)
            if kwargs.get('tab_id') is not None and note_agent_tab:
                note_agent_tab(self._session_id(), kwargs['tab_id'])
            token = AGENT_CALL.set(call)
            try:
                return await fn(*args, **kwargs)
            finally:
                AGENT_CALL.reset(token)

        status_param = inspect.Parameter(
            'status', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=STATUS_TYPE)
        wrapper.__signature__ = signature.replace(
            parameters=[*signature.parameters.values(), status_param])
        wrapper.__annotations__ = {**getattr(fn, '__annotations__', {}), 'status': STATUS_TYPE}
        return wrapper

    @staticmethod
    def _session_id():
        """The calling MCP session's id, or None outside a session (the test harness)"""
        try:
            from fastmcp.server.dependencies import get_context
            return get_context().session_id
        except Exception:
            return None

    def _validate_tools(self):
        """Check enabled_tools against the tools that exist, once they have been defined

        Raises ValueError naming both the unknown tools and every valid name. The
        message reaches a user through server.py's --enable-tools, so it carries
        the whole list rather than telling them where to look it up.
        """
        unknown = sorted(self.enabled_tools - set(self.group_by_tool))
        if unknown:
            raise ValueError(
                f"Unknown tool(s): {', '.join(unknown)}. "
                f"Valid tools: {', '.join(sorted(self.group_by_tool))}"
            )

    def _log_disabled_groups(self):
        """Log each disabled group, and any of its tools that were kept anyway"""
        for group in sorted(self.disabled_groups):
            kept = sorted(
                name for name, tool_group in self.group_by_tool.items()
                if tool_group == group and name in self.enabled_tools
            )
            if kept:
                logger.info(
                    f"Tool group '{group}' disabled - registering only: {', '.join(kept)}"
                )
            else:
                logger.info(f"Tool group '{group}' disabled - not registering its tools")

    def _setup_window_tools(self):
        """Setup window management tools"""

        @self._tool()
        async def list_windows(populate: bool = True) -> str:
            """
            List all browser windows
            
            Args:
                populate: Whether to include tab information for each window
                
            Returns:
                String containing list of windows with their details
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.list",
                "data": {"populate": populate},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting windows: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                windows_data = response["data"]
                windows = windows_data.get("windows", [])
                if not windows:
                    return "No windows found"

                result = f"Browser windows ({len(windows)} found):\n"
                for window in windows:
                    state_info = f"state: {window.get('state', 'unknown')}"
                    focused_info = " (focused)" if window.get("focused") else ""
                    size_info = f"{window.get('width', '?')}x{window.get('height', '?')}"
                    tabs_count = len(window.get('tabs', [])) if populate else '?'
                    result += f"- ID {window.get('id')}: {window.get('type', 'normal')} window, {state_info}, {size_info}, {tabs_count} tabs{focused_info}\n"
                return result

            return "Unable to retrieve windows"

        @self._tool()
        async def get_window(window_id: int, populate: bool = True) -> str:
            """
            Get information about a specific window
            
            Args:
                window_id: The ID of the window to retrieve
                populate: Whether to include tab information
                
            Returns:
                String containing window details
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.get",
                "data": {"windowId": window_id, "populate": populate},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting window {window_id}: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                window_data = response["data"].get("window", {})
                if not window_data:
                    return f"Window {window_id} not found"

                state_info = f"state: {window_data.get('state', 'unknown')}"
                focused_info = " (focused)" if window_data.get("focused") else ""
                size_info = f"{window_data.get('width', '?')}x{window_data.get('height', '?')}"
                tabs_count = len(window_data.get('tabs', [])) if populate else '?'
                return f"Window {window_data.get('id')}: {window_data.get('type', 'normal')} window, {state_info}, {size_info}, {tabs_count} tabs{focused_info}"

            return f"Unable to retrieve window {window_id}"

        # There is no get_last_focused_window tool because it could not have differed
        # from this one.
        #
        # ext-windows.js reads `context.currentWindow || windowTracker.topWindow` for
        # getCurrent and plain `windowTracker.topWindow` for getLastFocused, and
        # ExtensionParent.sys.mjs returns undefined for `currentWindow` whenever the
        # context's viewType is "background" - which is the only context this
        # extension has. So both calls evaluate the same expression. A separate tool
        # shipped until 1.2.0 and always returned the same window as this one.
        @self._tool()
        async def get_current_window(populate: bool = True) -> str:
            """Get the current active window, which is also the last focused window

            Args:
                populate: Whether to include tab information
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.get_current",
                "data": {"populate": populate},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting current window: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                window_data = response["data"].get("window", {})
                if not window_data:
                    return "No current window found"

                state_info = f"state: {window_data.get('state', 'unknown')}"
                size_info = f"{window_data.get('width', '?')}x{window_data.get('height', '?')}"
                tabs_count = len(window_data.get('tabs', [])) if populate else '?'
                return f"Current window (ID {window_data.get('id')}): {window_data.get('type', 'normal')} window, {state_info}, {size_info}, {tabs_count} tabs"

            return "Unable to retrieve current window"

        @self._tool()
        async def create_window(
            url: Optional[str] = None,
            window_type: str = "normal",
            state: str = "normal", 
            focused: bool = True,
            width: Optional[int] = None,
            height: Optional[int] = None,
            top: Optional[int] = None,
            left: Optional[int] = None,
            incognito: bool = False
        ) -> str:
            """
            Create a new browser window
            
            Args:
                url: URL to load in the new window
                window_type: Type of window ("normal", "popup", "panel", "detached_panel")
                state: Window state ("normal", "minimized", "maximized", "fullscreen")
                focused: Whether the new window takes focus. Firefox always focuses a
                    new window, so with False the extension hands focus back to the
                    window that had it; the result says "(focused)" if the new window
                    kept it anyway.
                width: Window width in pixels
                height: Window height in pixels  
                top: Window top position in pixels
                left: Window left position in pixels
                incognito: Whether to create an incognito window
                
            Returns:
                String containing created window details
            """
            data = {'type': window_type, 'state': state, 'focused': focused, 'incognito': incognito}
            if url: data['url'] = url
            if width: data['width'] = width
            if height: data['height'] = height
            if top is not None: data['top'] = top
            if left is not None: data['left'] = left
            
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.create",
                "data": data,
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error creating window: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                window_data = response["data"].get("window", {})
                if window_data:
                    window_id = window_data.get('id')
                    window_url = url or "about:blank"
                    size_info = f"{window_data.get('width', '?')}x{window_data.get('height', '?')}"
                    focused_info = " (focused)" if window_data.get("focused") else ""
                    return f"Created {window_type} window (ID {window_id}): {window_url}, {size_info}{focused_info}"

            return "Window created but unable to retrieve details"

        @self._tool() 
        async def close_window(window_id: int) -> str:
            """
            Close a browser window
            
            Args:
                window_id: The ID of the window to close
                
            Returns:
                String indicating success/failure
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.close",
                "data": {"windowId": window_id},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error closing window {window_id}: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                if response["data"].get("success"):
                    return f"Window {window_id} closed successfully"
                else:
                    return f"Failed to close window {window_id}"

            return f"Unable to close window {window_id}"

        @self._tool()
        async def focus_window(window_id: int) -> str:
            """
            Bring a window to front and focus it
            
            Args:
                window_id: The ID of the window to focus
                
            Returns:
                String indicating success/failure
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.focus",
                "data": {"windowId": window_id},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error focusing window {window_id}: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                if response["data"].get("success"):
                    return f"Window {window_id} focused successfully"
                else:
                    return f"Failed to focus window {window_id}"

            return f"Unable to focus window {window_id}"

        @self._tool()
        async def update_window(
            window_id: int,
            state: Optional[str] = None,
            focused: Optional[bool] = None,
            width: Optional[int] = None,
            height: Optional[int] = None,
            top: Optional[int] = None,
            left: Optional[int] = None
        ) -> str:
            """
            Update window properties
            
            Args:
                window_id: The ID of the window to update
                state: New window state ("normal", "minimized", "maximized", "fullscreen")
                focused: True brings the window to the front. False is refused:
                    Firefox cannot take focus away from a window, so call
                    focus_window on the window that should have it instead.
                width: New window width in pixels
                height: New window height in pixels
                top: New window top position in pixels
                left: New window left position in pixels
                
            Returns:
                String containing updated window details
            """
            # Firefox's windows.update ignores focused: false without an error.
            if focused is False:
                return ("Error: focused=False is not supported; Firefox cannot take focus "
                        "away from a window. Call focus_window on the window that should "
                        "have focus instead.")

            data = {'windowId': window_id}
            if state: data['state'] = state
            if focused is not None: data['focused'] = focused
            if width: data['width'] = width
            if height: data['height'] = height
            if top is not None: data['top'] = top
            if left is not None: data['left'] = left
            
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "windows.update",
                "data": data,
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error updating window {window_id}: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                window_data = response["data"].get("window", {})
                if window_data:
                    state_info = f"state: {window_data.get('state', 'unknown')}"
                    size_info = f"{window_data.get('width', '?')}x{window_data.get('height', '?')}"
                    return f"Updated window {window_id}: {state_info}, {size_info}"
                else:
                    return f"Window {window_id} updated successfully"

            return f"Unable to update window {window_id}"

    def _setup_tab_tools(self):
        """Setup tab management tools"""

        # Tab List Tool
        @self._tool()
        async def tabs_list(window_id: Optional[Union[int, str]] = None) -> str:
            """
            List open browser tabs, across every window or within one window

            Each line ends with "[window {id}, index {n}]", and carries "(active)" for the
            active tab of its window and "(pinned)" for a pinned one. The window, the index
            and the pinned mark are what tabs_move needs: the window to move a tab out of or
            into, the position it currently holds, and whether a leading run of pinned tabs
            blocks the low indexes.

            Args:
                window_id: Restrict the listing to this window (optional, accepts int or
                    string). Omit it to list tabs in every window.

            Returns:
                Formatted string with tab information:
                "Open tabs ({count} found):
                - ID {tab_id}: {title} - {url}{status_indicators} [window {window_id}, index {index}]"

                The index is the tab's position in its window, counting from 0 — pass it to
                tabs_move to reorder.

                Not exposed to clients — see the description above.
            """
            # Convert window_id to int if it's a string (for MCP client compatibility)
            if window_id is not None and isinstance(window_id, str):
                try:
                    window_id = int(window_id)
                except (ValueError, TypeError):
                    return f"Error: Invalid window_id '{window_id}'. Must be a valid integer."

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.list",
                "data": {
                    **({"windowId": window_id} if window_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting tabs: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                tabs_data: TabsListResponse = response["data"]
                tabs: List[TabInfo] = tabs_data.get("tabs", [])
                if not tabs:
                    # More informative message for debugging
                    return f"No tabs found. Extension response: {response.get('data', {})}"

                # The location goes at the end of the line, not next to the tab ID.
                #
                # Callers pick tab IDs out of this listing with an `ID (\d+):` pattern, so
                # nothing may come between the ID and its colon. One test in the suite feeds
                # that match into pytest.skip, which would quietly stop testing rather than
                # fail if the pattern broke.
                scope = f" in window {window_id}" if window_id else ""
                result = f"Open tabs{scope} ({len(tabs)} found):\n"
                for tab in tabs:
                    active = " (active)" if tab.get("active") else ""
                    pinned = " (pinned)" if tab.get("pinned") else ""
                    location = f" [window {tab.get('windowId')}, index {tab.get('index')}]"
                    result += f"- ID {tab.get('id')}: {tab.get('title', 'No title')} - {tab.get('url', 'No URL')}{active}{pinned}{location}\n"
                return result

            return "Unable to retrieve tabs"

        # Tab Create Tool
        @self._tool()
        async def tabs_create(
            url: str,
            active: bool = True,
            pinned: bool = False,
            window_id: Optional[Union[int, str]] = None
        ) -> str:
            """Create a new browser tab

            Args:
                url: URL to open in the new tab
                active: Whether the tab should be active (default: True)
                pinned: Whether the tab should be pinned (default: False)
                window_id: Window ID to create tab in (optional, accepts int or string)
            """
            # Convert window_id to int if it's a string (for MCP client compatibility)
            if window_id is not None and isinstance(window_id, str):
                try:
                    window_id = int(window_id)
                except (ValueError, TypeError):
                    return f"Error: Invalid window_id '{window_id}'. Must be a valid integer."
            
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.create",
                "data": {
                    "url": url,
                    "active": active,
                    "pinned": pinned,
                    **({"windowId": window_id} if window_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error creating tab: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                tab = response["data"].get("tab", {})
                return f"Created tab: ID {tab.get('id')} - {tab.get('title', 'Loading...')} - {tab.get('url', url)}"

            return "Unable to create tab"

        # Tab Close Tool
        @self._tool()
        async def tabs_close(tab_id: int) -> str:
            """Close a browser tab

            Args:
                tab_id: ID of the tab to close
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.close",
                "data": {
                    "tabId": tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error closing tab: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully closed tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to close tab: {error_msg}"

            return f"Unable to close tab {tab_id}"

        # Tab Switch Tool
        @self._tool()
        async def tabs_switch(tab_id: int) -> str:
            """Switch to a specific browser tab

            Args:
                tab_id: ID of the tab to switch to
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.switch",
                "data": {
                    "tabId": tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error switching to tab: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully switched to tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to switch to tab: {error_msg}"

            return f"Unable to switch to tab {tab_id}"

        # Tab Release Tool
        @self._tool()
        async def tabs_release(tab_id: int) -> str:
            """Say you are done working in a tab, so the "AI agent is working"
            overlay, cursor and tab marker leave it

            Call this when a job in a tab is finished. Until then the overlay stays
            up so the person can see the tab is in use; it also clears by itself
            when your session ends, or after 5 minutes with no action in the tab.

            Args:
                tab_id: ID of the tab you are done with
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.release",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error releasing tab: {response['error']}"
            if response.get("type") == "response":
                forget_agent_tab = getattr(self.websocket_server, 'forget_agent_tab', None)
                if forget_agent_tab:
                    forget_agent_tab(tab_id)
                return f"Released tab {tab_id}"
            error_msg = response.get("data", {}).get("message", "Unknown error")
            return f"Failed to release tab: {error_msg}"

        @self._tool()
        async def tabs_group(
            tab_ids: Union[int, str, List[Union[int, str]]],
            group_id: Optional[Union[int, str]] = None,
            title: Optional[str] = None,
            color: Optional[str] = None
        ) -> str:
            """Group existing tabs, optionally setting the group's title and colour

            Never activates a tab or focuses a window. Requires Firefox 139+. Omit
            group_id to create a group in the first tab's window, which moves tabs from
            other windows into it; pass a group_id from tabs_groups_query to add tabs
            to that group instead. Firefox unpins a pinned tab before grouping it, and
            refuses to mix private and non-private tabs.

            Grouping and styling are separate steps in Firefox. If the tabs were
            grouped but the title or colour could not be set, the result still names
            the group, and tabs_groups_update can retry the styling.

            Args:
                tab_ids: Tab ID, or several as a list (`[12, 15]`) or a JSON string
                    (`"[12, 15]"`).
                group_id: Group to add the tabs to; omit it to create a new group.
                title: Group title; an empty string clears it. Omit to leave as is.
                color: blue, cyan, grey, green, orange, pink, purple, red or yellow.
                    Omit to leave as is.
            """
            try:
                data = {"tabIds": parse_tab_ids(tab_ids)}
                if group_id is not None:
                    data["groupId"] = parse_id(group_id, "group_id")
                data.update(parse_group_properties(title=title, color=color))
            except ValueError as error:
                return f"Error: {error}"

            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()), "type": "request", "action": "tabs.group",
                "data": data, "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return f"Error grouping tabs: {response['error']}"
            if response.get("type") == "error":
                return f"Failed to group tabs: {response.get('data', {}).get('message', 'Unknown error')}"
            reply = response.get("data", {})
            if response.get("type") == "response" and "groupId" in reply:
                if reply.get("styleError"):
                    return (f"Tabs grouped into group {reply['groupId']}, but its title/colour "
                            f"could not be set: {reply['styleError']}. "
                            f"Retry the styling with tabs_groups_update.")
                return f"Tabs grouped into group {reply['groupId']}"
            return "Unable to group tabs"

        @self._tool()
        async def tabs_ungroup(
            tab_ids: Union[int, str, List[Union[int, str]]]
        ) -> str:
            """Remove tabs from their tab group

            Never activates a tab or focuses a window. Requires Firefox 138+. Firefox
            deletes a group once its last tab leaves it.

            Args:
                tab_ids: Tab ID, or several as a list (`[12, 15]`) or a JSON string
                    (`"[12, 15]"`).
            """
            try:
                data = {"tabIds": parse_tab_ids(tab_ids)}
            except ValueError as error:
                return f"Error: {error}"

            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()), "type": "request", "action": "tabs.ungroup",
                "data": data, "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return f"Error ungrouping tabs: {response['error']}"
            if response.get("type") == "error":
                return f"Failed to ungroup tabs: {response.get('data', {}).get('message', 'Unknown error')}"
            if response.get("type") == "response" and response.get("data", {}).get("success"):
                return f"Ungrouped {len(data['tabIds'])} tab(s)"
            return "Unable to ungroup tabs"

        @self._tool()
        async def tabs_groups_update(
            group_id: Union[int, str],
            title: Optional[str] = None,
            color: Optional[str] = None,
            collapsed: Optional[bool] = None
        ) -> str:
            """Change a tab group's title, colour or collapsed state

            Leaves the group's tabs where they are. Requires Firefox 139+. Give at
            least one of title, color or collapsed.

            Args:
                group_id: The group to change, from tabs_group or tabs_groups_query.
                title: New title; an empty string clears it.
                color: blue, cyan, grey, green, orange, pink, purple, red or yellow.
                collapsed: Whether the group should be collapsed.
            """
            try:
                data = {"groupId": parse_id(group_id, "group_id")}
                properties = parse_group_properties(title=title, color=color, collapsed=collapsed)
                if not properties:
                    raise ValueError("Provide at least one of title, color or collapsed")
                data.update(properties)
            except ValueError as error:
                return f"Error: {error}"

            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()), "type": "request", "action": "tabGroups.update",
                "data": data, "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return f"Error updating group: {response['error']}"
            if response.get("type") == "error":
                return f"Failed to update group: {response.get('data', {}).get('message', 'Unknown error')}"
            if response.get("type") == "response" and "group" in response.get("data", {}):
                group = response["data"]["group"]
                return f"Updated group {group.get('id', data['groupId'])}"
            return "Unable to update group"

        @self._tool()
        async def tabs_groups_query(
            window_id: Optional[Union[int, str]] = None,
            title: Optional[str] = None,
            color: Optional[str] = None,
            collapsed: Optional[bool] = None
        ) -> str:
            """List tab groups with their IDs, in every window or filtered

            Requires Firefox 139+. tabs_group reports a group's ID only when it creates
            the group, so this is how to find the ID of any other group.

            Args:
                window_id: Only groups in this window (optional, accepts int or string).
                title: Only groups with exactly this title.
                color: Only groups of this colour.
                collapsed: Only collapsed (True) or expanded (False) groups.
            """
            try:
                data = {}
                if window_id is not None:
                    data["windowId"] = parse_id(window_id, "window_id")
                data.update(parse_group_properties(title=title, color=color, collapsed=collapsed))
            except ValueError as error:
                return f"Error: {error}"

            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()), "type": "request", "action": "tabGroups.query",
                "data": data, "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return f"Error listing groups: {response['error']}"
            if response.get("type") == "error":
                return f"Failed to list groups: {response.get('data', {}).get('message', 'Unknown error')}"
            if response.get("type") == "response" and "groups" in response.get("data", {}):
                groups = response["data"]["groups"]
                if not groups:
                    return "No tab groups found"
                result = f"Tab groups ({len(groups)} found):\n"
                for group in groups:
                    collapsed_info = " (collapsed)" if group.get("collapsed") else ""
                    result += (
                        f"- ID {group.get('id')}: {group.get('title') or 'Untitled'} - "
                        f"{group.get('color')}{collapsed_info} [window {group.get('windowId')}]\n"
                    )
                return result
            return "Unable to list groups"

        # Tab Move Tool
        @self._tool()
        async def tabs_move(
            tab_ids: Union[int, str, List[Union[int, str]]],
            window_id: Optional[Union[int, str]] = None,
            index: int = -1
        ) -> str:
            """Move tabs to a new position, optionally into another window

            Reorders tabs within their window, or gathers them into a different window —
            to collect every tab for one site into a window of its own, create the window
            with create_window and pass its ID here.

            Succeeding partially is normal, so check the result: it reads
            "Moved {n} of {m}", and a tab missing from the list did not move. Firefox
            declines some moves without raising an error, and the common one is that
            **an unpinned tab cannot be placed in front of a pinned tab** — with 3 pinned
            tabs, the lowest index an unpinned tab can reach is 3, and asking for 0, 1 or 2
            moves nothing. Use tabs_list to see which tabs are pinned and what index each
            tab currently holds. A pinned tab can be moved to index 0 freely.

            Moving between windows requires both to be normal windows, not popups, and
            either both private or both non-private.

            Args:
                tab_ids: Tab ID, or several as a list (`[12, 15]`) or a JSON string
                    (`"[12, 15]"`). Listed tabs keep their given order at the destination.
                window_id: Window to move the tabs into (optional, accepts int or string).
                    Omit it to reorder within each tab's current window.
                index: Position to move to, counting from 0; -1 means the end (default: -1).
                    An index inside a leading run of pinned tabs is refused for an unpinned
                    tab — the move reports "No tabs moved" rather than failing.

            Returns:
                "Moved {n} of {m} tab(s):" and one line per moved tab with its new window
                and index, or "No tabs moved." when Firefox declined every one. Note that
                FastMCP does not expose this section to clients — anything a caller must
                know belongs in the description above or on the arguments.
            """
            try:
                normalized_ids = parse_tab_ids(tab_ids)
            except ValueError as error:
                return f"Error: {error}"

            # Convert window_id to int if it's a string (for MCP client compatibility)
            if window_id is not None and isinstance(window_id, str):
                try:
                    window_id = int(window_id)
                except (ValueError, TypeError):
                    return f"Error: Invalid window_id '{window_id}'. Must be a valid integer."

            if index < -1:
                return f"Error: Invalid index {index}. Must be -1 (end) or 0 or greater."

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.move",
                "data": {
                    "tabIds": normalized_ids,
                    "index": index,
                    **({"windowId": window_id} if window_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error moving tabs: {response['error']}"

            if response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to move tabs: {error_msg}"

            if response.get("type") == "response" and "data" in response:
                moved_tabs: List[TabInfo] = response["data"].get("tabs", [])
                if not moved_tabs:
                    return (
                        f"No tabs moved. Firefox refused the move of {len(normalized_ids)} tab(s) — "
                        "check that the target index is not in front of a pinned tab."
                    )

                result = f"Moved {len(moved_tabs)} of {len(normalized_ids)} tab(s):\n"
                for tab in moved_tabs:
                    result += f"- ID {tab.get('id')} -> window {tab.get('windowId')}, index {tab.get('index')}: {tab.get('title', 'No title')}\n"
                return result

            return "Unable to move tabs"

        # Tab Screenshot Tool
        @self._tool()
        async def tabs_capture_screenshot(
            filename: Optional[str] = None,
            window_id: Optional[int] = None,
            format: str = "png",
            quality: int = 90,
            tab_id: Optional[int] = None
        ) -> Union[str, Image]:
            """Capture a screenshot of a tab

            Without a filename the image comes back to you to look at. With one it
            is saved to disk and you get the full path; a bare name is saved in the
            system's temporary folder, not in the current directory.

            With no tab_id or window_id it captures the tab you last acted on, not
            whichever window has focus (that may be another app the person is in).

            Args:
                filename: File to save the screenshot to (optional)
                window_id: Capture the tab showing in this window (optional)
                tab_id: Capture this tab (optional; defaults to the last tab you used)
                format: Image format ('png' or 'jpeg', default: 'png'); jpeg is much
                    smaller to look at
                quality: Image quality for JPEG format (1-100, default: 90)
            """
            last_agent_tab = getattr(self.websocket_server, 'last_agent_tab', None)
            default_tab = None
            if tab_id is None and not window_id and last_agent_tab:
                default_tab = last_agent_tab()
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.captureVisibleTab",
                "data": {
                    **({"windowId": window_id} if window_id else {}),
                    **({"tabId": tab_id} if tab_id is not None else {}),
                    **({"tabId": default_tab, "tabIdOptional": True} if default_tab is not None else {}),
                    "format": format,
                    "quality": quality
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error capturing screenshot: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                data_url = response["data"].get("dataUrl", "")
                captured_format = response["data"].get("format", format)
                captured_quality = response["data"].get("quality", quality)
                captured_window_id = response["data"].get("windowId") or "current"
                captured_tab = response["data"].get("tabId")
                shown = f"tab {captured_tab}, window {captured_window_id}" if captured_tab else f"window {captured_window_id}"

                return self._screenshot_result(
                    data_url, captured_format, filename, f"{shown}, {captured_format}, quality: {captured_quality}")

            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to capture screenshot: {error_msg}"

            return "Unable to capture screenshot"

    @staticmethod
    def _screenshot_result(data_url: str, image_format: str, filename: Optional[str], details: str):
        """The image itself, or the path it was saved to when a filename is given"""
        if not data_url:
            return "No screenshot data received"

        data_prefix = f"data:image/{image_format};base64,"
        if not data_url.startswith(data_prefix):
            return f"Screenshot captured but unexpected format: {data_url[:100]}..."
        image_data = base64.b64decode(data_url[len(data_prefix):])

        if not filename:
            # The image itself, which a client shows to the model; a data URL
            # in text was cut short and could not be looked at
            return Image(data=image_data, format=image_format)

        try:
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                filename = f"{filename}.{image_format}"
            if not os.path.isabs(filename):
                os.makedirs(SCREENSHOT_DIR, exist_ok=True)
                filename = os.path.join(SCREENSHOT_DIR, filename)
            with open(filename, 'wb') as f:
                f.write(image_data)
            return f"Screenshot saved to '{filename}' ({details}, size: {len(image_data)} bytes)"
        except Exception as e:
            return f"Error saving screenshot to file '{filename}': {str(e)}"

    def _setup_thunderbird_tools(self):
        """Setup tools only the Thunderbird add-on answers"""

        @self._tool()
        async def thunderbird_update_addon(path: str) -> str:
            """Update a Thunderbird add-on from a local .xpi build

            Only an add-on that is already installed and unsigned (a local build,
            such as MCP Bridge for Thunderbird itself) can be updated; nothing is
            added, and add-ons from addons.thunderbird.net are left alone. Needs
            "Let the agent update add-ons" in the Thunderbird add-on's options.
            Updating MCP Bridge for Thunderbird restarts it: it reconnects within
            about 30 seconds.

            Args:
                path: Absolute path of the .xpi file on this computer
            """
            path = os.path.abspath(os.path.expanduser(path))
            if not path.lower().endswith(".xpi") or not os.path.isfile(path):
                raise ToolError(f"{path} is not an .xpi file")
            data = await self._thunderbird_request(
                {"action": "thunderbird.updateAddon", "data": {"path": path}}, "update the add-on")
            done = f'Updated {data.get("name")} ({data.get("id")}) from {data.get("from")} to {data.get("to")}'
            return done + (". It is restarting and reconnects shortly." if data.get("restarting") else "")

        @self._tool()
        async def thunderbird_capture_window(
            filename: Optional[str] = None,
            scale: float = 1.0
        ) -> Union[str, Image]:
            """Capture Thunderbird's main window, mail view included

            Unlike tabs_capture_screenshot this shows the whole window: folder
            pane, message list and whichever tab is open, add-on tabs too.
            Without a filename the image comes back to you to look at; with one
            it is saved and you get the path.

            Args:
                filename: File to save the PNG to (optional)
                scale: 0.25 to 1; below 1 makes the image smaller (default: 1)
            """
            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "thunderbird.captureWindow",
                "data": {"scale": min(max(scale, 0.25), 1.0)},
                "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return f"Error capturing Thunderbird: {response['error']}"
            if response.get("type") == "error":
                return f"Failed to capture Thunderbird: {response.get('data', {}).get('message', 'Unknown error')}"
            data = response.get("data", {})
            return self._screenshot_result(
                data.get("dataUrl", ""), "png", filename, f"Thunderbird window, {data.get('width')}x{data.get('height')}")

        @self._tool()
        async def thunderbird_snapshot(max_elements: int = 200) -> str:
            """List the buttons, folders, message rows and fields shown in
            Thunderbird's main window, each with a ref that thunderbird_click takes

            Covers the toolbar, the folder pane, the message list and the open
            message. Add-on tabs (such as Smart Inbox) are not reachable this way;
            use thunderbird_capture_window to see them.

            Args:
                max_elements: Stop listing after this many (default: 200)
            """
            data = await self._thunderbird_request(
                {"action": "thunderbird.snapshot", "data": {"maxElements": min(max(max_elements, 1), 1000)}},
                "snapshot Thunderbird")
            elements = data.get("elements", [])
            lines = [f'{e["ref"]} {e["role"]} "{e["label"]}" at {e["x"]},{e["y"]}' for e in elements]
            more = f" (first {len(elements)} of {data.get('total')})" if data.get("total", 0) > len(elements) else ""
            skipped = (f"\n{data['skipped']} elements could not be measured: {data.get('skipReason')}"
                       if data.get("skipped") else "")
            return f"Thunderbird elements{more}:\n" + "\n".join(lines) + skipped

        @self._tool()
        async def thunderbird_click(
            ref: Optional[str] = None,
            text: Optional[str] = None,
            x: Optional[float] = None,
            y: Optional[float] = None,
            screenshot_scale: float = 1.0,
            hover: bool = False,
            double_click: bool = False,
            right_click: bool = False,
            status: Optional[str] = None
        ) -> str:
            """Click a button, folder or message in Thunderbird, moving a visible
            cursor there first so the person can follow along

            Needs "Let the agent click" turned on in the Thunderbird add-on's
            options. Double-click a message row to open it in its own tab.

            Add-on tabs (such as Smart Inbox) do not show in thunderbird_snapshot:
            click those by position instead, with x and y read off a
            thunderbird_capture_window screenshot and the scale it was taken at.
            hover=True only moves the mouse there, to show hover buttons.

            Args:
                ref: Element ref from thunderbird_snapshot, such as "r12"
                text: Or the element's label, as shown: "Get Messages", "Inbox"
                x: Or a position: pixels from the screenshot's left edge
                y: Pixels from the screenshot's top edge
                screenshot_scale: The scale the screenshot was taken at (default: 1)
                hover: Move the mouse to x, y without clicking (default: False)
                double_click: Double-click instead (default: False)
                right_click: Right-click, to open a context menu (default: False)
                status: A few words on what you are doing, shown next to the cursor
            """
            at_point = x is not None or y is not None
            if at_point and (x is None or y is None):
                raise ToolError("Give both x and y")
            if not ref and not text and not at_point:
                raise ToolError("Give a ref from thunderbird_snapshot, the element's text, or x and y")
            if at_point and screenshot_scale <= 0:
                raise ToolError("screenshot_scale must be above 0")
            target = {"ref": ref, "text": text, "doubleClick": double_click, "rightClick": right_click,
                      "status": (status or "").strip()[:MAX_STATUS_LENGTH]}
            if at_point:
                target.update({"x": x / screenshot_scale, "y": y / screenshot_scale, "hover": hover})
            data = await self._thunderbird_request({"action": "thunderbird.click", "data": target},
                                                   "click in Thunderbird")
            done = ("Moved the mouse" if at_point and hover else "Right-clicked" if right_click
                    else "Double-clicked" if double_click else "Clicked")
            if at_point:
                over = data.get("label") or data.get("role") or "the window"
                return f"{done} at {round(x)},{round(y)} (over {over})"
            return f'{done} {data.get("role")} "{data.get("label")}"'

        @self._tool()
        async def thunderbird_type(
            value: str,
            ref: Optional[str] = None,
            text: Optional[str] = None,
            clear: bool = True,
            submit: bool = False,
            status: Optional[str] = None
        ) -> str:
            """Type into a Thunderbird field, such as the search bar or the
            Quick Filter, with the visible cursor going there first

            Needs "Let the agent click" in the Thunderbird add-on's options.

            Args:
                value: The text to type
                ref: Field ref from thunderbird_snapshot, such as "r12"
                text: Or the field's label or placeholder: "Search"
                clear: Replace what the field holds (default: True)
                submit: Press Enter afterwards (default: False)
                status: A few words on what you are doing, shown next to the cursor
            """
            if not ref and not text:
                raise ToolError("Give a ref from thunderbird_snapshot or the field's label")
            data = await self._thunderbird_request({"action": "thunderbird.type", "data": {
                "ref": ref, "text": text, "value": value, "clear": clear, "submit": submit,
                "status": (status or "").strip()[:MAX_STATUS_LENGTH]}}, "type in Thunderbird")
            if submit:
                return f'Typed "{data.get("value")}" into "{data.get("label")}" and pressed Enter'
            return f'Typed into "{data.get("label")}"; it now holds "{data.get("value")}"'

        @self._tool()
        async def thunderbird_console(
            since_minutes: int = 10,
            contains: Optional[str] = None,
            errors_only: bool = False,
            limit: int = 50
        ) -> str:
            """Read Thunderbird's error console: errors and warnings from
            Thunderbird and its add-ons, and console calls from Thunderbird's
            own code. Add-on pages run in another process, so their
            console.log calls do not show here; their uncaught errors do.

            Use it when an add-on misbehaves: contains="moz-extension" or the
            add-on's file name narrows it down.

            Args:
                since_minutes: How far back to look (default: 10)
                contains: Only messages whose text or source contains this
                errors_only: Leave warnings and logs out (default: False)
                limit: Newest messages to show, up to 500 (default: 50)
            """
            data = await self._thunderbird_request({"action": "thunderbird.console", "data": {
                "sinceMs": max(since_minutes, 1) * 60000, "contains": contains or "",
                "errorsOnly": errors_only, "limit": min(max(limit, 1), 500)}}, "read Thunderbird's console")
            messages = data.get("messages", [])
            if not messages:
                return f"No console messages in the last {since_minutes} minutes"
            # The add-on sends UTC; show the clock time the user sees
            lines = [f"{datetime.fromisoformat(m['time'].replace('Z', '+00:00')).astimezone():%H:%M:%S} {m['level']}: {m['text']}" + (f"  ({m['source']})" if m.get("source") else "")
                     for m in messages]
            more = f" (newest {len(messages)} of {data.get('total')})" if data.get("total", 0) > len(messages) else ""
            return f"Console, last {since_minutes} minutes{more}:\n" + "\n".join(lines)

        # Delete and Backspace move mail to the trash in the message list
        THUNDERBIRD_KEYS = ("Enter", "Escape", "Tab", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                            "PageUp", "PageDown", "Home", "End")

        @self._tool()
        async def thunderbird_press_key(key: str, shift: bool = False, status: Optional[str] = None) -> str:
            """Press a key on whatever has focus in Thunderbird: move through the
            message list with ArrowDown, open with Enter, close with Escape

            Allowed: Enter, Escape, Tab, Space, ArrowUp, ArrowDown, ArrowLeft,
            ArrowRight, PageUp, PageDown, Home, End. Delete and Backspace are not,
            since they move mail to the trash. Needs "Let the agent click".

            Args:
                key: One of the keys above
                shift: Hold Shift, as Shift+Tab or extending a selection (default: False)
                status: A few words on what you are doing, shown next to the cursor
            """
            if key not in THUNDERBIRD_KEYS:
                raise ToolError(f"{key} is not allowed. Use one of: {', '.join(THUNDERBIRD_KEYS)}")
            data = await self._thunderbird_request({"action": "thunderbird.pressKey", "data": {
                "key": " " if key == "Space" else key, "shift": shift,
                "status": (status or "").strip()[:MAX_STATUS_LENGTH]}}, "press a key in Thunderbird")
            return f'Pressed {data.get("key")} on {data.get("on")}'

    async def _thunderbird_request(self, request: dict, doing: str = "read mail") -> dict:
        """Send a request to the Thunderbird add-on; its data, or ToolError with its reason"""
        response = await self.websocket_server.send_request_and_wait(
            {**request, "id": str(uuid.uuid4()), "type": "request", "timestamp": datetime.now().isoformat()})
        if "error" in response:
            raise ToolError(f"Error trying to {doing}: {response['error']}")
        if response.get("type") == "error":
            raise ToolError(f"Failed to {doing}: {response.get('data', {}).get('message', 'Unknown error')}")
        return response.get("data", {})

    @staticmethod
    def _message_lines(messages) -> List[str]:
        lines = []
        for m in messages:
            marks = "".join(mark for mark, on in (("unread ", not m.get("read")), ("flagged ", m.get("flagged"))) if on)
            lines.append(f"- [{m.get('id')}] {m.get('date', '')[:16]} {marks}{m.get('author')}: "
                         f"{m.get('subject') or '(no subject)'}")
        return lines

    def _setup_mail_tools(self):
        """Read-only mail tools, answered by the Thunderbird add-on"""

        @self._tool()
        async def mail_list_accounts() -> str:
            """List Thunderbird's mail accounts with their ids and addresses"""
            data = await self._thunderbird_request({"action": "mail.listAccounts", "data": {}})
            lines = [f"- {a['name']} ({a['type']}): id {a['id']}, {', '.join(a.get('identities') or []) or 'no identity'}"
                     for a in data.get("accounts", [])]
            return f"Accounts ({len(lines)}):\n" + "\n".join(lines)

        @self._tool()
        async def mail_list_folders(account_id: Optional[str] = None) -> str:
            """List mail folders with their ids and unread/total counts

            Args:
                account_id: Only this account's folders (from mail_list_accounts)
            """
            data = await self._thunderbird_request(
                {"action": "mail.listFolders", "data": {"accountId": account_id} if account_id else {}})
            lines = []
            for f in data.get("folders", []):
                use = f" [{', '.join(f['specialUse'])}]" if f.get("specialUse") else ""
                lines.append(f"- {f['accountId']} {f['path']}{use}: id {f['id']}, "
                             f"{f.get('unread', '?')} unread of {f.get('total', '?')}")
            return f"Folders ({len(lines)}):\n" + "\n".join(lines)

        @self._tool()
        async def mail_list_messages(
            folder_id: str,
            since_days: int = 30,
            unread_only: bool = False,
            limit: int = 25
        ) -> str:
            """List a folder's messages, newest first

            Each line starts with the message id that mail_get_message takes.

            Args:
                folder_id: Folder id from mail_list_folders
                since_days: Only messages from the last N days (default: 30)
                unread_only: Only unread messages (default: false)
                limit: How many to show, up to 200 (default: 25)
            """
            data = await self._thunderbird_request({"action": "mail.listMessages", "data": {
                "folderId": folder_id, "sinceDays": max(since_days, 1), "unreadOnly": unread_only,
                "limit": min(max(limit, 1), 200)}})
            messages = data.get("messages", [])
            return (f"{len(messages)} of {data.get('total', len(messages))} messages from the last {since_days} days:\n"
                    + "\n".join(self._message_lines(messages)))

        @self._tool()
        async def mail_search_messages(
            text: Optional[str] = None,
            author: Optional[str] = None,
            subject: Optional[str] = None,
            folder_id: Optional[str] = None,
            since_days: int = 90,
            unread_only: bool = False,
            limit: int = 25
        ) -> str:
            """Search mail across every account, newest first

            Give at least one of text, author or subject. All given ones must match.

            Args:
                text: Words anywhere in the message, headers and body
                author: Part of the sender's name or address
                subject: Part of the subject
                folder_id: Only this folder (from mail_list_folders)
                since_days: Only messages from the last N days (default: 90)
                unread_only: Only unread messages (default: false)
                limit: How many to show, up to 200 (default: 25)
            """
            if not (text or author or subject):
                raise ToolError("Give text, author or subject to search for")
            data = await self._thunderbird_request({"action": "mail.search", "data": {
                "text": text, "author": author, "subject": subject, "folderId": folder_id,
                "sinceDays": max(since_days, 1), "unreadOnly": unread_only, "limit": min(max(limit, 1), 200)}})
            messages = data.get("messages", [])
            return (f"{len(messages)} of {data.get('total', len(messages))} matches from the last {since_days} days:\n"
                    + "\n".join(self._message_lines(messages)))

        @self._tool()
        async def mail_get_message(message_id: int, headers_only: bool = False, max_body_chars: int = 20000) -> str:
            """Read one message: every header, its attachments and the body as text

            Args:
                message_id: The id at the start of a mail_list_messages or
                    mail_search_messages line
                headers_only: Leave the body out (default: false)
                max_body_chars: Cut the body after this many characters (default: 20000)
            """
            m = await self._thunderbird_request({"action": "mail.getMessage", "data": {
                "messageId": message_id, "headersOnly": headers_only, "maxBodyChars": max(max_body_chars, 0)}})
            lines = [f"Message {m.get('id')} in {m.get('accountId')} {m.get('folder')}",
                     f"Subject: {m.get('subject')}", f"From: {m.get('author')}", f"Date: {m.get('date')}",
                     f"Read: {m.get('read')}, flagged: {m.get('flagged')}, tags: {', '.join(m.get('tags') or []) or 'none'}",
                     "", "Headers:"]
            for name, values in (m.get("headers") or {}).items():
                lines += [f"  {name}: {str(value)[:500]}" for value in values]
            attachments = m.get("attachments") or []
            lines += ["", f"Attachments ({len(attachments)}):"]
            lines += [f"  {a['name']} ({a.get('contentType')}, {a.get('size')} bytes)" for a in attachments]
            if not headers_only:
                cut = " (cut short; raise max_body_chars for more)" if m.get("bodyTruncated") else ""
                lines += ["", f"Body{cut}:", m.get("body") or "(no text body)"]
            return "\n".join(lines)

    def _setup_history_tools(self):
        """Setup history management tools"""

        # History Query Tool
        @self._tool()
        async def history_query(
            query: str,
            max_results: int = 200,
            start_time: Optional[str] = None,
            end_time: Optional[str] = None
        ) -> str:
            """Search browser history

            Args:
                query: Substring to match in URL or title (exact substring match, not tokenized keywords)
                max_results: Maximum number of results (default: 200)
                start_time: Start time filter (ISO format, optional)
                end_time: End time filter (ISO format, optional)
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "history.query",
                "data": {
                    "query": query,
                    "maxResults": max_results,
                    **({"startTime": start_time} if start_time else {}),
                    **({"endTime": end_time} if end_time else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error querying history: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                items = response["data"].get("items", [])
                total_count = response["data"].get("totalCount", len(items))

                if not items:
                    return f"No history items found for query: {query}"

                result = f"Found {total_count} history items for '{query}':\n"
                for item in items:
                    visit_time = item.get("lastVisitTime", "Unknown time")
                    visit_count = item.get("visitCount", 0)
                    result += f"- {item.get('title', 'No title')} - {item.get('url', 'No URL')} (visited {visit_count} times, last: {visit_time})\n"

                return result

            return f"Unable to query history for: {query}"

        # Get Recent History Tool
        @self._tool()
        async def history_get_recent(count: int = 10) -> str:
            """Get recent browser history

            Args:
                count: Number of recent items to get (default: 10)
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "history.recent",
                "data": {
                    "count": count
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            # Debug logging for troubleshooting
            #
            # A print() here would go to stdout, which under --stdio carries the
            # JSON-RPC framing: dumping a response there breaks the client's
            # connection on the first call to this tool.
            logger.debug(f"Recent history WebSocket response: {json.dumps(response, indent=2)}")

            if "error" in response:
                return f"Error getting recent history: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                items = response["data"].get("items", [])

                if not items:
                    return "No recent history items found"

                result = f"Recent {len(items)} history items:\n"
                for item in items:
                    visit_time = item.get("lastVisitTime", "Unknown time")
                    result += f"- {item.get('title', 'No title')} - {item.get('url', 'No URL')} (last visit: {visit_time})\n"

                return result

            # More detailed error message for debugging
            return f"Unable to get recent history. Response type: {response.get('type')}, has_data: {'data' in response}, keys: {list(response.keys())}"

        # Delete History Item Tool
        @self._tool()
        async def history_delete_item(url: str) -> str:
            """Delete a specific history item

            Args:
                url: URL of the history item to delete
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "history.delete_item",
                "data": {
                    "url": url
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error deleting history item: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully deleted history item: {url}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to delete history item: {error_msg}"

            return f"Unable to delete history item: {url}"

    def _setup_bookmark_tools(self):
        """Setup bookmark management tools"""

        # List Bookmarks Tool
        @self._tool()
        async def bookmarks_list(folder_id: Optional[str] = None) -> str:
            """List browser bookmarks

            Folders are marked 📁 and bookmarks 🔖. Every line carries its own ID and its
            parent folder's ID — those IDs are what bookmarks_update, bookmarks_delete and
            the parent_id of bookmarks_create take, so this listing is how you find them.
            An empty folder returns "No bookmarks found".

            Args:
                folder_id: Optional folder ID to list bookmarks from

            Returns:
                Formatted string with bookmark information:
                "Bookmarks:
                📁 {folder_title} (ID: {folder_id}, Parent: {parent_id})
                🔖 {bookmark_title} - {bookmark_url} (ID: {bookmark_id}, Parent: {parent_id})"

                Not exposed to clients — see the description above.
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.list",
                "data": {
                    **({"folderId": folder_id} if folder_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error listing bookmarks: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                bookmarks = response["data"].get("bookmarks", [])

                if not bookmarks:
                    return "No bookmarks found"

                result = "Bookmarks:\n"
                for bookmark in bookmarks:
                    parent_info = f", Parent: {bookmark.get('parentId', 'None')}"
                    if bookmark.get("isFolder", False):
                        result += f"📁 {bookmark.get('title', 'Untitled Folder')} (ID: {bookmark.get('id')}{parent_info})\n"
                    else:
                        result += f"🔖 {bookmark.get('title', 'Untitled')} - {bookmark.get('url', 'No URL')} (ID: {bookmark.get('id')}{parent_info})\n"

                return result

            return "Unable to list bookmarks"

        # Search Bookmarks Tool
        @self._tool()
        async def bookmarks_search(query: str) -> str:
            """Search browser bookmarks by title or URL

            Matches bookmarks only — **folders are never returned**, so a folder cannot be
            found this way; use bookmarks_list to walk the tree instead. Each result carries
            its own ID and its parent folder's ID, which is what bookmarks_update and
            bookmarks_delete take. No matches returns "No bookmarks found".

            Args:
                query: Search query for bookmarks

            Returns:
                Formatted string with search results:
                "Found N bookmarks for 'query':
                🔖 {bookmark_title} - {bookmark_url} (ID: {bookmark_id}, Parent: {parent_id})"

                Not exposed to clients — see the description above.
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.search",
                "data": {
                    "query": query
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error searching bookmarks: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                bookmarks = response["data"].get("bookmarks", [])

                if not bookmarks:
                    return f"No bookmarks found for query: {query}"

                result = f"Found {len(bookmarks)} bookmarks for '{query}':\n"
                for bookmark in bookmarks:
                    if not bookmark.get("isFolder", False):
                        parent_info = f", Parent: {bookmark.get('parentId', 'None')}"
                        result += f"🔖 {bookmark.get('title', 'Untitled')} - {bookmark.get('url', 'No URL')} (ID: {bookmark.get('id')}{parent_info})\n"

                return result

            return f"Unable to search bookmarks for: {query}"

        # Create Bookmark Tool
        @self._tool()
        async def bookmarks_create(
            title: str,
            url: str,
            parent_id: Optional[str] = None
        ) -> str:
            """Create a new bookmark

            Args:
                title: Title of the bookmark
                url: URL of the bookmark
                parent_id: Optional parent folder ID
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.create",
                "data": {
                    "title": title,
                    "url": url,
                    **({"parentId": parent_id} if parent_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error creating bookmark: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                bookmark = response["data"].get("bookmark", {})
                return f"Created bookmark: {bookmark.get('title', title)} - {bookmark.get('url', url)} (ID: {bookmark.get('id')})"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to create bookmark: {error_msg}"

            return f"Unable to create bookmark: {title}"

        # Create Bookmark Folder Tool
        @self._tool()
        async def bookmarks_create_folder(
            title: str,
            parent_id: Optional[str] = None
        ) -> str:
            """Create a new bookmark folder

            Args:
                title: Title of the folder
                parent_id: Optional parent folder ID
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.createFolder",
                "data": {
                    "title": title,
                    **({"parentId": parent_id} if parent_id else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error creating folder: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                folder = response["data"].get("folder", {})
                return f"Created folder: {folder.get('title', title)} (ID: {folder.get('id')})"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to create folder: {error_msg}"

            return f"Unable to create folder: {title}"

        # Update Bookmark Tool
        @self._tool()
        async def bookmarks_update(
            bookmark_id: str,
            title: Optional[str] = None,
            url: Optional[str] = None
        ) -> str:
            """Update a bookmark or folder's title and/or URL

            Args:
                bookmark_id: ID of the bookmark or folder to update
                title: New title (optional)
                url: New URL (optional, only for bookmarks not folders)
            """
            if title is None and url is None:
                return "Error: At least one of title or url must be provided"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.update",
                "data": {
                    "bookmarkId": bookmark_id,
                    **({"title": title} if title is not None else {}),
                    **({"url": url} if url is not None else {})
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error updating bookmark: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                bookmark = response["data"].get("bookmark", {})
                return f"Updated bookmark: {bookmark.get('title', '')} (ID: {bookmark.get('id')})"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to update bookmark: {error_msg}"

            return f"Unable to update bookmark: {bookmark_id}"

        # Delete Bookmark Tool
        @self._tool()
        async def bookmarks_delete(bookmark_id: str) -> str:
            """Delete a bookmark

            Args:
                bookmark_id: ID of the bookmark to delete
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "bookmarks.delete",
                "data": {
                    "bookmarkId": bookmark_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error deleting bookmark: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully deleted bookmark {bookmark_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to delete bookmark: {error_msg}"

            return f"Unable to delete bookmark {bookmark_id}"

    def _setup_browser_tools(self):
        """Setup tools that list and choose between connected browsers"""

        @self._tool()
        async def browsers_list() -> str:
            """List the browsers connected to this server, marking the active one

            Each Firefox profile running the add-on is one browser. Every other
            tool acts on the active browser; tab, window and bookmark ids belong
            to it and mean nothing in another. Names come from each add-on's
            options page.
            """
            server = self.websocket_server
            browsers = list(getattr(server, 'browsers', {}).values())
            away = ""
            if server.active_browser and server.active_browser not in server.browsers:
                away = f"\n{server.no_browser_reason()}"
            if not browsers:
                return "No browser is connected" + away

            lines = []
            for browser in browsers:
                active = " (active)" if browser.key == server.active_browser else ""
                version = f", add-on {browser.version}" if browser.version else ", add-on older than 1.4.3"
                minutes = int((time.time() - browser.connected_at) // 60)
                app = " [Thunderbird]" if browser.app == 'thunderbird' else ""
                paired = ", paired" if getattr(browser, 'paired', False) else ""
                lines.append(f"- {browser.name}{app}{active}: id {browser.key[:8]}{version}{paired}, "
                             f"connected {minutes} min ago")
            return f"Connected browsers ({len(browsers)}):\n" + "\n".join(lines) + away

        @self._tool()
        async def browsers_select(browser: str) -> str:
            """Make another connected browser the one every other tool acts on

            Args:
                browser: The browser's name, as browsers_list shows it, or the
                    start of its id. Names are not case sensitive.

            Running request monitors stay with the browser they were started in.
            """
            try:
                chosen = self.websocket_server.select_browser(browser)
            except ValueError as e:
                return str(e)
            return f"Now acting on {chosen.name} (id {chosen.key[:8]})"

    def _setup_debug_tools(self):
        """Setup connection diagnostics tools"""

        @self._tool()
        async def debug_websocket_status() -> str:
            """Debug WebSocket connection status

            Says whether the browser extension is connected, and from where. Every
            other tool fails with a timeout when it is not, so check here first.
            """
            # Reports the active browser; browsers_list shows every connection.
            connection = getattr(self.websocket_server, 'extension_connection', None)

            if connection is None:
                return "No browser extension is connected"

            # close_code is None until the socket closes, which is the same
            # liveness test the server itself makes before displacing a connection.
            if getattr(connection, 'close_code', None) is not None:
                return "No browser extension is connected (the last connection has closed)"

            address = getattr(connection, 'remote_address', None)
            where = f" from {address[0]}:{address[1]}" if address else ""
            others = len(getattr(self.websocket_server, 'browsers', {}) or {}) - 1
            extra = f"; {others} more browser(s) connected, see browsers_list" if others > 0 else ""
            return f"Browser extension connected{where}{extra}"

    def _setup_navigation_tools(self):
        """Setup navigation tools"""

        # Navigate Back Tool
        class NavigationBackParams(BaseModel):
            """Parameters for navigating back"""
            tab_id: int = Field(description="ID of the tab to navigate back in")

        @self._tool()
        async def navigation_back(params: NavigationBackParams) -> str:
            """Navigate back in browser history for a tab"""
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.back",
                "data": {
                    "tabId": params.tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating back: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully navigated back in tab {params.tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate back: {error_msg}"

            return f"Unable to navigate back in tab {params.tab_id}"

        # Navigate Forward Tool
        class NavigationForwardParams(BaseModel):
            """Parameters for navigating forward"""
            tab_id: int = Field(description="ID of the tab to navigate forward in")

        @self._tool()
        async def navigation_forward(params: NavigationForwardParams) -> str:
            """Navigate forward in browser history for a tab"""
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.forward",
                "data": {
                    "tabId": params.tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating forward: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully navigated forward in tab {params.tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate forward: {error_msg}"

            return f"Unable to navigate forward in tab {params.tab_id}"

        # Reload Page Tool
        @self._tool()
        async def navigation_reload(tab_id: int, bypass_cache: bool = False) -> str:
            """Reload a page in a tab

            Returns as soon as the reload is issued, not when the page has
            finished loading. Code that watches for what the reload does - a
            request monitor, a content read - has to wait for the page itself.

            Args:
                tab_id: ID of the tab to reload
                bypass_cache: Whether to bypass cache when reloading (default: False)
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.reload",
                "data": {
                    "tabId": tab_id,
                    "bypassCache": bypass_cache
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error reloading page: {response['error']}"

            if response.get("type") == "response":
                cache_text = " (bypassing cache)" if bypass_cache else ""
                return f"Successfully reloaded tab {tab_id}{cache_text}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to reload page: {error_msg}"

            return f"Unable to reload tab {tab_id}"

        # Go to URL Tool
        @self._tool()
        async def navigation_go_to_url(tab_id: int, url: str) -> str:
            """Navigate to a specific URL in a tab

            Args:
                tab_id: ID of the tab to navigate
                url: URL to navigate to
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.go_to_url",
                "data": {
                    "tabId": tab_id,
                    "url": url
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating to URL: {response['error']}"

            if response.get("type") == "response":
                return f"Successfully navigated tab {tab_id} to {url}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate to URL: {error_msg}"

            return f"Unable to navigate tab {tab_id} to {url}"

    def _setup_content_tools(self):
        """Setup content access tools"""

        # Get Page Text Tool
        @self._tool()
        async def content_get_text(tab_id: int, max_length: Optional[int] = None) -> str:
            """Get text content from a tab's page

            Args:
                tab_id: ID of the tab to get content from
                max_length: Optional maximum length of text to return (default: unlimited)
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.get_text",
                "data": {
                    "tabId": tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting page text: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                text = response["data"].get("text", "")
                url = response["data"].get("url", "Unknown URL")
                title = response["data"].get("title", "Unknown Title")

                if not text:
                    return f"No text content found in tab {tab_id} ({title})"

                # Apply length limit if specified
                if max_length is not None and len(text) > max_length:
                    truncated_text = text[:max_length]
                    return f"Text content from {title} ({url}):\n\n{truncated_text}..."
                else:
                    return f"Text content from {title} ({url}):\n\n{text}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to get page text: {error_msg}"

            return f"Unable to get text content from tab {tab_id}"

        # Get Page HTML Tool
        @self._tool()
        async def content_get_html(tab_id: int) -> str:
            """Get HTML content from a tab's page

            Args:
                tab_id: ID of the tab to get HTML content from
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.get_html",
                "data": {
                    "tabId": tab_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting page HTML: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                html = response["data"].get("html", "")
                url = response["data"].get("url", "Unknown URL")
                title = response["data"].get("title", "Unknown Title")

                if not html:
                    return f"No HTML content found in tab {tab_id} ({title})"

                return f"HTML content from {title} ({url}):\n\n{html[:2000]}{'...' if len(html) > 2000 else ''}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to get page HTML: {error_msg}"

            return f"Unable to get HTML content from tab {tab_id}"

        # Execute Script Tool
        @self._tool()
        async def content_execute_script(tab_id: int, code: str) -> str:
            """Execute JavaScript code in a tab

            The result is the value of the last expression. A script with a
            top-level `return` or `await` runs as an async function instead, and
            then only `return` gives a result. `let`, `const` and `class` names
            are gone in the next call; `var` and `function` ones stay.

            Args:
                tab_id: ID of the tab to execute script in
                code: JavaScript code to execute
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.execute_script",
                "data": {
                    "tabId": tab_id,
                    "script": code
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error executing script: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                result = response["data"].get("result")
                url = response["data"].get("url", "Unknown URL")

                if result is None:
                    return f"Script executed successfully in tab {tab_id} ({url}) - no return value"

                return f"Script result from tab {tab_id} ({url}):\n{result}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to execute script: {error_msg}"

            return f"Unable to execute script in tab {tab_id}"

        async def page_request(message: Dict[str, Any]):
            """Send a {"action", "data"} content request

            Returns (data, None) on success and (None, error message) on failure.
            """
            response = await self.websocket_server.send_request_and_wait({
                "id": str(uuid.uuid4()),
                "type": "request",
                **message,
                "timestamp": datetime.now().isoformat()
            })
            if "error" in response:
                return None, response["error"]
            if response.get("type") == "response" and "data" in response:
                return response["data"], None
            return None, response.get("data", {}).get("message", "Unknown error")

        async def send_file_bytes(files):
            """Send local files to the extension in chunks, for upload_finish or
            copy_image to pick up by the returned upload id

            files is a list of (path, relative path). Returns (upload id, None),
            or (None, error) with the error naming what failed.
            """
            upload_id = str(uuid.uuid4())
            _, error = await page_request({
                "action": "content.upload_begin",
                "data": {"uploadId": upload_id, "files": [
                    {"name": os.path.basename(full), "relativePath": relative,
                     "type": mimetypes.guess_type(full)[0] or ""}
                    for full, relative in files]}
            })
            if error:
                return None, error
            for index, (full, _) in enumerate(files):
                with open(full, "rb") as handle:
                    while chunk := handle.read(UPLOAD_CHUNK_BYTES):
                        _, error = await page_request({
                            "action": "content.upload_chunk",
                            "data": {"uploadId": upload_id, "fileIndex": index,
                                     "data": base64.b64encode(chunk).decode("ascii")}
                        })
                        if error:
                            return None, f"{full}: {error}"
            return upload_id, None

        async def real_input(tab_id, ref, selector, purpose, act, prep_message=None, target_name=None):
            """Do act(backend, context, element) with real input, when the browser has it

            Returns None when the browser has no real input (or it is not
            reachable), so the caller falls back to synthetic events; otherwise
            (prep, None) or (None, error). The content script's prep step goes
            first: it moves the on-page cursor, says where the page is on screen,
            marks the tab for BiDi to find, and goes through the person's
            settings (pause, permissions, blocked sites) like any other action,
            so real input cannot get round them.

            On Windows the input is posted to the tab's Firefox window
            (win_input.py), which needs no remote control; elsewhere, or when
            that window is not found, it goes through WebDriver BiDi.
            """
            server = self.websocket_server
            if not getattr(server, 'real_input_enabled', lambda: False)():
                REAL_INPUT_MISS.set("real input is off in this browser's add-on settings")
                return None
            async with server.real_input_turn():
                return await real_input_in_turn(tab_id, ref, selector, purpose, act, prep_message, target_name)

        async def real_input_in_turn(tab_id, ref, selector, purpose, act, prep_message, target_name):
            server = self.websocket_server
            ports = []
            if server.real_input_allowed():
                ports = server.real_input_ports()
                if not ports:
                    # No port set and none found yet: the usual ones that are open
                    ports = await listening_ports(LIKELY_PORTS)
            if not WINDOW_INPUT and not ports:
                REAL_INPUT_MISS.set("no Firefox under remote control was found (start it with --remote-debugging-port)")
                return None
            token = uuid.uuid4().hex
            message = prep_message(token) if prep_message else {
                "action": "content.real_input_prep",
                "data": {"tabId": tab_id, "ref": ref, "selector": selector, "targetName": target_name,
                         "purpose": purpose, "token": token}
            }
            # Window messages reach only the tab showing in its window
            message["data"]["activate"] = WINDOW_INPUT
            prep, error = await page_request(message)
            if error:
                return None, error
            # A target real input cannot work (a <select>): the scripted path,
            # even when the server runs real input only
            if prep.get("scripted"):
                return SCRIPTED_ONLY

            prep["token"] = token

            async def run(backend, context):
                if prep_message:
                    await act(backend, context, prep)
                else:
                    element = await backend.element(context, prep["selector"]) if prep.get("selector") else None
                    await act(backend, context, element)
                return prep, None

            if WINDOW_INPUT:
                for delay in (*WINDOW_SETTLE_DELAYS, None):
                    try:
                        async with WindowInput(prep) as window:
                            return await run(window, window.hwnd)
                    except WindowNotShowing as unavailable:
                        logger.info(f"No window input yet: {unavailable}")
                        REAL_INPUT_MISS.set(str(unavailable))
                        if delay is None:
                            break
                        await asyncio.sleep(delay)
                        prep, error = await page_request(message)
                        if error:
                            return None, error
                        if prep.get("scripted"):
                            return SCRIPTED_ONLY
                        prep["token"] = token
                    except WindowInputUnavailable as unavailable:
                        logger.info(f"No window input: {unavailable}")
                        REAL_INPUT_MISS.set(str(unavailable))
                        break
                    except (RuntimeError, ValueError) as failure:
                        return None, str(failure)
            # Only the Firefox holding this tab finds the token, so with several
            # profiles under remote control the right one picks itself
            for port in ports:
                try:
                    async with BidiSession(port) as bidi:
                        context = await bidi.find_tab(token)
                        server.real_input_found_on(port)
                        return await run(bidi, context)
                except BidiUnavailable as unavailable:
                    logger.info(f"Real input not on port {port}: {unavailable}")
                except (RuntimeError, ValueError, asyncio.TimeoutError) as failure:
                    return None, str(failure)
            if ports:
                server.real_input_found_on(0)
            return None

        def scripted_refusal(action):
            """Why the scripted fallback is refused, when the server runs real input only

            The fallback is what a page sees as untrusted events. Some editors
            (Facebook's composer) show the text yet keep none of it, so a
            fallback that "succeeded" can publish an empty post.
            """
            if not getattr(self.websocket_server, 'real_input_only', False):
                return None
            why = REAL_INPUT_MISS.get() or "this browser has no real input right now"
            return (f"Did not {action}: the server runs with real input only, and {why}. "
                    "Start the server without --real-input-only to allow scripted events.")

        def spot(tab_id, ref=None, selector=None, x=None, y=None, side="", name=None):
            """An element or a viewport point, as the content script takes it"""
            if not ref and not selector and not name and (x is None or y is None):
                raise ValueError(f"Give {side}ref, {side}selector, {side}text, or both {side}x and {side}y")
            return {"ref": ref, "selector": selector, "targetName": name, "x": x, "y": y}

        async def real_pointer(tab_id, purpose, start, end, act, button=0):
            """Real hover, point click or drag: the content script moves the
            cursor and says where in the viewport, the server does the rest"""
            return await real_input(tab_id, None, None, purpose, act, prep_message=lambda token: {
                "action": "content.real_pointer_prep",
                "data": {"tabId": tab_id, "from": start, "to": end, "purpose": purpose, "token": token,
                         "button": button}})

        def target(tab_id: int, ref: Optional[str], selector: Optional[str],
                   name: Optional[str] = None) -> Dict[str, Any]:
            if not ref and not selector and not name:
                raise ValueError("Pass ref (from content_snapshot), selector, or the element's visible text")
            return {"tabId": tab_id, "ref": ref, "selector": selector, **({"targetName": name} if name else {})}

        # Real input can report success while the page got nothing (on
        # Windows, a Firefox window behind another one, or one Firefox does not
        # deliver to). It is said, never redone: the input may still have
        # landed where the page cannot see it (an iframe, the browser itself,
        # another window), and doing it again could pay or send twice.
        NOT_SEEN = ("; but the page showed no sign of a real {what} press, so it may not have "
                    "arrived (is the Firefox window behind another one?). Check before doing it again")

        async def click_effect(tab_id: int, prep: Dict[str, Any]) -> str:
            """What a real click changed on the page, from the content script"""
            data, error = await page_request({"action": "content.click_effect",
                                              "data": {"tabId": tab_id, "token": prep.get("token")}})
            if error:
                return ""
            effect = f": {data['effect']}" if data.get("effect") else ""
            return effect + (NOT_SEEN.format(what="pointer") if data.get("notArrived") else "")

        def effect_of(data: Dict[str, Any]) -> str:
            return f": {data['effect']}" if data.get("effect") else ""

        clipboard = {"agent": None}

        async def clipboard_stamp():
            """Something that changes whenever the clipboard does, without the
            contents: Windows' change counter, or elsewhere a hash from the add-on"""
            if WINDOW_INPUT:
                return clipboard_sequence()
            data, error = await page_request({"action": "content.clipboard_fingerprint", "data": {}})
            return None if error else data.get("fingerprint")

        async def note_copy(before) -> str:
            """After keys that copy: wait for the app to fill the clipboard and
            remember it as the agent's, or say nothing was copied"""
            deadline = time.monotonic() + COPY_WAIT_S
            while time.monotonic() < deadline:
                now = await clipboard_stamp()
                if now is not None and now != before:
                    clipboard["agent"] = now
                    return ". The clipboard now holds the copy"
                await asyncio.sleep(0.15)
            return ". Nothing was copied: the clipboard did not change"

        async def paste_refusal(allow_clipboard: bool) -> Optional[str]:
            """A paste only puts on the page what the agent itself copied. What
            else is on the clipboard is the person's (a message, a password, an
            address), and pasting it onto a page, often a shared one, leaks it."""
            if allow_clipboard:
                return None
            now = await clipboard_stamp()
            if now is not None and now == clipboard["agent"]:
                return None
            return ("Failed to paste: the clipboard holds something the agent did not copy, most likely the "
                    "person's own. Copy what you mean to paste first (Control+c on it, or content_copy_image), "
                    "or pass allow_clipboard=true only if the person asked to paste their clipboard")

        # Snapshot Tool
        @self._tool()
        async def content_snapshot(tab_id: int, max_elements: int = 300) -> str:
            """List the visible interactive elements of a page, each with a ref
            that content_click and content_type accept

            Args:
                tab_id: ID of the tab to snapshot
                max_elements: Stop listing after this many elements (default: 300)
            """
            data, error = await page_request(
                {"action": "content.snapshot",
                 "data": {"tabId": tab_id, "maxElements": max_elements}}
            )
            if error:
                return f"Failed to snapshot page: {error}"
            header = f"Snapshot of {data.get('title', '')} ({data.get('url', '')}):"
            return f"{header}\n{data.get('snapshot', '')}"

        # Click Tool
        @self._tool()
        async def content_click(
            tab_id: int,
            ref: Optional[str] = None,
            selector: Optional[str] = None,
            double_click: bool = False,
            right_click: bool = False,
            x: Optional[int] = None,
            y: Optional[int] = None,
            text: Optional[str] = None
        ) -> str:
            """Click an element. With real input set up in the add-on's settings the
            click is a real one, which canvas apps and menus that ignore scripted
            clicks accept; otherwise the events are dispatched by script.

            The result says what the click changed: a new address, a menu or
            dialog opening or closing, the caret moving into a field, the element
            expanding or getting checked. When a selector matches several
            elements, the first visible one is clicked.

            Args:
                tab_id: ID of the tab
                ref: Element ref from content_snapshot, such as "r12"
                selector: CSS selector, used when no ref is given
                double_click: Double-click instead, as opening a file or folder
                    in a file list usually needs (default: False)
                right_click: Right-click, to open the page's context menu (default: False)
                x: With y, click this point in the visible page (CSS pixels from the
                    top left) instead of an element, for canvases such as a Miro board
                y: See x
                text: Click the element showing this text or named this (its label,
                    aria-label or placeholder), as a person would point at it,
                    instead of a ref or selector: "Send", "Add comment"
            """
            done = "Right-clicked" if right_click else "Double-clicked" if double_click else "Clicked"
            if not ref and not selector and not text:
                start = spot(tab_id, x=x, y=y)
                real = await real_pointer(tab_id, "click", start, None, lambda bidi, context, prep:
                                          bidi.click_at(context, prep["from"]["x"], prep["from"]["y"],
                                                        button=2 if right_click else 0,
                                                        clicks=2 if double_click and not right_click else 1))
                if real is not None:
                    prep, error = real
                    if error:
                        return f"Failed to click: {error}"
                    return f"{done} {prep.get('target')} at {x},{y} (real input){await click_effect(tab_id, prep)}"
                if refusal := scripted_refusal("click"):
                    return refusal
                data, error = await page_request({"action": "content.click", "data": {
                    "tabId": tab_id, **start, "doubleClick": double_click, "rightClick": right_click}})
                if error:
                    return f"Failed to click: {error}"
                return f"{done} {data.get('clicked')}{effect_of(data)}"
            real = await real_input(tab_id, ref, selector, "click", lambda bidi, context, element:
                                    bidi.click(context, element, button=2 if right_click else 0,
                                               clicks=2 if double_click and not right_click else 1),
                                    target_name=text)
            if real is not None:
                prep, error = real
                if error:
                    return f"Failed to click: {error}"
                return f"{done} {prep.get('target')} (real input){await click_effect(tab_id, prep)}"
            if refusal := scripted_refusal("click"):
                return refusal
            data, error = await page_request({
                "action": "content.click",
                "data": {**target(tab_id, ref, selector, text), "doubleClick": double_click,
                         "rightClick": right_click}
            })
            if error:
                return f"Failed to click: {error}"
            return f"{done} {data.get('clicked')}{effect_of(data)}"

        # Hover Tool
        @self._tool()
        async def content_hover(
            tab_id: int, ref: Optional[str] = None, selector: Optional[str] = None,
            x: Optional[int] = None, y: Optional[int] = None, text: Optional[str] = None
        ) -> str:
            """Move the mouse onto an element or a point without clicking, to open
            hover menus and show tooltips or buttons that appear on hover

            Args:
                tab_id: ID of the tab
                ref: Element ref from content_snapshot
                selector: CSS selector, used when no ref is given
                x: With y, a point in the visible page (CSS pixels) instead of an element
                y: See x
                text: The element showing this text or named this, instead of a ref
                    or selector
            """
            start = spot(tab_id, ref, selector, x, y, name=text)
            real = await real_pointer(tab_id, "hover", start, None, lambda bidi, context, prep:
                                      bidi.hover(context, prep["from"]["x"], prep["from"]["y"]))
            if real is not None:
                prep, error = real
                if error:
                    return f"Failed to hover: {error}"
                return f"Hovering over {prep.get('target')} (real input)"
            if refusal := scripted_refusal("hover"):
                return refusal
            data, error = await page_request({"action": "content.hover", "data": {"tabId": tab_id, **start}})
            if error:
                return f"Failed to hover: {error}"
            return f"Hovering over {data.get('hovered')}"

        # Drag Tool
        @self._tool()
        async def content_drag(
            tab_id: int,
            from_ref: Optional[str] = None, from_selector: Optional[str] = None,
            from_x: Optional[int] = None, from_y: Optional[int] = None,
            to_ref: Optional[str] = None, to_selector: Optional[str] = None,
            to_x: Optional[int] = None, to_y: Optional[int] = None,
            button: str = "left"
        ) -> str:
            """Drag from one element or point to another and let go: move items on
            a board or canvas, reorder lists, drop a card in a column, move sliders

            Each end is an element (ref or selector) or a point in the visible page
            (x and y, CSS pixels). With real input set up it is a real mouse drag.
            To move the view around a canvas rather than an item on it, drag with
            the button the site pans with: "right" on Miro, "middle" on Figma,
            draw.io and many maps.

            Args:
                tab_id: ID of the tab
                from_ref: Element ref to start on
                from_selector: CSS selector to start on
                from_x: With from_y, the point to start at
                from_y: See from_x
                to_ref: Element ref to drop on
                to_selector: CSS selector to drop on
                to_x: With to_y, the point to drop at
                to_y: See to_x
                button: The mouse button held: "left", "middle" or "right"
                    (default: "left")
            """
            buttons = {"left": 0, "middle": 1, "right": 2}
            if button not in buttons:
                raise ValueError('button must be "left", "middle" or "right"')
            pan = button != "left"
            button = buttons[button]
            start = spot(tab_id, from_ref, from_selector, from_x, from_y, "from_")
            end = spot(tab_id, to_ref, to_selector, to_x, to_y, "to_")
            async def drag_for_real(bidi, context, prep):
                # Firefox's remote control starts a page's own drag-and-drop
                # (draggable elements, links, images) but never drops, so those
                # get the scripted drag, whose drop events pages do accept. Only
                # the left button starts one.
                if pan or not prep.get("pageDragAndDrop"):
                    await bidi.drag(context, prep["from"], prep["to"], button)

            real = await real_pointer(tab_id, "drag", start, end, drag_for_real, button)
            if real is not None:
                prep, error = real
                if error:
                    return f"Failed to drag: {error}"
                if pan or not prep.get("pageDragAndDrop"):
                    return f"Dragged {prep.get('target')} (real input)"
            # A page's own drag-and-drop goes scripted even with real input, since
            # Firefox's remote control never drops it; only a missing backend is refused
            elif refusal := scripted_refusal("drag"):
                return refusal
            data, error = await page_request({"action": "content.drag",
                                              "data": {"tabId": tab_id, "from": start, "to": end,
                                                       "button": button}})
            if error:
                return f"Failed to drag: {error}"
            return f"Dragged {data.get('dragged')}"

        # Type Tool
        @self._tool()
        async def content_type(
            tab_id: int,
            text: str,
            ref: Optional[str] = None,
            selector: Optional[str] = None,
            clear: bool = True,
            submit: bool = False,
            field: Optional[str] = None
        ) -> str:
            """Type text into an input, textarea or editable element

            On a <select>, picks the option whose value or label is the text.

            Args:
                tab_id: ID of the tab
                text: Text to enter
                ref: Element ref from content_snapshot
                selector: CSS selector, used when no ref is given
                clear: Replace the current value instead of appending (default: True)
                submit: Press Enter afterwards (default: False)
                field: The field's label, aria-label or placeholder ("Search",
                    "Leave a reply"), instead of a ref or selector
            """
            async def type_for_real(bidi, context, element):
                await bidi.click(context, element)
                if clear:
                    await bidi.press(context, "Control+a")
                    await bidi.press(context, "Backspace")
                await bidi.type_text(context, text)

            real = await real_input(tab_id, ref, selector, "type", type_for_real, target_name=field)
            if real is not None and real is not SCRIPTED_ONLY:
                prep, error = real
                if error:
                    return f"Failed to type: {error}"
                # The field the prep found, so a name is not looked up again
                found = {"tabId": tab_id, "ref": None, "selector": prep.get("selector")}
                checked, error = await page_request({
                    "action": "content.check_typed",
                    "data": {**found, "text": text, "clear": clear, "token": prep["token"]}
                })
                if error:
                    return f"Failed to type: {error}"
                if submit:
                    await real_input(tab_id, None, prep.get("selector"), "key",
                                     lambda bidi, context, element: bidi.press(context, "Enter"))
                return f"Typed into {checked.get('typed')} (real input)"
            if real is not SCRIPTED_ONLY and (refusal := scripted_refusal("type")):
                return refusal
            data, error = await page_request({
                "action": "content.type",
                "data": {**target(tab_id, ref, selector, field),
                         "text": text, "clear": clear, "submit": submit}
            })
            if error:
                return f"Failed to type: {error}"
            return f"Typed into {data.get('typed')}"

        # Press Key Tool
        @self._tool()
        async def content_press_key(
            tab_id: int, key: str, ref: Optional[str] = None, selector: Optional[str] = None,
            allow_clipboard: bool = False
        ) -> str:
            """Press a key such as "Enter", "Escape" or "Tab" on an element, or on
            the focused element when no target is given

            A single character ("m", "5") is typed into a text field unless the
            page cancels the key. Use content_type for longer text.

            Copy keys (Control+c, Control+x) report whether anything was copied.
            Paste keys (Control+v, Shift+Insert) go through only while the
            clipboard holds what the agent copied, so the person's own clipboard
            never lands on a page by accident.

            Args:
                tab_id: ID of the tab
                key: Key name as in KeyboardEvent.key; with real input also a
                    combination such as "Control+a" or "Shift+Tab"
                ref: Element ref from content_snapshot
                selector: CSS selector, used when no ref is given
                allow_clipboard: Paste even though the clipboard holds something
                    the agent did not copy. Only when the person asked to paste
                    their own clipboard (default: False)
            """
            if PASTE_KEYS.match(key):
                refusal = await paste_refusal(allow_clipboard)
                if refusal:
                    return refusal
            copy_before = await clipboard_stamp() if COPY_KEYS.match(key) else None

            async def press_for_real(bidi, context, element):
                if element:
                    await bidi.click(context, element)
                await bidi.press(context, key)

            real = await real_input(tab_id, ref, selector, "key", press_for_real)
            if real is not None:
                prep, error = real
                if error:
                    return f"Failed to press {key}: {error}"
                copied = await note_copy(copy_before) if COPY_KEYS.match(key) else ""
                # Firefox keeps some combinations for itself (Control+t), and
                # those never reach the page anyway
                unseen = ""
                if not MODIFIED_KEY.search(key):
                    checked, _ = await page_request({"action": "content.real_input_arrived",
                                                     "data": {"tabId": tab_id, "token": prep["token"]}})
                    if checked and checked.get("arrived") is False:
                        unseen = NOT_SEEN.format(what="key")
                return f"Pressed {key} (real input){copied}{unseen}"
            if refusal := scripted_refusal(f"press {key}"):
                return refusal
            data, error = await page_request({
                "action": "content.press_key",
                "data": {"tabId": tab_id, "key": key, "ref": ref, "selector": selector}
            })
            if error:
                return f"Failed to press {key}: {error}"
            copied = await note_copy(copy_before) if COPY_KEYS.match(key) else ""
            return f"Pressed {data.get('pressed')}{copied}"

        # Upload Files Tool
        @self._tool()
        async def content_upload_files(
            tab_id: int,
            path: str,
            ref: Optional[str] = None,
            selector: Optional[str] = None,
            trigger_ref: Optional[str] = None,
            trigger_selector: Optional[str] = None
        ) -> str:
            """Put a local file, or every file in a local folder, on a page's file input

            Folders keep their structure on inputs that pick folders, so a site
            that rebuilds the tree (a cloud drive's folder upload) gets the
            subfolders; on a plain file input the files arrive flat.

            Pages that open the file dialog from a button or menu item instead
            of showing an input: pass that element as trigger_ref or
            trigger_selector, and the input it would open is filled instead of
            the dialog appearing.

            Args:
                tab_id: ID of the tab
                path: Local file or folder path
                ref: Ref of the <input type=file> from content_snapshot
                selector: CSS selector of the <input type=file>
                trigger_ref: Ref of the element that opens the file dialog
                trigger_selector: CSS selector of the element that opens the file dialog
            """
            if not os.path.exists(path):
                return f"Failed to upload: {path} does not exist"
            if os.path.isdir(path):
                root = os.path.basename(os.path.normpath(path))
                files = []
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames.sort()
                    for name in sorted(filenames):
                        full = os.path.join(dirpath, name)
                        relative = os.path.relpath(full, path).replace(os.sep, "/")
                        files.append((full, f"{root}/{relative}"))
                if not files:
                    return f"Failed to upload: {path} contains no files"
            else:
                files = [(path, "")]

            if trigger_ref or trigger_selector:
                captured, error = await page_request({
                    "action": "content.capture_file_input",
                    "data": {"tabId": tab_id, "triggerRef": trigger_ref,
                             "triggerSelector": trigger_selector}
                })
                if error:
                    return f"Failed to open the file input: {error}"
                ref, selector = captured["ref"], None
            input_target = target(tab_id, ref, selector)

            upload_id, error = await send_file_bytes(files)
            if error:
                return f"Failed to upload: {error}"
            data, error = await page_request({
                "action": "content.upload_finish",
                "data": {**input_target, "uploadId": upload_id}
            })
            if error:
                return f"Failed to upload: {error}"
            shown = ", ".join(data.get("paths", []))
            more = data["count"] - len(data.get("paths", []))
            return (f"Set {data['count']} file(s) on {data.get('target')}: {shown}"
                    + (f" and {more} more" if more > 0 else ""))

        # Copy Image Tool
        @self._tool()
        async def content_copy_image(
            tab_id: Optional[int] = None,
            ref: Optional[str] = None,
            selector: Optional[str] = None,
            path: Optional[str] = None
        ) -> str:
            """Put an image on the system clipboard, as the browser's "Copy Image"
            does: an image on a page (give tab_id with ref or selector) or a local
            image file (give path)

            The image goes on the clipboard as PNG. Paste it into a page with
            content_paste, or the person can paste it with Ctrl+V. To put an
            image on a Miro board, use content_upload_files on its "Upload from
            device" instead: Miro ignores pastes it did not get from a person.

            Args:
                tab_id: ID of the tab holding the image
                ref: Ref of the image, or of an element holding one, from content_snapshot
                selector: CSS selector of the image, used when no ref is given
                path: Local image file to copy instead of an image on a page
            """
            if path:
                if not os.path.isfile(path):
                    return f"Failed to copy: {path} is not a file"
                upload_id, error = await send_file_bytes([(path, "")])
                if error:
                    return f"Failed to copy: {error}"
                request = {"uploadId": upload_id}
            else:
                if tab_id is None:
                    return "Failed to copy: give tab_id with ref or selector, or a path"
                request = target(tab_id, ref, selector)
            data, error = await page_request({"action": "content.copy_image", "data": request})
            if error:
                return f"Failed to copy the image: {error}"
            clipboard["agent"] = await clipboard_stamp()
            return (f"Copied {data.get('copied')} to the clipboard "
                    f"({data.get('width')}x{data.get('height')} PNG)")

        # Paste Tool
        @self._tool()
        async def content_paste(
            tab_id: int, ref: Optional[str] = None, selector: Optional[str] = None,
            allow_clipboard: bool = False
        ) -> str:
            """Paste what is on the system clipboard into a page, as Ctrl+V would

            The page gets a real paste event with the clipboard's contents, text or
            an image (see content_copy_image). Firefox lets the extension paste only
            into a textarea: give one as the target to paste into it. With no
            target, or another kind, the paste goes to the page as a whole, for
            pages that listen for it there. Miro ignores it.

            Args:
                tab_id: ID of the tab
                ref: Ref of a textarea from content_snapshot to paste into
                selector: CSS selector, used when no ref is given
                allow_clipboard: Paste even though the clipboard holds something
                    the agent did not copy. Only when the person asked to paste
                    their own clipboard (default: False)
            """
            refusal = await paste_refusal(allow_clipboard)
            if refusal:
                return refusal
            data, error = await page_request({
                "action": "content.paste",
                "data": {"tabId": tab_id, "ref": ref, "selector": selector}
            })
            if error:
                return f"Failed to paste: {error}"
            return f"Pasted into {data.get('pasted')}"

        # Execute Predefined Script Tool
        @self._tool()
        async def content_execute_predefined(
            tab_id: int,
            script_name: str,
            script_args: str = ""
        ) -> str:
            """Execute a predefined external script and run its JavaScript output in a tab

            Args:
                tab_id: ID of the tab to execute script in
                script_name: Name of the external script to run
                script_args: JSON array of strings to pass to the external script (e.g., '["arg1", "arg2"]')
                            or empty string for no arguments
            """
            # Every rejection below is logged at warning level.
            #
            # This is the code-execution surface an MCP client can reach, so a
            # refusal is worth a line whether it came from a typo or from an
            # attempt to escape the scripts directory - the two are
            # indistinguishable here, and only the log preserves the attempt.

            # Get the scripts directory from environment variable
            scripts_dir = os.environ.get('FOXMCP_EXT_SCRIPTS')
            if not scripts_dir:
                logger.warning(
                    f"Predefined script '{script_name}' refused: FOXMCP_EXT_SCRIPTS is not set"
                )
                return "Error: FOXMCP_EXT_SCRIPTS environment variable not set"

            # Validate script name to prevent path traversal attacks
            if not script_name or '..' in script_name or '/' in script_name or '\\' in script_name:
                logger.warning(
                    f"Predefined script refused: name {script_name!r} contains a path separator or '..'"
                )
                return f"Error: Invalid script name '{script_name}'. Script names cannot contain path separators or '..' sequences"

            # Additional validation: only allow alphanumeric, underscore, dash, and dot
            import re
            if not re.match(r'^[a-zA-Z0-9._-]+$', script_name):
                logger.warning(
                    f"Predefined script refused: name {script_name!r} has characters outside [a-zA-Z0-9._-]"
                )
                return f"Error: Invalid script name '{script_name}'. Only alphanumeric characters, underscore, dash, and dot are allowed"

            # Resolve absolute paths to prevent symlink attacks
            scripts_dir = os.path.abspath(scripts_dir)
            script_path = os.path.abspath(os.path.join(scripts_dir, script_name))

            # Ensure the resolved script path is still within the scripts directory
            if not script_path.startswith(scripts_dir + os.sep) and script_path != scripts_dir:
                logger.warning(
                    f"Predefined script refused: {script_name!r} resolves to {script_path!r}, "
                    f"outside {scripts_dir!r}"
                )
                return f"Error: Script path '{script_name}' escapes the allowed directory"

            if not os.path.exists(script_path):
                logger.warning(f"Predefined script refused: {script_name!r} not found in {scripts_dir!r}")
                return f"Error: Script '{script_name}' not found in {scripts_dir}"

            if not os.access(script_path, os.X_OK):
                logger.warning(f"Predefined script refused: {script_name!r} is not executable")
                return f"Error: Script '{script_name}' is not executable"

            try:
                # Parse JSON arguments - handle both empty string and JSON arrays
                try:
                    if script_args.strip() == "":
                        # Empty string means no arguments
                        args_list = []
                    else:
                        # Parse as JSON array
                        args_list = json.loads(script_args)
                        if not isinstance(args_list, list):
                            logger.warning(
                                f"Predefined script {script_name!r} refused: script_args is "
                                f"{type(args_list).__name__}, not a list"
                            )
                            return f"Error: script_args must be a JSON array of strings or empty string, got: {type(args_list).__name__}"

                        # Validate all arguments are strings
                        for i, arg in enumerate(args_list):
                            if not isinstance(arg, str):
                                logger.warning(
                                    f"Predefined script {script_name!r} refused: argument {i} is "
                                    f"{type(arg).__name__}, not a string"
                                )
                                return f"Error: All arguments must be strings. Argument {i} is {type(arg).__name__}: {arg}"

                except json.JSONDecodeError as e:
                    logger.warning(f"Predefined script {script_name!r} refused: script_args is not valid JSON - {e}")
                    return f"Error: Invalid JSON in script_args: {e}"

                # Execute the external script with arguments
                logger.info(
                    f"Running predefined script {script_name!r} in tab {tab_id} "
                    f"with {format_script_args_for_log(args_list)}"
                )
                started_at = time.monotonic()
                result = subprocess.run(
                    [script_path] + args_list,
                    capture_output=True,
                    text=True,
                    timeout=30  # 30 second timeout
                )
                elapsed = time.monotonic() - started_at

                if result.returncode != 0:
                    logger.warning(
                        f"Predefined script {script_name!r} exited {result.returncode} "
                        f"after {elapsed:.1f}s: {result.stderr.strip()!r}"
                    )
                    return f"Error: Script '{script_name}' failed with exit code {result.returncode}. stderr: {result.stderr}"

                # The script output should be JavaScript code
                javascript_code = result.stdout.strip()
                if not javascript_code:
                    logger.warning(f"Predefined script {script_name!r} produced no output after {elapsed:.1f}s")
                    return f"Error: Script '{script_name}' produced no output"

                # Log the size, never the JavaScript itself - a generated
                # DOM-walking script runs to tens of kilobytes and would bury
                # every other line in the log.
                logger.info(
                    f"Predefined script {script_name!r} generated {len(javascript_code)} bytes "
                    f"of JavaScript in {elapsed:.1f}s; injecting into tab {tab_id}"
                )

                # Now execute the generated JavaScript in the tab
                request = {
                    "id": str(uuid.uuid4()),
                    "type": "request",
                    "action": "content.execute_script",
                    "data": {
                        "tabId": tab_id,
                        "script": javascript_code
                    },
                    "timestamp": datetime.now().isoformat()
                }

                response = await self.websocket_server.send_request_and_wait(request)

                if "error" in response:
                    logger.warning(
                        f"Predefined script {script_name!r} failed in tab {tab_id}: {response['error']}"
                    )
                    return f"Error executing generated script: {response['error']}"

                if response.get("type") == "response" and "data" in response:
                    result_data = response["data"].get("result")
                    url = response["data"].get("url", "Unknown URL")

                    # The URL is the point of this line: it says which page the
                    # script actually ran against, which the tab id alone does
                    # not - the tab may have navigated since the caller chose it.
                    logger.info(f"Predefined script {script_name!r} completed in tab {tab_id} ({url})")

                    if result_data is None:
                        return f"Predefined script '{script_name}' executed successfully in tab {tab_id} ({url}) - no return value"

                    return f"Predefined script '{script_name}' result from tab {tab_id} ({url}):\n{result_data}"
                elif response.get("type") == "error":
                    error_msg = response.get("data", {}).get("message", "Unknown error")
                    logger.warning(
                        f"Predefined script {script_name!r} failed in tab {tab_id}: {error_msg}"
                    )
                    return f"Failed to execute generated script: {error_msg}"

                return f"Unable to execute generated script in tab {tab_id}"

            except subprocess.TimeoutExpired:
                logger.warning(f"Predefined script {script_name!r} timed out after 30 seconds and was killed")
                return f"Error: Script '{script_name}' timed out after 30 seconds"
            except subprocess.SubprocessError as e:
                logger.warning(f"Predefined script {script_name!r} could not be run: {e}")
                return f"Error executing script '{script_name}': {e}"
            except Exception as e:
                logger.exception(f"Predefined script {script_name!r} raised an unexpected error")
                return f"Unexpected error running script '{script_name}': {e}"

    def _remember_monitor_owner(self, monitor_id):
        """Tie a monitor just started to the MCP client that asked for it

        The server stops a client's monitors when that client disconnects, and
        this is where it learns whose they are. Every monitor is recorded, session
        or no session: one started outside a transport has no client to outlive it
        and is never reaped, but a monitor missing from the registry altogether is
        taken for a stray and stopped on sight.
        """
        if not monitor_id:
            return

        register = getattr(self.websocket_server, 'register_monitor', None)
        if register is None:
            return

        try:
            from fastmcp.server.dependencies import get_context
            session_id = get_context().session_id
        except Exception:
            # Reached in-process rather than over a transport, as the test harness
            # does. register_monitor records its own marker for that.
            session_id = None

        register(monitor_id, session_id)

    def _setup_request_monitoring_tools(self):
        """Setup web request monitoring tools"""

        @self._tool()
        async def requests_start_monitoring(
            url_patterns: List[str],
            options: Optional[Dict[str, Any]] = None,
            tab_id: Optional[int] = None
        ) -> str:
            """
            Start monitoring web requests

            Returns JSON containing a **monitor_id**. Keep it — requests_list_captured,
            requests_get_content and requests_stop_monitoring all take it, so losing it
            leaves the monitor running with no way to read or stop it.

            Args:
                url_patterns: List of URL patterns to monitor (e.g., ["https://api.example.com/*", "*/api/*"])
                options: Optional configuration for monitoring:
                    - capture_response_bodies: bool (default: True)
                    - max_body_size: int (default: 50000) — bytes kept per body; the
                      full size is reported either way
                    - content_types_to_capture: List[str] (default: ["application/json", "text/plain"])
                      — a body comes back as text only if its content type matches one
                      of these and is text to begin with
                tab_id: Optional tab ID to monitor (if not provided, monitors all tabs)

            Returns:
                JSON string with monitor_id and status information
            """
            if not url_patterns:
                return json.dumps({"error": "url_patterns is required"})

            # Set default options
            default_options = {
                "capture_request_bodies": True,
                "capture_response_bodies": True,
                "max_body_size": 50000,
                "content_types_to_capture": ["application/json", "text/plain"],
                "sensitive_headers": ["Authorization", "Cookie"]
            }

            if options:
                default_options.update(options)

            request_data = {
                "url_patterns": url_patterns,
                "options": default_options
            }

            if tab_id is not None:
                request_data["tab_id"] = tab_id

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "requests.start_monitoring",
                "data": request_data,
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return json.dumps({"error": f"Failed to start monitoring: {response['error']}"})

            if response.get("type") == "response" and "data" in response:
                self._remember_monitor_owner(response["data"].get("monitor_id"))
                return json.dumps(response["data"])
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return json.dumps({"error": f"Failed to start monitoring: {error_msg}"})

            return json.dumps({"error": "Unable to start monitoring"})

        @self._tool()
        async def requests_stop_monitoring(
            monitor_id: str,
            drain_timeout: int = 5
        ) -> str:
            """
            Stop monitoring web requests with graceful drainage

            Args:
                monitor_id: ID of the monitoring session to stop
                drain_timeout: Accepted and ignored; the monitor stops at once

            Returns:
                JSON string with stop status and statistics
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "requests.stop_monitoring",
                "data": {
                    "monitor_id": monitor_id,
                    "drain_timeout": drain_timeout
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return json.dumps({"error": f"Failed to stop monitoring: {response['error']}"})

            if response.get("type") == "response" and "data" in response:
                forget = getattr(self.websocket_server, 'forget_monitor', None)
                if forget:
                    forget(monitor_id)
                return json.dumps(response["data"])
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return json.dumps({"error": f"Failed to stop monitoring: {error_msg}"})

            return json.dumps({"error": "Unable to stop monitoring"})

        @self._tool()
        async def requests_list_captured(monitor_id: str) -> str:
            """
            List all captured request summaries from a monitoring session

            A request appears here once it has completed, so a page navigation
            started a moment ago will not be in the list yet.

            Args:
                monitor_id: ID of the monitoring session

            Returns:
                JSON string with captured request summaries
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "requests.list_captured",
                "data": {
                    "monitor_id": monitor_id
                },
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return json.dumps({"error": f"Failed to list captured requests: {response['error']}"})

            if response.get("type") == "response" and "data" in response:
                return json.dumps(response["data"])
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return json.dumps({"error": f"Failed to list captured requests: {error_msg}"})

            return json.dumps({"error": "Unable to list captured requests"})

        @self._tool()
        async def requests_get_content(
            monitor_id: str,
            request_id: str,
            include_binary: bool = False,
            save_request_body_to: Optional[str] = None,
            save_response_body_to: Optional[str] = None
        ) -> str:
            """
            Get full request/response content for a specific request

            The response carries **response_body.included** and **note**: a body that
            was not text, or not one of the monitored content types, comes back with
            `content: null` and its size filled in. `request_headers` is always empty.

            Args:
                monitor_id: ID of the monitoring session
                request_id: ID of the specific request
                include_binary: Accepted and ignored; binary bodies never come back
                save_request_body_to: Accepted and ignored; nothing is written to disk
                save_response_body_to: Accepted and ignored; nothing is written to disk

            Returns:
                JSON string with full request/response content
            """
            request_data = {
                "monitor_id": monitor_id,
                "request_id": request_id,
                "include_binary": include_binary
            }

            if save_request_body_to:
                request_data["save_request_body_to"] = save_request_body_to
            if save_response_body_to:
                request_data["save_response_body_to"] = save_response_body_to

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "requests.get_content",
                "data": request_data,
                "timestamp": datetime.now().isoformat()
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return json.dumps({"error": f"Failed to get request content: {response['error']}"})

            if response.get("type") == "response" and "data" in response:
                return json.dumps(response["data"])
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return json.dumps({"error": f"Failed to get request content: {error_msg}"})

            return json.dumps({"error": "Unable to get request content"})

    def get_mcp_app(self):
        """Get the FastMCP application instance"""
        return self.mcp