#!/usr/bin/env python3
"""
FoxMCP Server - WebSocket server that bridges browser extension with MCP clients

Copyright (c) 2024 FoxMCP Project
Licensed under the MIT License - see LICENSE file for details
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import sys
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional

import websockets
import uvicorn
try:
    from .mcp_tools import FoxMCPTools, AGENT_CALL
except ImportError:
    from mcp_tools import FoxMCPTools, AGENT_CALL

# Try to import port coordinator for dynamic port allocation
try:
    tests_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tests')
    sys.path.insert(0, tests_dir)
    from port_coordinator import get_port_by_type
    HAS_PORT_COORDINATOR = True
except ImportError:
    HAS_PORT_COORDINATOR = False

# Configure logging
#
# stderr is already basicConfig's default, but it is named here because with
# --stdio the process speaks JSON-RPC over stdout: a single log line written
# there breaks the framing and the client drops the connection. Anything in
# this process that has something to say says it on stderr.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Only browser extensions may open the extension WebSocket.
#
# WebSocket handshakes are exempt from the same-origin policy and are never
# preflighted, so without this any web page the user visits could connect to
# the localhost port and be accepted as the extension. Binding to localhost
# does not exclude web pages: they reach it from inside the user's own browser.
#
# The check is on the scheme rather than a whole origin because Firefox
# generates the extension's UUID per install, so no fixed value would work for
# more than one profile. A page's origin is always http:// or https://, which
# the pattern excludes, and page JavaScript cannot suppress or forge it.
#
# The trailing .+ is required: websockets matches this with fullmatch(), so a
# bare prefix pattern would reject every origin including the extension's.
# Largest message accepted from the extension.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
EXTENSION_ORIGIN_PATTERN = re.compile(r'moz-extension://.+')

def find_available_port(start_port=3000, max_attempts=100):
    """Find an available port starting from start_port"""
    if HAS_PORT_COORDINATOR:
        # Use the fixed MCP port if available
        try:
            return get_port_by_type('mcp')
        except Exception:
            pass  # Fall through to traditional approach
    else:
        # Fallback to traditional approach
        for i in range(max_attempts):
            port = start_port + i
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('localhost', port))
                    return port
            except OSError:
                continue

    raise RuntimeError(f"Could not find available port starting from {start_port}")

# How many ports after the configured one a server may move to when that one is
# taken. Each MCP client launches a server of its own under --stdio, so a second
# Claude session would otherwise find no port and no browser. The extension
# connects to every server on these ports, so each session gets its own.
EXTRA_SERVER_PORTS = 4

# A secret shared with each add-on, so neither side has to trust whoever holds
# the port: any local process can listen on 8765 first, or claim to be the
# add-on. Both prove they know it with an HMAC over the other's nonce, and the
# secret itself never goes over the socket. Every server on this machine reads
# the same file, so one pairing covers them all.
PAIRING_TOKEN_FILE = os.path.join(os.path.expanduser('~'), '.foxmcp', 'pairing-token')


def pairing_proof(token: str, role: str, nonce: str) -> str:
    """What `role` ('server' or 'client') sends to prove it holds the token"""
    return hmac.new(token.encode(), f"{role}|{nonce}".encode(), hashlib.sha256).hexdigest()


def ensure_pairing_token(path: str = PAIRING_TOKEN_FILE) -> str:
    """The token in `path`, created on first use, readable only by this user"""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(secrets.token_hex(16))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows: the profile folder is already private to the user
    with open(path) as f:
        return f.read().strip()


def port_is_free(port: int) -> bool:
    """Whether no other process is listening on the port, on IPv4 or IPv6 localhost

    On Windows the test socket asks for exclusive use: with SO_REUSEADDR, which
    the WebSocket server used to set, Windows lets a second process bind a port
    another one is listening on, and both servers start with no error.
    """
    for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                elif sys.platform.startswith('linux'):
                    # On Linux SO_REUSEADDR still refuses a port someone listens
                    # on, but not one left in TIME_WAIT by a server that just
                    # stopped. Without it a restarted server took the next port
                    # while the extension and the tests looked for this one.
                    # Not on macOS, where it would also allow a port another
                    # program listens on through the wildcard address.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, port))
        except OSError as e:
            if family == socket.AF_INET6 and getattr(e, 'errno', None) in (97, 10047, 99, 10049):
                continue  # no IPv6 on this machine
            return False
    return True


@dataclass
class BrowserConnection:
    """One connected browser: a Firefox or Thunderbird profile running the add-on"""
    key: str
    websocket: Any
    name: str
    version: str = ''
    # 'firefox', or 'thunderbird' for the Thunderbird add-on, which answers a
    # different set of actions
    app: str = 'firefox'
    # Whether the add-on proved it holds the pairing token, and the nonce it
    # was asked to sign
    paired: bool = False
    server_nonce: str = ''
    connected_at: float = field(default_factory=time.time)
    # Real input through WebDriver BiDi: whether the person allows it, the port
    # they set (0: find it), and the port it was last found on
    real_input: bool = False
    real_input_port: int = 0
    real_input_found: int = 0
    real_input_missed_at: float = 0.0
    # The tab the agent last named, which a screenshot shows by default
    last_tab: Optional[int] = None


class FoxMCPServer:
    # How long a browser that had no real input is left alone before looking again
    REAL_INPUT_RETRY_AFTER = 60.0

    # How often to check whether the MCP clients owning monitors are still there
    MONITOR_REAP_INTERVAL = 5.0

    # Owner recorded for a monitor started outside any MCP session, which is what
    # a tool called in-process rather than over a transport gets. There is no
    # client to outlive it, so it is never reaped - but it is still a monitor the
    # server knows about, and that is what keeps it from being stopped as a stray.
    NO_MCP_SESSION = "no-mcp-session"

    # A reply naming a monitor the server has no record of is not evidence of a
    # stray when the reply is what creates the record, or what confirms the
    # removal. Excluding the second is also what stops a stray from being stopped
    # over and over.
    MONITOR_LIFECYCLE_ACTIONS = ("requests.start_monitoring", "requests.stop_monitoring")

    def __init__(self, host: str = "localhost", port: int = 8765, mcp_port: int = None, start_mcp: bool = True,
                 disabled_tool_groups=None, enabled_tools=None, use_stdio: bool = False,
                 pairing_token: Optional[str] = None, real_input_only: bool = False):
        self.host = host
        self.port = port
        self.pairing_token = pairing_token
        self.use_stdio = use_stdio
        # Refuse the scripted fallback for clicks, keys, typing, hovers and drags
        self.real_input_only = real_input_only
        # Real input acts on whichever tab is showing, so one action at a time,
        # from activating its tab to the last event: another client activating
        # its tab mid-click would get this one's click. One lock rather than one
        # per browser, because requests follow the active browser, which
        # browsers_select can change mid-call.
        self.real_input_turn = asyncio.Lock()

        # In stdio mode the MCP side has no listener, so there is no port to pick.
        # Running the selection anyway would bind-test a port nothing serves and
        # report "port in use" against whatever else holds 3000 - most likely the
        # user's own HTTP-mode server, which is not a conflict at all here.
        if use_stdio:
            self.mcp_port = None
        # Set MCP port - default to 3000 for production, dynamic allocation only for tests
        elif mcp_port is None:
            # Check if we're in a test environment by checking for pytest or explicit test indicators
            in_test_env = ('pytest' in sys.modules or
                          'PYTEST_CURRENT_TEST' in os.environ or
                          any('pytest' in path or 'test_' in os.path.basename(path) for path in sys.path))
            if in_test_env and HAS_PORT_COORDINATOR:
                # Use dynamic port allocation for tests to avoid conflicts
                self.mcp_port = find_available_port(3000)
            else:
                # Use fixed port 3000 for production
                self.mcp_port = 3000
        else:
            # Check if requested port is available
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('localhost', mcp_port))
                    self.mcp_port = mcp_port
            except OSError:
                logger.warning(f"Requested MCP port {mcp_port} is in use, finding alternative...")
                self.mcp_port = find_available_port(mcp_port)

        if self.mcp_port is not None:
            logger.info(f"MCP server will use port {self.mcp_port}")
        self.start_mcp = start_mcp

        # Every connected browser, in the order they connected. Tools act on the
        # active one; browsers_select changes which. A browser arriving never
        # takes over from the active one: two profiles running the add-on used
        # to close each other's connection in turn, every few seconds, so tab
        # ids and monitors vanished between calls.
        self.browsers: Dict[str, BrowserConnection] = {}
        self.active_browser: Optional[str] = None
        # The active browser stays active while it is away, so its tab ids
        # never start meaning tabs in another profile: when Lost Tribe restarted
        # for an add-on update, tools moved to the main profile and a "tab 1"
        # navigation replaced the person's own home tab. Only browsers_select
        # moves the agent to a browser that is already connected; a browser no
        # one chose is replaced only by one arriving to an empty server.
        self.active_browser_name = ''
        self.browser_chosen = False
        self._browsers_seen = 0
        # Which browser each request monitor runs in, so calls naming the
        # monitor reach it even after another browser is selected.
        self.monitor_browsers: Dict[str, str] = {}
        self.pending_requests = {}  # Map of request IDs to Future objects

        # Connection event management
        self._connection_waiters = []  # List of futures waiting for connection

        # Initialize MCP tools
        self.mcp_tools = FoxMCPTools(self, disabled_groups=disabled_tool_groups,
                                     enabled_tools=enabled_tools)
        self.mcp_app = self.mcp_tools.get_mcp_app()
        self.mcp_server_task = None
        self.mcp_thread = None
        self.mcp_server_instance = None

        # Which MCP client asked for each running monitor, so its monitors can be
        # stopped when it goes. Monitors live in the extension, which outlives
        # every client, and only the client that started one knows its id.
        self.monitor_owners = {}  # monitor_id -> MCP session id
        self.monitors_being_stopped = set()
        # Which MCP client is working in which tab, so a client that goes can
        # have its tabs released: the extension keeps the "AI agent is working"
        # overlay up until then. Only matters in HTTP mode; under --stdio the
        # process ends with its client and the extension releases the tabs when
        # the connection drops.
        self.agent_tabs = {}  # tab id -> MCP session id
        self.mcp_http_app = None
        self.monitor_reaper_task = None
        self._shutdown_event = None
        self.websocket_server = None

    def log_rejected_handshake(self, connection, request, response):
        """Log handshakes the library refused, and let the refusal stand

        Registered as the `process_response` hook so that origin rejections are
        visible. The library rejects them with a 403 but logs only at debug
        level, which would make a legitimate extension that stopped connecting
        indistinguishable from a server that was never reached at all.

        Always returns None, meaning "use the response you already built" - this
        hook observes, it never decides.
        """
        if response.status_code == 403:
            origin = request.headers.get('Origin')
            logger.warning(
                f"Rejected WebSocket handshake from {origin!r} - only "
                f"moz-extension:// origins may connect as the extension"
            )
        return None

    def real_input_ports(self) -> List[int]:
        """Ports to try for the active browser's real input, best guess first

        Empty when the person turned real input off. A port they set is the
        only one tried; otherwise the one it was last found on comes first,
        and the caller probes the usual ones after it.
        """
        browser = self.browsers.get(self.active_browser)
        if not browser or not browser.real_input:
            return []
        if browser.real_input_port:
            return [browser.real_input_port]
        return [browser.real_input_found] if browser.real_input_found else []

    def real_input_found_on(self, port: int):
        """Remember where real input worked, or (port 0) that it was not found"""
        browser = self.browsers.get(self.active_browser)
        if browser:
            browser.real_input_found = port
            browser.real_input_missed_at = 0.0 if port else time.time()

    def real_input_enabled(self) -> bool:
        """Whether the person allows real input in this browser at all"""
        browser = self.browsers.get(self.active_browser)
        return bool(browser and browser.real_input)

    def real_input_allowed(self) -> bool:
        """Whether to try real input now: the person allows it, and it was not
        just looked for in vain (a profile started without remote control
        would otherwise search for its tab on every click)"""
        browser = self.browsers.get(self.active_browser)
        return bool(browser and browser.real_input
                    and time.time() - browser.real_input_missed_at > self.REAL_INPUT_RETRY_AFTER)

    @property
    def extension_connection(self):
        """The active browser's socket, or None when no browser is connected"""
        browser = self.browsers.get(self.active_browser)
        return browser.websocket if browser else None

    @extension_connection.setter
    def extension_connection(self, websocket):
        # Kept so code and tests that hand the server one socket still work: a
        # socket becomes a connected browser and is selected, None drops the
        # active browser.
        if websocket is None:
            if self.active_browser in self.browsers:
                self._remove_browser(self.browsers[self.active_browser].websocket)
            return
        self.active_browser = self._add_browser(websocket).key

    def _add_browser(self, websocket) -> BrowserConnection:
        """Record a new connection under a provisional key until it says hello"""
        self._browsers_seen += 1
        key = f"browser-{self._browsers_seen}"
        browser = BrowserConnection(key=key, websocket=websocket, name=f"Firefox {self._browsers_seen}")
        self.browsers[key] = browser
        return browser

    def _browser_for_socket(self, websocket) -> Optional[BrowserConnection]:
        for browser in self.browsers.values():
            if browser.websocket is websocket:
                return browser
        return None

    def _remove_browser(self, websocket):
        """Forget a closed connection and its monitors, and pick another active browser"""
        browser = self._browser_for_socket(websocket)
        if browser is None:
            return
        del self.browsers[browser.key]

        # The extension clears its own monitors the moment a connection ends,
        # so records naming them have nothing left to reach.
        for monitor_id, key in list(self.monitor_browsers.items()):
            if key == browser.key:
                self.monitor_browsers.pop(monitor_id, None)
                self.monitor_owners.pop(monitor_id, None)

        if self.active_browser == browser.key:
            self.active_browser_name = browser.name
            logger.warning(f"Active browser {browser.name} disconnected; tools wait for it or for browsers_select")

    def _may_take_over(self) -> bool:
        """Whether a browser arriving may become the active one"""
        if self.active_browser is None:
            return True
        return (self.active_browser not in self.browsers and not self.browser_chosen
                and len(self.browsers) == 1)

    def no_browser_reason(self) -> str:
        """Why there is no browser to act on, and what would fix it"""
        if self.active_browser is None or not self.active_browser_name:
            return "No extension connection available"
        others = ', '.join(f'"{b.name}"' for b in self.browsers.values())
        choice = f" Connected now: {others}; browsers_select one of them to act on it instead." if others else ""
        return (f'The selected browser "{self.active_browser_name}" is disconnected. Tools act on it again '
                f'when it reconnects.{choice}')

    def _handle_hello(self, websocket, data: Dict[str, Any]):
        """Name a connection and key it by the profile's id

        A profile reconnecting keeps its key, so a selection survives a dropped
        connection, and its old socket, if still open, is closed rather than left
        as a second entry for the same browser.
        """
        browser = self._browser_for_socket(websocket)
        if browser is None:
            return
        browser.version = str(data.get('version') or '')
        browser.real_input = bool(data.get('real_input'))
        browser.real_input_missed_at = 0.0  # the person may just have set it up
        try:
            browser.real_input_port = int(data.get('real_input_port') or 0)
        except (TypeError, ValueError):
            browser.real_input_port = 0
        browser.app = str(data.get('app') or 'firefox').lower()[:20]
        if data.get('name'):
            browser.name = str(data['name'])[:60]
        elif browser.app == 'thunderbird' and browser.name.startswith('Firefox '):
            browser.name = 'Thunderbird ' + browser.name[len('Firefox '):]

        instance_id = str(data.get('instance_id') or '')
        if not instance_id or instance_id == browser.key:
            return

        old = self.browsers.get(instance_id)
        was_active = self.active_browser in (browser.key, instance_id)
        if old is not None and old.websocket is not websocket:
            del self.browsers[instance_id]
            asyncio.create_task(old.websocket.close())

        self.browsers = {(instance_id if k == browser.key else k): v for k, v in self.browsers.items()}
        for monitor_id, key in self.monitor_browsers.items():
            if key == browser.key:
                self.monitor_browsers[monitor_id] = instance_id
        browser.key = instance_id
        if was_active or (self.active_browser not in self.browsers and self._may_take_over()):
            self.active_browser = instance_id
        if self.active_browser == instance_id:
            self.active_browser_name = browser.name
        logger.info(f"Browser {browser.name} ({instance_id[:8]}) said hello, add-on {browser.version}")

    def _answer_pairing(self, websocket, data: Dict[str, Any]):
        """Prove the token to an add-on that sent a nonce, and ask it to prove it back

        An add-on that sends no nonce does not pair (a Firefox add-on without a
        token set) and is left as it was. Each hello starts over, unpaired.
        """
        nonce = str(data.get('client_nonce') or '')
        browser = self._browser_for_socket(websocket)
        if not nonce or browser is None:
            return
        browser.paired = False
        ack = {'proof': None}
        if self.pairing_token:
            browser.server_nonce = secrets.token_hex(16)
            ack = {'server_nonce': browser.server_nonce,
                   'proof': pairing_proof(self.pairing_token, 'server', nonce)}
        asyncio.create_task(websocket.send(json.dumps({
            'id': f'hello_ack_{int(time.time() * 1000)}', 'type': 'hello_ack', 'action': 'browser.hello_ack',
            'data': ack, 'timestamp': datetime.now().isoformat()})))

    def _handle_auth(self, websocket, data: Dict[str, Any]):
        """Mark a browser paired when its proof matches the nonce it was sent"""
        browser = self._browser_for_socket(websocket)
        if browser is None or not self.pairing_token or not browser.server_nonce:
            return
        expected = pairing_proof(self.pairing_token, 'client', browser.server_nonce)
        browser.paired = hmac.compare_digest(str(data.get('proof') or ''), expected)
        browser.server_nonce = ''  # one use
        logger.info(f"Browser {browser.name} {'paired' if browser.paired else 'failed to pair: wrong token'}")

    def select_browser(self, query: str) -> BrowserConnection:
        """Make a browser active by its name or the start of its id"""
        wanted = query.strip().lower()
        matches = [b for b in self.browsers.values() if b.name.lower() == wanted]
        if not matches:
            matches = [b for b in self.browsers.values() if wanted and b.key.lower().startswith(wanted)]
        if len(matches) != 1:
            names = ', '.join(f'"{b.name}"' for b in self.browsers.values()) or 'none'
            problem = 'matches more than one browser' if matches else 'matches no browser'
            raise ValueError(f'"{query}" {problem}. Connected: {names}')
        self.active_browser = matches[0].key
        self.active_browser_name = matches[0].name
        self.browser_chosen = True
        return matches[0]

    async def handle_extension_connection(self, websocket):
        """Handle a WebSocket connection from a browser running the add-on

        Several browsers may be connected at once. The first becomes active;
        later ones wait until selected with browsers_select.
        """
        logger.info(f"Extension connected from {websocket.remote_address}")

        browser = self._add_browser(websocket)
        if self._may_take_over():
            self.active_browser = browser.key
            self.active_browser_name = browser.name

        # Notify all waiters that a connection has been established
        self._notify_connection_waiters()

        try:
            async for message in websocket:
                await self.handle_extension_message(message, websocket)
        except ConnectionAbortedError:
            logger.info("Extension disconnected")
        except Exception as e:
            logger.error(f"Error handling extension connection: {e}")
        finally:
            self._remove_browser(websocket)

    async def handle_extension_message(self, message: str, websocket=None):
        """Process message from browser extension"""
        try:
            data = json.loads(message)
            message_type = data.get('type', 'unknown')
            if message_type == 'hello':
                self._handle_hello(websocket, data.get('data') or {})
                self._answer_pairing(websocket, data.get('data') or {})
                return
            if message_type == 'auth':
                self._handle_auth(websocket, data.get('data') or {})
                return
            message_id = data.get('id')
            action = data.get('action', 'unknown')

            # Handle debug logs specially - print them prominently
            if message_type == 'debug_log':
                level = data.get('data', {}).get('level', 'log')
                log_message = data.get('data', {}).get('message', '')
                timestamp = data.get('data', {}).get('timestamp', '')

                logger.info("----- EXTENSION DEBUG LOG -----")
                if level == 'error':
                    logger.info(f"🔴 EXTENSION ERROR [{timestamp}]: {log_message}")
                else:
                    logger.info(f"🔵 EXTENSION LOG [{timestamp}]: {log_message}")
                return

            logger.info(f"Received from extension: {message_type} - {action} (ID: {message_id})")

            # The extension answered for a monitor the server has no record of,
            # so nothing can reach it: stop it rather than leave it capturing.
            #
            # The two sides can only drift this way if one of them let go of a
            # monitor the other kept - an extension too old to clear its monitors
            # when a connection ends, or a server that has forgotten an owner. In
            # a matched pair this never fires, which is why it is worth having: it
            # is the case nobody planned for.
            stray_monitor = self._stray_monitor_named_in(action, data)
            if stray_monitor:
                asyncio.create_task(self._stop_stray_monitor(stray_monitor))

            if message_type == 'request':
                # Handle ping-pong for connection testing
                if action == 'ping':
                    await self.handle_ping_request(data)
                    return

            elif message_type in ['response', 'error']:
                # Handle response/error from extension
                await self.handle_extension_response(data)
                return

            # For other message types, just log for now
            logger.warning(f"Unhandled message type: {message_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {message}")
        except Exception as e:
            logger.error(f"Error processing extension message: {e}")

    async def handle_extension_response(self, response_data: Dict[str, Any]):
        """Handle response or error from browser extension"""
        request_id = response_data.get('id')
        if not request_id:
            logger.warning("Received response without ID")
            return

        if request_id in self.pending_requests:
            future = self.pending_requests.pop(request_id)
            if not future.cancelled():
                future.set_result(response_data)
                logger.info(f"Completed pending request: {request_id}")
        else:
            logger.warning(f"Received response for unknown request: {request_id}")

    async def handle_ping_request(self, request: Dict[str, Any]):
        """Handle ping request from extension"""
        response = {
            "id": request["id"],
            "type": "request",
            "action": "ping",
            "data": {"test": True},
            "timestamp": datetime.now().isoformat()
        }

        success = await self.send_to_extension(response)
        if success:
            logger.info(f"Sent ping request to extension: {request['id']}")
        else:
            logger.error(f"Failed to send ping request: {request['id']}")


    async def test_ping_extension(self) -> Dict[str, Any]:
        """Send ping to extension and wait for response"""
        if not self.extension_connection:
            return {"success": False, "error": "No extension connection"}

        test_id = f"server_ping_{int(datetime.now().timestamp() * 1000)}"
        ping_request = {
            "id": test_id,
            "type": "request",
            "action": "ping",
            "data": {"server_test": True},
            "timestamp": datetime.now().isoformat()
        }

        # Send ping and return immediately (for now)
        success = await self.send_to_extension(ping_request)
        if success:
            return {"success": True, "message": "Ping sent to extension", "id": test_id}
        else:
            return {"success": False, "error": "Failed to send ping"}

    def _socket_for(self, message: Dict[str, Any]):
        """The browser a message goes to: a monitor's own browser, else the active one"""
        data = message.get('data')
        monitor_id = data.get('monitor_id') if isinstance(data, dict) else None
        key = self.monitor_browsers.get(monitor_id) if monitor_id else None
        if key in self.browsers:
            return self.browsers[key].websocket
        return self.extension_connection

    async def send_to_extension(self, message: Dict[str, Any]) -> bool:
        """Send message to browser extension"""
        target = self._socket_for(message)
        if not target:
            logger.warning("No extension connection available")
            return False

        try:
            message['timestamp'] = datetime.now().isoformat()
            await target.send(json.dumps(message))
            return True
        except Exception as e:
            logger.error(f"Error sending to extension: {e}")
            return False

    async def send_request_and_wait(self, request: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Send request to extension and wait for response"""
        request_id = request.get('id')
        if not request_id:
            raise ValueError("Request must have an ID")

        if not self.extension_connection:
            return {"error": self.no_browser_reason()}

        # The tool this request comes from, and the status line its caller gave,
        # so the extension can show on the page what the agent is doing
        agent_call = AGENT_CALL.get()
        if agent_call and 'agent' not in request:
            request = {**request, 'agent': agent_call}

        # Create future for response
        response_future = asyncio.Future()
        self.pending_requests[request_id] = response_future

        try:
            # Send the request
            success = await self.send_to_extension(request)
            if not success:
                self.pending_requests.pop(request_id, None)
                return {"error": "Failed to send request to extension"}

            # Wait for response with timeout
            response = await asyncio.wait_for(response_future, timeout=timeout)
            # The extension puts its reason in data.message; most tools only
            # look for "error", and without it said "Unable to create tab"
            # where the extension had said why (a blocked site, a pause).
            if response.get("type") == "error" and "error" not in response:
                response = {**response, "error": (response.get("data") or {}).get("message") or "The extension refused the request"}
            return response

        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            return {"error": f"Request timed out after {timeout} seconds"}
        except Exception as e:
            self.pending_requests.pop(request_id, None)
            return {"error": f"Request failed: {str(e)}"}

    def register_monitor(self, monitor_id: str, session_id: str):
        """Record the MCP client a newly started request monitor belongs to

        Called by the requests_start_monitoring tool once the extension has
        answered with an id. Monitors with no owner recorded are never reaped;
        that is what happens when a tool is called outside an MCP session, as the
        test harness does.
        """
        if monitor_id:
            self.monitor_owners[monitor_id] = session_id or self.NO_MCP_SESSION
            if self.active_browser:
                self.monitor_browsers[monitor_id] = self.active_browser

    def forget_monitor(self, monitor_id: str):
        """Drop a monitor the client stopped itself, so the reaper ignores it"""
        self.monitor_owners.pop(monitor_id, None)
        self.monitor_browsers.pop(monitor_id, None)

    def _live_mcp_session_ids(self) -> Optional[set]:
        """Sessions of the MCP clients still connected, or None if that cannot be told

        None and the empty set mean different things: None is "no answer
        available" and must not be read as "every client has gone".

        `_server_instances` is private to the MCP SDK's
        StreamableHTTPSessionManager, and fastmcp reaches into it the same way in
        its own shutdown path. A terminated session stays in that dict rather than
        being removed, so `is_terminated` is what separates a client that has gone
        from one that is merely quiet.
        """
        app = self.mcp_http_app
        if app is None:
            return None

        for route in getattr(app, 'routes', []):
            manager = getattr(getattr(route, 'app', None), 'session_manager', None)
            transports = getattr(manager, '_server_instances', None)
            if transports is not None:
                return {
                    session_id
                    for session_id, transport in list(transports.items())
                    if not getattr(transport, 'is_terminated', False)
                }

        return None

    def note_agent_tab(self, session_id: Optional[str], tab_id: Any):
        """Record that an MCP client is acting on a tab"""
        try:
            self.agent_tabs[int(tab_id)] = session_id or self.NO_MCP_SESSION
        except (TypeError, ValueError):
            return
        browser = self.browsers.get(self.active_browser)
        if browser:
            browser.last_tab = int(tab_id)

    def last_agent_tab(self) -> Optional[int]:
        """The tab the agent last acted on in the active browser"""
        browser = self.browsers.get(self.active_browser)
        return browser.last_tab if browser else None

    def forget_agent_tab(self, tab_id: Any):
        """Drop a tab its client released itself"""
        try:
            self.agent_tabs.pop(int(tab_id), None)
        except (TypeError, ValueError):
            pass

    async def release_tabs_of_gone_clients(self):
        """Release the tabs of every MCP client that has disconnected

        Without this a client that ends without calling tabs_release would
        leave its overlay up until the extension's idle timeout.
        """
        if not self.agent_tabs:
            return
        live_sessions = self._live_mcp_session_ids()
        if live_sessions is None:
            return
        gone = [tab_id for tab_id, session_id in self.agent_tabs.items()
                if session_id != self.NO_MCP_SESSION and session_id not in live_sessions]
        for tab_id in gone:
            self.agent_tabs.pop(tab_id, None)
            if not self.extension_connection:
                continue
            response = await self.send_request_and_wait({
                "id": f"release_{tab_id}_{time.time()}",
                "type": "request",
                "action": "tabs.release",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat()
            }, timeout=10.0)
            if "error" in response or response.get("type") == "error":
                logger.info(f"Could not release tab {tab_id} of a departed MCP client: {response}")

    async def stop_monitors_of_gone_clients(self):
        """Stop every running monitor whose MCP client has disconnected

        A monitor outlives the client that started it: it lives in the extension,
        and only its creator ever knew its id. Left alone it keeps the extension's
        webRequest listeners registered and filters response bodies nobody can
        read.

        A monitor whose extension has gone is forgotten here rather than stopped:
        the extension clears its own monitors whenever a connection ends, so there
        is nothing left to stop.
        """
        if not self.monitor_owners:
            return

        live_sessions = self._live_mcp_session_ids()
        if live_sessions is None:
            return

        abandoned = [monitor_id for monitor_id, session_id in self.monitor_owners.items()
                     if session_id != self.NO_MCP_SESSION and session_id not in live_sessions]

        for monitor_id in abandoned:
            self.monitor_owners.pop(monitor_id, None)

            if not self.extension_connection:
                continue

            request = {
                "id": f"reap_{monitor_id}",
                "type": "request",
                "action": "requests.stop_monitoring",
                "data": {"monitor_id": monitor_id},
                "timestamp": datetime.now().isoformat()
            }
            response = await self.send_request_and_wait(request, timeout=10.0)
            if "error" in response or response.get("type") == "error":
                detail = response.get("error") or response.get("data", {}).get("message", response)
                logger.warning(f"Could not stop monitor {monitor_id} of a departed MCP client: {detail}")
            else:
                logger.info(f"Stopped monitor {monitor_id}: the MCP client that started it has gone")

    def _stray_monitor_named_in(self, action: str, message: Dict[str, Any]) -> Optional[str]:
        """The monitor id in a message from the extension that no client owns, if any

        Returns None for the monitor lifecycle replies, which name ids the registry
        is not expected to hold yet or any more.
        """
        if action in self.MONITOR_LIFECYCLE_ACTIONS:
            return None

        payload = message.get('data')
        if not isinstance(payload, dict):
            return None

        monitor_id = payload.get('monitor_id')
        if not monitor_id or monitor_id in self.monitor_owners:
            return None

        return monitor_id

    async def _stop_stray_monitor(self, monitor_id: str):
        """Stop a monitor the extension is running that no client can reach

        Runs as its own task so the message being handled is not held up waiting
        for the extension to answer this.
        """
        if monitor_id in self.monitors_being_stopped:
            return

        self.monitors_being_stopped.add(monitor_id)
        try:
            request = {
                "id": f"stray_{monitor_id}",
                "type": "request",
                "action": "requests.stop_monitoring",
                "data": {"monitor_id": monitor_id},
                "timestamp": datetime.now().isoformat()
            }
            response = await self.send_request_and_wait(request, timeout=10.0)
            if "error" in response or response.get("type") == "error":
                detail = response.get("error") or response.get("data", {}).get("message", response)
                logger.warning(f"Could not stop stray monitor {monitor_id}: {detail}")
            else:
                logger.info(f"Stopped stray monitor {monitor_id}: no MCP client owns it")
        finally:
            self.monitors_being_stopped.discard(monitor_id)

    async def _reap_monitors(self):
        """Poll for departed MCP clients for as long as the server runs

        Polling rather than a callback because the MCP SDK offers no hook for a
        session ending. Only started in HTTP mode: under --stdio the server has
        the one client, and when it goes the process goes with it, which drops the
        WebSocket and lets the extension clear its own monitors.
        """
        while True:
            await asyncio.sleep(self.MONITOR_REAP_INTERVAL)
            try:
                await self.stop_monitors_of_gone_clients()
                await self.release_tabs_of_gone_clients()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Error reaping monitors of departed clients: {e}")

    # Test Helper Methods
    async def get_popup_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get current popup display state from extension"""
        request = {
            "id": f"test_popup_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_popup_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def get_options_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get current options page display state from extension"""
        request = {
            "id": f"test_options_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_options_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def get_storage_values(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Get raw storage.sync values from extension"""
        request = {
            "id": f"test_storage_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.get_storage_values",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def validate_ui_sync(self, expected_values: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Validate UI-storage synchronization with expected values"""
        request = {
            "id": f"test_validate_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.validate_ui_sync",
            "data": {"expectedValues": expected_values},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def refresh_ui_state(self, timeout: float = 30.0) -> Dict[str, Any]:
        """Trigger UI state refresh in extension"""
        request = {
            "id": f"test_refresh_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.refresh_ui_state",
            "data": {},
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def visit_url_for_test(self, url: str, wait_time: float = 6.0, timeout: float = 20.0) -> Dict[str, Any]:
        """Visit a URL to create browser history entry for testing"""
        request = {
            "id": f"test_visit_url_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.visit_url",
            "data": {
                "url": url,
                "waitTime": int(wait_time * 1000)  # Convert to milliseconds
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def visit_multiple_urls_for_test(self, urls: list, wait_time: float = 6.0, delay_between: float = 2.0, timeout: float = 90.0) -> Dict[str, Any]:
        """Visit multiple URLs to create browser history entries for testing"""
        request = {
            "id": f"test_visit_multiple_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.visit_multiple_urls",
            "data": {
                "urls": urls,
                "waitTime": int(wait_time * 1000),  # Convert to milliseconds
                "delayBetween": int(delay_between * 1000)  # Convert to milliseconds
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def clear_test_history(self, urls: list = None, clear_all: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
        """Clear test history entries for cleanup"""
        request = {
            "id": f"test_clear_history_{datetime.now().isoformat()}",
            "type": "request",
            "action": "test.clear_test_history",
            "data": {
                "urls": urls or [],
                "clearAll": clear_all
            },
            "timestamp": datetime.now().isoformat()
        }
        response = await self.send_request_and_wait(request, timeout)
        return response.get('data', response) if isinstance(response, dict) and 'data' in response else response

    async def test_storage_sync_workflow(self, test_values: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Complete test workflow: set values, validate sync, return results"""
        results = {
            "workflow_success": False,
            "steps": {},
            "errors": []
        }

        try:
            # Step 1: Get initial state
            initial_storage = await self.get_storage_values(timeout)
            if "error" in initial_storage:
                results["errors"].append(f"Failed to get initial storage: {initial_storage['error']}")
                return results
            results["steps"]["initial_storage"] = initial_storage

            # Step 2: Get popup state
            popup_state = await self.get_popup_state(timeout)
            if "error" in popup_state:
                results["errors"].append(f"Failed to get popup state: {popup_state['error']}")
                return results
            results["steps"]["popup_state"] = popup_state

            # Step 3: Get options state
            options_state = await self.get_options_state(timeout)
            if "error" in options_state:
                results["errors"].append(f"Failed to get options state: {options_state['error']}")
                return results
            results["steps"]["options_state"] = options_state

            # Step 4: Validate synchronization
            validation_result = await self.validate_ui_sync(test_values, timeout)
            if "error" in validation_result:
                results["errors"].append(f"Failed to validate UI sync: {validation_result['error']}")
                return results
            results["steps"]["validation"] = validation_result

            # Check if validation passed
            if validation_result.get("popupSyncValid") and validation_result.get("optionsSyncValid") and validation_result.get("storageMatches"):
                results["workflow_success"] = True
            else:
                results["errors"].extend(validation_result.get("issues", []))

            return results

        except Exception as e:
            results["errors"].append(f"Workflow exception: {str(e)}")
            return results

    def _notify_connection_waiters(self):
        """Notify all futures waiting for a connection"""
        for future in self._connection_waiters:
            if not future.cancelled():
                future.set_result(True)
        self._connection_waiters.clear()

    async def wait_for_extension_connection(self, timeout: float = 30.0) -> bool:
        """
        Wait for an extension connection to be established.

        Args:
            timeout: Maximum time to wait for connection in seconds

        Returns:
            bool: True if connection was established, False if timeout occurred

        Example:
            # Wait for Firefox extension to connect
            connected = await server.wait_for_extension_connection(timeout=10.0)
            if connected:
                print("Extension connected!")
            else:
                print("Connection timeout")
        """
        # If already connected, return immediately
        if self.extension_connection and self.extension_connection.close_code is None:
            return True

        # Create a future to wait for connection
        connection_future = asyncio.Future()
        self._connection_waiters.append(connection_future)

        try:
            # Wait for connection with timeout
            await asyncio.wait_for(connection_future, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            # Remove the future from waiters if it timed out
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False
        except Exception:
            # Remove the future from waiters on any other error
            if connection_future in self._connection_waiters:
                self._connection_waiters.remove(connection_future)
            return False

    async def start_mcp_server(self):
        """Start the MCP server in a separate thread"""
        import threading

        # Create shutdown event
        self._shutdown_event = threading.Event()

        # Created here rather than in the thread, so a shutdown that comes
        # before the thread gets going still finds the server to stop. One that
        # found nothing let the thread start uvicorn afterwards, which then held
        # the port for every later server in the process.
        self.mcp_http_app = self.mcp_app.http_app()
        config = uvicorn.Config(
            self.mcp_http_app,
            host=self.host,
            port=self.mcp_port,
            log_level="error"  # Reduce log noise during tests
        )
        self.mcp_server_instance = uvicorn.Server(config)

        def run_mcp_server():
            try:
                logger.info(f"Starting MCP server on {self.host}:{self.mcp_port}")
                self.mcp_server_instance.run()

            except Exception as e:
                logger.warning(f"MCP server failed to start on {self.host}:{self.mcp_port}: {e}")
                # Don't crash the whole server if MCP fails - this is important for tests

        # Run MCP server in separate thread
        self.mcp_thread = threading.Thread(target=run_mcp_server, daemon=True)
        self.mcp_thread.start()
        logger.info(f"MCP server thread started for {self.host}:{self.mcp_port}")

        # Give MCP server time to start (reduced time for faster tests)
        await asyncio.sleep(0.5)

    def _stop_mcp_server(self):
        """Stop the MCP server gracefully"""
        if self.mcp_server_instance:
            try:
                logger.info("Stopping MCP server...")

                # Signal server to shutdown
                self.mcp_server_instance.should_exit = True

                # Wait for thread to finish with timeout
                if self.mcp_thread and self.mcp_thread.is_alive():
                    self.mcp_thread.join(timeout=5.0)

                    if self.mcp_thread.is_alive():
                        logger.warning("MCP server thread did not stop gracefully within timeout")
                    else:
                        logger.info("MCP server stopped gracefully")

                # Clean up references
                self.mcp_server_instance = None
                self.mcp_thread = None

            except Exception as e:
                logger.warning(f"Error stopping MCP server: {e}")

    async def _stop_websocket_server(self):
        """
        Stop the WebSocket server and wait for its listening socket to be released.

        Must be awaited: both the connection close and the server shutdown are
        coroutines, and returning before they finish leaves the port bound. The
        next server to bind that port then fails, which in the test suite shows
        up as a client connecting to the old listener and timing out on the
        handshake rather than as an obvious "address already in use".
        """
        if self.websocket_server:
            try:
                logger.info("Stopping WebSocket server...")

                # Close all existing connections first
                for browser in list(self.browsers.values()):
                    try:
                        await browser.websocket.close()
                        logger.info(f"Connection to {browser.name} closed")
                    except Exception as e:
                        logger.warning(f"Error closing extension connection: {e}")
                self.browsers.clear()
                self.active_browser = None
                self.active_browser_name = ''
                self.browser_chosen = False

                # close() only starts the shutdown; wait_closed() is what
                # actually releases the port. Bounded so a connection that
                # refuses to close cannot hang the caller forever.
                self.websocket_server.close()
                try:
                    await asyncio.wait_for(self.websocket_server.wait_closed(), timeout=5.0)
                    logger.info("WebSocket server stopped gracefully")
                except asyncio.TimeoutError:
                    logger.warning("WebSocket server did not release its port within timeout")

                # Clean up reference
                self.websocket_server = None

            except Exception as e:
                logger.warning(f"Error stopping WebSocket server: {e}")

    async def _stop(self):
        """Stop all servers (WebSocket and MCP)"""
        logger.info("Stopping FoxMCP server...")

        if self.monitor_reaper_task:
            self.monitor_reaper_task.cancel()
            try:
                await self.monitor_reaper_task
            except asyncio.CancelledError:
                pass
            self.monitor_reaper_task = None

        # Stop MCP server
        self._stop_mcp_server()

        # Stop WebSocket server
        await self._stop_websocket_server()

        logger.info("FoxMCP server stopped")

    async def shutdown(self, server_task):
        """
        Gracefully shutdown the server and its task.

        Args:
            server_task: asyncio.Task running the server
        """
        try:
            # Cancel the task first, so a server still starting cannot start
            # anything more after its resources were stopped
            server_task.cancel()

            # Wait for task to finish, handling CancelledError
            try:
                await server_task
            except asyncio.CancelledError:
                pass
            finally:
                # Also when the task had already failed: its MCP port is still held
                await self._stop()

        except Exception as e:
            logger.warning(f"Error during server shutdown: {e}")

    async def start_server(self):
        """Start both WebSocket and MCP servers"""
        logger.info(f"Starting FoxMCP server on {self.host}:{self.port}")

        # Start MCP server first (if enabled)
        #
        # stdio is the exception, and starts below instead: HTTP mode hands
        # uvicorn a thread and returns, while the stdio transport runs in this
        # loop until the client hangs up. Awaiting it here would mean never
        # reaching the WebSocket server it depends on.
        if self.start_mcp and not self.use_stdio:
            await self.start_mcp_server()
            logger.info(f"MCP tools available at http://{self.host}:{self.mcp_port}/")
        elif not self.start_mcp:
            logger.info("MCP server disabled for this instance")

        # Another server already serving a browser keeps its port; this one takes
        # the next free one, which the extension also connects to.
        if not port_is_free(self.port):
            for candidate in range(self.port + 1, self.port + 1 + EXTRA_SERVER_PORTS):
                if port_is_free(candidate):
                    logger.info(f"Port {self.port} is taken by another FoxMCP server; using {candidate}")
                    self.port = candidate
                    break

        try:
            self.websocket_server = await websockets.serve(
                self.handle_extension_connection,
                self.host,
                self.port,
                # SO_REUSEADDR lets a restarted server rebind at once on Linux and
                # macOS. On Windows it means something else - a second process
                # may bind a port that is in use - so it stays off there.
                reuse_address=os.name != 'nt',
                # A full-window screenshot comes back as one base64 message of
                # several MB; the library's 1 MiB default drops the connection.
                max_size=MAX_MESSAGE_BYTES,
                # Rejected during the handshake, so a non-extension client never
                # reaches handle_extension_connection - which would otherwise close
                # the real extension's socket to make room for it.
                origins=[EXTENSION_ORIGIN_PATTERN],
                process_response=self.log_rejected_handshake
            )
        except OSError as e:
            # A taken port normally moves the server to one of the next
            # EXTRA_SERVER_PORTS before it gets here, so this means all of them
            # are in use (one server per Claude session), or the port was taken
            # in between. The bare errno says neither.
            logger.error(
                f"Cannot listen on {self.host}:{self.port} for the extension: {e}. "
                f"The add-on connects to this port and the {EXTRA_SERVER_PORTS} after it; "
                "if every one is held by another session's server, close one of those sessions."
            )
            raise

        logger.info("FoxMCP WebSocket server is running...")

        if self.start_mcp and not self.use_stdio:
            self.monitor_reaper_task = asyncio.create_task(self._reap_monitors())

        if self.start_mcp and self.use_stdio:
            await self._serve_mcp_over_stdio()
        else:
            await self.websocket_server.wait_closed()

    async def _serve_mcp_over_stdio(self):
        """Serve MCP on stdin/stdout until the client hangs up, then release the port

        Only reached with --stdio, and only after the WebSocket server is
        listening. Returns when either side finishes: the client closing stdin
        is the normal end, and a WebSocket server that stops is a reason to stop
        answering tool calls it can no longer carry out.

        Unlike HTTP mode, the MCP server runs as a task in this loop rather than
        in a uvicorn thread, because the stdio transport is asyncio all the way
        down and has no thread of its own to run in.
        """
        logger.info("MCP serving over stdio; no HTTP listener")

        # show_banner=False keeps startup off the network as well as quiet: the
        # banner asks PyPI whether a newer fastmcp exists, which is a delay a
        # client is waiting on and a request the user did not ask for.
        mcp_task = asyncio.create_task(
            self.mcp_app.run_async(transport="stdio", show_banner=False),
            name="foxmcp-stdio"
        )
        websocket_task = asyncio.create_task(
            self.websocket_server.wait_closed(),
            name="foxmcp-websocket"
        )

        try:
            done, _ = await asyncio.wait(
                {mcp_task, websocket_task},
                return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (mcp_task, websocket_task):
                task.cancel()

            # Awaited, not just cancelled: _stop_websocket_server() below waits
            # for the port to be released, and a live stdio task still holding
            # the connection open would make that wait pointless.
            await asyncio.gather(mcp_task, websocket_task, return_exceptions=True)
            await self._stop()

        # A tool that raised inside the transport is the client's problem to see,
        # so it is re-raised rather than logged as a clean shutdown.
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()

async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='FoxMCP Server - WebSocket server for browser extension')
    parser.add_argument('--host', default='localhost',
                        help='Host to bind to (default: localhost)')
    parser.add_argument('--port', type=int, default=8765,
                        help='WebSocket port (default: 8765)')
    parser.add_argument('--mcp-port', type=int, default=None,
                        help='MCP server port (default: 3000, dynamic allocation in tests)')
    parser.add_argument('--no-mcp', action='store_true',
                        help='Disable MCP server')
    parser.add_argument('--stdio', action='store_true',
                        help='Serve MCP over stdin/stdout instead of HTTP, so an MCP '
                             'client can launch the server itself. The WebSocket port '
                             'for the extension is unchanged.')
    parser.add_argument('--disable-tools', default=None, metavar='GROUP[,GROUP...]',
                        help='Comma-separated tool groups to leave unregistered, so their '
                             'descriptions never reach the client. Groups: '
                             f"{', '.join(sorted(FoxMCPTools.TOOL_GROUPS))}. "
                             'Overrides FOXMCP_DISABLE_TOOLS.')
    parser.add_argument('--enable-tools', default=None, metavar='TOOL[,TOOL...]',
                        help='Comma-separated individual tools to register even though '
                             'their group is disabled, named as the client sees them '
                             '(e.g. tabs_capture_screenshot). Overrides FOXMCP_ENABLE_TOOLS.')
    parser.add_argument('--real-input-only', action='store_true',
                        help='Never fall back to events dispatched by script: a click, key, '
                             'typing, hover or drag that cannot be done with real input '
                             'fails and says why. Sites such as Facebook ignore scripted '
                             'events while showing their effect, so the fallback can report '
                             'success for an action that did nothing. Also '
                             'FOXMCP_REAL_INPUT_ONLY=1.')

    args = parser.parse_args()

    # Both of these are silent no-ops rather than errors if left to run, and a
    # user who typed them meant something by them. --no-mcp with --stdio asks for
    # a server with nothing on its stdout, which no client can use; --mcp-port
    # names a listener stdio mode does not open.
    if args.stdio and args.no_mcp:
        parser.error('--stdio and --no-mcp cannot be combined: stdio mode exists to '
                     'serve MCP, and --no-mcp turns it off')
    if args.stdio and args.mcp_port is not None:
        parser.error('--mcp-port has no meaning with --stdio: stdio mode serves MCP on '
                     'stdin/stdout and opens no HTTP listener')

    # FOXMCP_DISABLE_TOOLS is the same setting for a client that launches the server
    # through a wrapper: an MCP client config names the command and sets the
    # environment, but the arguments in between are often not the user's to edit.
    group_list = args.disable_tools
    if group_list is None:
        group_list = os.environ.get('FOXMCP_DISABLE_TOOLS', '')
    disabled_tool_groups = [g.strip() for g in group_list.split(',') if g.strip()]

    tool_list = args.enable_tools
    if tool_list is None:
        tool_list = os.environ.get('FOXMCP_ENABLE_TOOLS', '')
    enabled_tools = [t.strip() for t in tool_list.split(',') if t.strip()]

    # FoxMCPTools validates these again in its constructor, which is what enforces
    # the rule; checking here is what turns the ValueError into argparse's usage
    # message, so a mistyped group name reads as a command-line error rather than a
    # traceback from server startup.
    try:
        FoxMCPTools._validate_groups(disabled_tool_groups)
    except ValueError as e:
        parser.error(str(e))

    # Ensure localhost-only binding for security
    if args.host != 'localhost' and args.host != '127.0.0.1':
        logger.warning(f"Host '{args.host}' changed to 'localhost' for security")
        args.host = 'localhost'

    # --enable-tools is checked here rather than beside --disable-tools above,
    # because the tool names only exist once the tool definitions have run, which
    # happens inside this constructor. Turning its ValueError into argparse's
    # usage message keeps a mistyped tool name reading as a command-line error
    # rather than a traceback from server startup, the same as a mistyped group.
    try:
        server = FoxMCPServer(
            host=args.host,
            port=args.port,
            mcp_port=args.mcp_port,
            start_mcp=not args.no_mcp,
            disabled_tool_groups=disabled_tool_groups,
            enabled_tools=enabled_tools,
            use_stdio=args.stdio,
            pairing_token=ensure_pairing_token(),
            real_input_only=args.real_input_only
                or os.environ.get('FOXMCP_REAL_INPUT_ONLY', '').strip().lower() in ('1', 'true', 'yes')
        )
        logger.info(f"Pairing token for the add-ons' options: {PAIRING_TOKEN_FILE}")
    except ValueError as e:
        parser.error(str(e))
    await server.start_server()

def run():
    """Command-line entry point (the mcp-bridge-for-firefox command)"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        # A server that could not start is a failure, and exiting 0 told every
        # caller otherwise. It matters most under --stdio: the client that
        # launched the process reports what it exited with, and often shows
        # nothing else.
        sys.exit(1)


if __name__ == "__main__":
    run()
