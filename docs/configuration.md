# Server Configuration

How to set up, run and configure the MCP Bridge for Firefox server and add-on.

## Agent profile

Firefox has one active window per process, so while you use a Firefox window, real keys for another window of the same Firefox have to wait for you to pause and briefly borrow its activation. While it borrows, about 0.4 s for a short text, the agent's window holds the Windows foreground from underneath yours: your title bar dims and your page sees a blur and a focus. A second profile runs as its own process and has neither problem.

1. Create it: `firefox.exe -CreateProfile Agents`
2. Start it alongside your own: `firefox.exe -P Agents --no-remote`
3. Install the add-on there, and in its settings tick **This Firefox is for agents**.
4. Log in there to the sites agents need.

Agents then act in that profile, and `browsers_list` marks it `[agent profile]`. `browsers_select` still chooses another browser by name or id, and that choice sticks.

## Setup

The server runs inside your AI client, which starts it when a session starts
and stops it when the session ends. There are three ways to set it up in Claude
Code.

**Ask Claude Code.** Tell it "Set up MCP Bridge for Firefox from
github.com/MashdorDev/mcp-bridge-for-firefox". It follows the steps in the
README: install [uv](https://docs.astral.sh/uv/) if it is missing, then install
the plugin. The add-on's toolbar popup offers the same request, with a Copy
button, whenever it has no server to talk to.

**Install the plugin**, from the `/plugin` menu or in a terminal:

```bash
claude plugin marketplace add MashdorDev/mcp-bridge-for-firefox
claude plugin install firefox@mcp-bridge-for-firefox
```

The repo is its own plugin marketplace (`.claude-plugin/marketplace.json`), and
its one plugin, `firefox` (`claude-plugin/`), registers the server so that Claude
Code starts it in every session.

**Register the server directly** (needs uv):

```bash
claude mcp add firefox -- uvx --from git+https://github.com/MashdorDev/mcp-bridge-for-firefox@v1.11.1 mcp-bridge-for-firefox --stdio
```

`uvx` downloads the tagged release, installs it as a Python package (see
`pyproject.toml`) and runs its `mcp-bridge-for-firefox` command, so there is
nothing to clone. The plugin runs the same command.

After either, restart Claude Code, or reconnect the server under `/mcp`, for the
browser tools to appear. Clients that read an `mcp.json` (Cursor, Claude Desktop,
VS Code) take the same command:

```json
{
  "mcpServers": {
    "firefox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/MashdorDev/mcp-bridge-for-firefox@v1.11.1", "mcp-bridge-for-firefox", "--stdio"]
    }
  }
}
```

All of these serve MCP over stdio. [Serving MCP over stdio](#serving-mcp-over-stdio)
covers what that means for the add-on's connection, and [Several
sessions](#several-sessions) how two Claude sessions share one browser.

## Starting the Server by Hand

From a clone, for development or to serve MCP over HTTP. `mcp-bridge-for-firefox`,
the command the package installs, takes the same options as `server/server.py`.

```bash
# Quick start (both WebSocket and MCP servers)
make run-server

# Custom configuration
python server/server.py --port 9000 --mcp-port 4000
python server/server.py --no-mcp  # WebSocket only, disable MCP server
python server/server.py --stdio   # MCP on stdin/stdout, for a client that launches it
```

## Command Line Options

```bash
python server/server.py [options]

Options:
  --host HOST          Host to bind to (default: localhost, security-enforced)
  --port PORT          WebSocket port (default: 8765)
  --mcp-port MCP_PORT  MCP server port (default: 3000)
  --no-mcp             Disable MCP server
  --stdio              Serve MCP on stdin/stdout instead of HTTP
  --disable-tools GROUP[,GROUP...]
                       Tool groups to leave unregistered (default: none)
  --enable-tools TOOL[,TOOL...]
                       Individual tools to register anyway (default: none)
  --real-input-only    Never fall back to scripted events (also FOXMCP_REAL_INPUT_ONLY=1)
  -h, --help           Show help message
```

## Real input only

Without real input, `content_click`, `content_type`, `content_press_key`,
`content_hover` and `content_drag` dispatch events from a script. Most pages
accept those. Some do not, and some accept them only on the surface: Facebook's
composer shows typed text and keeps none of it, so a post published after a
scripted `content_type` goes out empty while every tool reported success.

`--real-input-only` (or `FOXMCP_REAL_INPUT_ONLY=1` in the client's environment)
turns the fallback off. A click, key, typing, hover or drag that cannot be done
with real input fails and says why, and nothing is sent to the page. A page's own
drag-and-drop still drops by script, because Firefox's remote control never
drops one; that case is unchanged. Real input itself is set up as before: turn it
on in the add-on's settings (Windows, add-on 1.9 or later) or start Firefox with
`--remote-debugging-port`.

## Serving MCP over stdio

Started by hand, the server offers MCP over HTTP, which means starting it
yourself and leaving it running. `--stdio` offers the same tools on stdin and
stdout instead, so an MCP client can launch the server itself and shut it down
again when it exits, the way most clients expect to manage a server. Every
route under [Setup](#setup) uses it.

To run a clone this way, from the directory you want it scoped to:

```bash
claude mcp add --scope project firefox -- /path/to/mcp-bridge-for-firefox/venv/bin/python \
  /path/to/mcp-bridge-for-firefox/server/server.py --stdio
```

Name `venv/bin/python` rather than `python`: the server needs the virtual
environment's packages, and a client launches the command with its own
environment, not your shell's. On Windows the interpreter is
`venv\Scripts\python.exe`.

The extension side does not change. The WebSocket server still listens on 8765
(or the next free port, see [Several sessions](#several-sessions)), the extension
still connects to it, and every tool works as it does over HTTP.
Three things are different:

- **No HTTP listener.** Nothing serves port 3000, so `--mcp-port` is refused
  rather than silently ignored. `--no-mcp` is refused too, since it would leave
  stdio mode with nothing to serve.
- **The server lives as long as the client.** Closing the client ends the
  process, and the next client starts a new one. That has two consequences worth
  reading: [Several sessions](#several-sessions) and [The reconnect
  gap](#the-reconnect-gap).
- **stdout carries the protocol.** All logging goes to stderr, where your client
  keeps its MCP server logs.

### Several sessions

<a id="one-session-at-a-time"></a>
Every client that launches a server launches its own, so two `claude` sessions in
two terminals means two servers. The first binds 8765. A server that finds 8765
taken moves to the first free port of the next four, 8766 to 8769
(`EXTRA_SERVER_PORTS` in `server/server.py`). On Windows the free-port test asks
for exclusive use of the port, so a second server cannot quietly share 8765 with
the first.

The add-on connects to the server on 8765 and, while that connection is up,
looks for servers on the four ports after it every 15 seconds
(`scanExtraServers` in `extension/background.js`). Each session gets a connection
of its own, and each reply goes back on the connection its request came in on,
so several sessions can use the browser at once:

```
terminal 1:  claude  ->  server binds 8765, add-on connects
terminal 2:  claude  ->  8765 taken, server binds 8766, add-on connects within 15s
```

Three things follow from how this works:

- **A new session may wait up to 15 seconds.** Until the next scan, its tools
  answer `No extension connection available`. Run the call again.
- **The scan runs only while 8765 is connected.** If the session holding 8765
  ends, the others keep their connections, and the next session to start takes
  8765 again, since it is free.
- **Five sessions at once is the limit.** A sixth finds 8765 to 8769 taken and
  exits with an error naming the port. The add-on's "Other sessions" setting sets
  how many ports after 8765 it scans (4 by default; 0 connects to 8765 only).

Mixing modes works the same way: a standing HTTP-mode server on 8765 pushes a
client's `--stdio` server to 8766. To have several clients share one server
instead, run it in HTTP mode and point them all at `http://localhost:3000/mcp/`.

### The reconnect gap

The extension opens the WebSocket connection, not the server. The server only
listens on 8765 and waits. Over HTTP that ordering never matters, because you
start the server once and leave it running: it is up before Firefox connects and
still up after. Under `--stdio` the server is the short-lived side, alive only
while your MCP client is, and the two have no way to coordinate.

Two things follow, and neither is fixed in the server.

**A tool call in the first few seconds fails.** The server accepts MCP requests
as soon as your client launches it, but the extension is somewhere in its
five-second retry cycle and has not connected yet. A tool call in that window
returns `No extension connection available` immediately rather than waiting for
the browser. Run it again.

**The extension slows down without a server, but does not give up.** It retries
every Retry Interval for 50 attempts, at least four minutes at the default five
seconds, and then every 30 seconds for as long as the server stays away. Leaving
Firefox open and your editor closed is the normal case, so a server that comes
back is picked up on the next attempt:

```
12:00  editor exits, server dies
12:00  extension starts retrying, every 5s
12:05  50 attempts spent, extension retries every 30s
13:00  editor starts, server comes back
13:00  extension connects on its next attempt, within 30s
```

**A request monitor does not survive the gap.** The extension clears every monitor
it holds, and everything they captured, whenever the connection to the server ends,
which under `--stdio` is each time your client exits. Start monitoring, read what it
caught, and stop it within the one client session; a `monitor_id` from an earlier
session answers `MONITOR_NOT_FOUND`. See
[How long a monitor lives](web-request-monitoring.md#how-long-a-monitor-lives).

To get it back, open the extension popup and press **Reconnect**, which resets
the counter and dials again (and clears any monitors, as any other end of the
connection does). Restarting the extension or Firefox does the same
thing the slow way.

**Raise the Retry Interval if you use this mode.** The extension's options page
accepts 1000 to 60000 ms. The 50-attempt budget is spent at whatever rate you
set, so 60000 ms buys 50 minutes of quiet instead of four:

| Retry Interval | Budget before it stops | Delay picking up a new server |
|---|---|---|
| 5000 ms (default) | 4 minutes | up to 5 seconds |
| 30000 ms | 25 minutes | up to 30 seconds |
| 60000 ms (max) | 50 minutes | up to 1 minute |

The cost is the third column: a longer interval means the extension takes longer
to notice the server your client just started, which widens the first problem
while narrowing the second.

**Which mode to use.** It depends on how long your client lives, not on which
client it is.

A desktop client you open in the morning and leave running, such as Claude
Desktop, holds one server all day. The gap opens once, at startup, and the
extension has reconnected long before you ask for anything.

A terminal client is the opposite. Every `claude` invocation starts a server and
kills it on exit, so a morning of short sessions cycles the server all morning.
Each session pays the first-call gap, and the time between sessions comes out of
the 50-attempt budget. This is the case the example command at the top of this
section configures, which is why it is worth saying plainly.

If that is how you work, HTTP mode avoids all of it: one server, started once,
outliving every client and every session.

## Reducing the Tool Surface

Every tool's description sits in an MCP client's context for the whole session,
whether or not the tool is ever called. The full set of 51 tools costs roughly
9,700 tokens. `--disable-tools` leaves a group unregistered, so a client never
sees it:

```bash
# A setup that only drives tabs and reads pages
python server/server.py --disable-tools bookmarks,history,requests
```

The groups, and what each costs when enabled. The token figures estimate the
serialized name, description and input schema of each tool at four characters per
token, so treat them as proportions rather than exact counts:

| Group | Tools | ~Tokens |
|---|---|---|
| `content` | get_text, get_html, execute_script, execute_predefined, snapshot, click, hover, drag, type, press_key, upload_files, copy_image, paste | 3,462 |
| `tabs` | list, create, close, switch, release, move, capture_screenshot, group, ungroup, groups_update, groups_query | 2,536 |
| `thunderbird` | capture_window, snapshot, click, type, press_key, console, update_addon (Thunderbird) | 1,442 |
| `windows` | list, get, get_current, create, close, focus, update | 1,028 |
| `requests` | start_monitoring, stop_monitoring, list_captured, get_content | 853 |
| `mail` | list_accounts, list_folders, list_messages, search_messages, get_message (Thunderbird) | 732 |
| `bookmarks` | list, search, create, create_folder, update, delete | 703 |
| `navigation` | back, forward, reload, go_to_url | 546 |
| `history` | query, get_recent, delete_item | 304 |
| `browsers` | list, select | 196 |
| `debug` | websocket_status | 78 |

All groups are on by default. An unrecognized group name is an error rather than
a warning, so a typo cannot silently leave the group enabled.

Disabling a group hides its tools; it does not restrict the extension, which
still holds the same browser permissions. This is a context-size option, not a
security boundary. To stop the agent from doing something, turn it off on the
add-on's settings page instead ([What the agent may do](#what-the-agent-may-do)).
See also [architecture.md](architecture.md#security-architecture).

### Keeping one tool out of a disabled group

Sometimes a single tool is the reason to keep a group, and the rest of it is
dead weight: a screenshot of the page, without the ten tab tools that manage
tabs. `--enable-tools` names tools to register even though their group is
disabled:

```bash
# The page tools, plus a screenshot, and no tab management
python server/server.py --disable-tools tabs --enable-tools tabs_capture_screenshot
```

Names are the ones the client sees, as listed in
[api-reference.md](api-reference.md#available-mcp-tools). Naming a tool whose
group is enabled anyway does nothing, but a name that matches no tool is an
error, on the same reasoning as an unknown group: the symptom would otherwise be
the one tool you wanted quietly missing.

Most tools take a `tab_id` that in practice comes from `tabs_list`, so enabling
one of those on its own leaves you with a tool you have no way to address.
`tabs_capture_screenshot` is the exception that makes the example above work: it
captures the visible tab and takes no tab ID at all.

### Setting both from the environment

Both lists can be set through the environment, for clients that launch the
server through a wrapper whose arguments you do not control:

```bash
export FOXMCP_DISABLE_TOOLS=bookmarks,history
export FOXMCP_ENABLE_TOOLS=tabs_capture_screenshot
```

`--disable-tools` overrides `FOXMCP_DISABLE_TOOLS`, and `--enable-tools`
overrides `FOXMCP_ENABLE_TOOLS`, when both are given.

## Security Features

- **Localhost-only binding**: Both WebSocket and MCP servers bind to `localhost` only for security
- **Host enforcement**: Any attempt to bind to external interfaces (e.g., `0.0.0.0`) is automatically changed to `localhost` with a warning
- **Default secure configuration**: No configuration required for secure localhost-only operation

## Server Ports

- **WebSocket Port**: Default `8765` - Used for Firefox extension communication. A server that finds it taken uses the next free one of 8766 to 8769 (see [Several sessions](#several-sessions))
- **MCP Port**: Default `3000` - Used for MCP client connections

## Real input

Events an extension dispatches are untrusted, and pages that check ignore them:
Miro's canvas and comment menus, and native context menus. `content_click`,
`content_hover`, `content_drag`, `content_type` and `content_press_key` send real
mouse and keyboard input instead, which a page cannot tell from a person's. Their
results then end in `(real input)`. There are two ways it gets there, tried in
this order, and when neither reaches the tab the same tools fall back to events
dispatched by script.

### On Windows: window messages

On Windows nothing has to be set up. The server posts the mouse and key messages
to the Firefox window holding the tab (`server/win_input.py`), and Firefox turns
them into trusted events as it does for a real mouse and keyboard. Firefox runs
normally: no remote control, no `navigator.webdriver`, nothing a page can see.

- **Your pointer stays yours.** The messages carry their own position, so the
  mouse pointer on screen does not move and you can keep working in other windows.
  The Firefox window may sit behind others, but not minimised. When the tab is not
  showing, or no Firefox window with the page's title is at its spot, the action
  falls back to scripted events: input never goes to another Firefox window that
  happens to be there.
- **The tab comes to the front of its window** before each real action, because
  input reaches only the tab showing in a window.
- **Shortcuts work**: for a combination such as `Alt+2` or `Control+a` the server
  sets the modifier keys on Firefox's input thread for the length of the
  combination.
- **Emoji are the exception.** Firefox drops characters outside the Basic
  Multilingual Plane that arrive as window messages, so text containing them goes
  through remote control when it is on, or is typed by script.
- If your own pointer is over the Firefox window, its moves reach the page too,
  which can close a hover menu the agent opened.

### Elsewhere: remote control (WebDriver BiDi)

On macOS and Linux, and on Windows when the window is not found, real input goes
through Firefox's remote control.

#### Starting Firefox with remote control

Start Firefox with `--remote-debugging-port=PORT`. For a profile you open from a
shortcut, add the flag to the shortcut's target:

```
"C:\Program Files\Mozilla Firefox\firefox.exe" -P "Work" --remote-debugging-port=9223
```

If another Firefox is already running on the computer, add `-no-remote` as well.
Without it the running Firefox opens the profile through its profile switcher and
drops the flag, and the new window has no remote control.

#### How the server finds it

The server finds the port by itself. It tries 9222 to 9229, the ports remote
control usually listens on (`LIKELY_PORTS` in `server/bidi.py`), and remembers the
one that worked for each connected browser. With several profiles under remote
control, it still picks the right Firefox: before each real action the content
script puts a one-time token on the tab (a `data-foxmcp-tab` attribute on
`<html>`), and the server looks for that token through each port, so only the
Firefox holding the tab answers. When no port answers, the server uses scripted
events and does not look again for 60 seconds.

Firefox allows one BiDi session at a time, and several servers may share a
browser, so each action opens a session, does its work and ends it.

### The add-on's settings

"Use real input", under Real input on the settings page, is on by default; turn
it off to always use scripted events. "Remote control port" is 0 by default,
which means the server finds the port; set it only when Firefox was started with
a port outside 9222 to 9229. The toolbar popup shows which applies.

Real input does not get round the settings under [What the agent may
do](#what-the-agent-may-do). The content script's preparation step is an ordinary
request, refused like any other while the agent is paused, when "Click, type and
press keys" is off, or on a blocked site.

#### What remote control changes in Firefox

With remote control on, Firefox applies Mozilla's recommended automation
preferences to the profile, which among other things turn session restore off. To
keep the profile's own preferences, add this line to `user.js` in the profile
folder:

```js
user_pref("remote.prefs.recommended", false);
```

**While the port is open, any program on this computer can drive Firefox through
it**, signed-in sessions included, and the add-on's settings do not cover those
programs. Every page can also tell the browser is remote controlled
(`navigator.webdriver` is true), and sites with bot checks act on it; ChatGPT
showed a Cloudflare check. On Windows, leave the flag out: window messages need
none of this.

### Limits

- Firefox's remote control starts a page's own HTML5 drag-and-drop but cannot
  finish it: the drop never happens. `content_drag` therefore uses the scripted
  drag when it starts on a draggable element, a link or an image, and pages
  accept its drop events. Drags on canvases, sliders and lists that follow the
  mouse themselves use real input.
- Miro's menus do not close on Escape, even with real input.

## The Add-on's Settings Page

Open it with the Settings button in the toolbar popup, or from `about:addons`,
where it is shown inside the add-on's page (`options_ui.open_in_tab` is false).
Every change saves as it is made and applies at once, to this Firefox profile
only. The popup is a status panel: the connection and any other sessions, this
browser's name, the tabs an agent is working in (go to or release each), and the
follow and pause switches.

Settings are kept in `storage.local`, per profile, so Firefox Sync does not copy
one profile's name or blocked sites to every other. Their names and defaults are
in `extension/settings.js`, which the background script, the content script, the
popup and the options page all load. Host and port are in `storage.local` too.
The reconnect timings (retry interval, give up after, ping timeout) stay in
`storage.sync`, where older versions kept them.

### What the agent may do

**Pause the agent** refuses every request until it is turned off, and the agent
lets go of its tabs. The same switch is in the toolbar popup.

**Allowed** lists ten groups of actions, each on by default and each able to be
turned off on its own. A refused request reaches the agent as an error naming the
group, such as `"Run scripts in pages" is turned off in this browser's MCP Bridge
settings.`

| Setting | Covers |
|---|---|
| Read pages and take screenshots | `content_get_text`, `content_get_html`, `content_snapshot`, `tabs_capture_screenshot` |
| Click, type and press keys | `content_click`, `content_hover`, `content_drag`, `content_type`, `content_press_key`, and real input |
| Run scripts in pages | `content_execute_script`, and so `content_execute_predefined` |
| Put files on upload fields | `content_upload_files` (with a trigger element, it also needs "Click, type and press keys") |
| Copy images and paste | `content_copy_image`, `content_paste` |
| Open, close, switch and navigate tabs | `tabs_list`, `tabs_create`, `tabs_close`, `tabs_switch`, `tabs_move`, and the `navigation_*` tools |
| Manage windows and tab groups | The window tools, `tabs_group`, `tabs_ungroup`, `tabs_groups_update`, `tabs_groups_query` |
| Read and delete history | The `history_*` tools |
| Read and change bookmarks | The `bookmarks_*` tools |
| Watch network requests | The `requests_*` tools |

The mapping from actions to groups is `ACTION_PERMISSIONS` in
`extension/settings.js`. `tabs_release` belongs to no group and always works, so
an agent can let go of a tab even while paused.

**Blocked sites** takes one site per line. The agent cannot read, click, open or
screenshot a blocked site or its subdomains: `example.org` also blocks
`www.example.org`, and a full URL pasted in works too.

**Private windows** are off limits unless "Let the agent work in private windows"
is on. Firefox also has to allow the add-on in private windows (in `about:addons`).

**Following**, "Show me the tab the agent works in", brings the tab the agent acts
in to the front. It is off by default, so the agent works in background tabs and
you stay on yours.

**Let go of a tab after** sets how many minutes of no action in a tab (5 by
default) clear the overlay from it. It also leaves when the agent calls
`tabs_release` or its session ends.

### On the page

While the agent works in a tab, an overlay shows it. It can be turned off
altogether, or in parts: the glowing border around the page, the line saying what
the agent is doing (at the bottom or the top of the page), and the pulsing ring on
the tab's icon. Its colour can be changed, with a button to go back to the
default.

The agent's cursor can be hidden. Its movement is "Like a hand" (a slight curve
and a small correction at the end) or "Direct", and its speed is slow, normal or
fast; slower moves make each click and keystroke take longer. "Move the cursor
like a resting hand" (on by default) keeps it alive between actions: it mostly
stays put, now and then drifts a few pixels, and while the agent reads it follows
the lines and sweeps back to the start of the next, the way pointers move during
reading and search (Rodden et al., CHI 2008; Huang, White and Buscher, CHI 2012).
Only the drawn cursor moves; the page gets no mouse events from it. With "Show text
being typed in" off, text goes in at once, which is faster.

The cursor takes the shape the system cursor would have where it is, as the page
sets it: an arrow, a pointing hand over links and buttons, an I-beam over text,
arrows for moving and resizing, a no-entry sign, a crosshair, a magnifier, and an
arrow with a busy, help, copy or link mark. A drag shows the hand: open on the way
to the item, closed while the button is held, with a plus over a place that takes
a dropped item and a cross over one that does not, and open again on letting go.

The toolbar button shows "off" when no server is connected, "stop" when the agent
is paused, and otherwise how many tabs the agent is working in. "Show status on
the button" turns that off.

### Connection

"This browser's name" is how `browsers_list` tells this profile apart from others
(see [Several Firefox Profiles](#several-firefox-profiles)). Host and port are the
server's, `localhost` and `8765` by default. "Other sessions" is how many ports
after that one the add-on scans for more sessions, 4 by default (see [Several
sessions](#several-sessions)). Real input is described
[above](#real-input). Under Reconnecting: "Retry every" (1000 to
60000 ms, 5000 by default), "Give up after" (-1 keeps trying for as long as
Firefox is open), and the ping timeout. A Reconnect button dials the server again.

### Backup and reset

**Export settings** saves this profile's settings, connection and reconnect
timings to a JSON file. **Import settings** reads such a file into another
profile. It takes only known settings of the right type, and keeps the importing
profile's own name. **Reset all settings** asks for a second press within four
seconds, then puts everything back to its default except the browser's name.

## Programmatic Server Configuration

```python
# Default configuration (localhost-only, secure)
server = FoxMCPServer()  # WebSocket: localhost:8765, MCP: localhost:3000

# Custom ports (still localhost-only)
server = FoxMCPServer(host="localhost", port=9000, mcp_port=4000)

# WebSocket only (disable MCP)
server = FoxMCPServer(port=8765, start_mcp=False)
```

## MCP Client Connection

1. **Start the server** (both WebSocket and MCP servers)
2. **Load Firefox extension** (connects automatically to WebSocket)
3. **Connect MCP client** to `http://localhost:3000`

Or let the client start the server for you, as every route under
[Setup](#setup) does. There step 1 is the client's job and step 3 does not apply.

### Supported MCP Clients

**Claude Code**:
```bash
claude mcp add --transport http firefox http://localhost:3000/mcp/
```

**Other MCP Clients**:
Connect directly to `http://localhost:3000/mcp/`

**Complete Workflow**:
```
MCP Client → FastMCP Server → WebSocket → Firefox Extension → Browser API
```

## Environment Variables

### Required for Predefined Scripts

```bash
# Set path to your custom scripts directory
export FOXMCP_EXT_SCRIPTS="/path/to/your/scripts"
```

### Optional Configuration

The server reads three environment variables. `FOXMCP_EXT_SCRIPTS` points at the
directory holding predefined scripts:

```bash
export FOXMCP_EXT_SCRIPTS=/path/to/predefined/
```

`FOXMCP_DISABLE_TOOLS` names tool groups to leave unregistered, and
`FOXMCP_ENABLE_TOOLS` names individual tools to register anyway. See
[Reducing the Tool Surface](#reducing-the-tool-surface).

Ports are set on the command line, not through the environment. See
[Multiple Server Instances](#multiple-server-instances).

## Several Firefox Profiles

Every profile running the add-on can connect to the same server at once. The
first to connect is active; one arriving later never takes over. Name each
profile in its options page, then:

```text
browsers_list                      -> - Main (active): id 3f2a9c1e ...
                                      - Lost Tribe: id 7b0d4e22 ...
browsers_select browser="Lost Tribe"
```

When the active browser disconnects, the most recently connected one takes over.
A profile that reconnects keeps its id, so a selection survives a dropped
connection.

To keep profiles fully apart instead, give each its own port in the options page
and run one server per port, each as its own MCP entry:

```bash
claude mcp add firefox-work -- uvx --from git+https://github.com/MashdorDev/mcp-bridge-for-firefox@v1.11.1 mcp-bridge-for-firefox --stdio --port 8775
```

Pick a port outside 8765 to 8769, which the first entry's sessions use (see
[Several sessions](#several-sessions)); the second entry's sessions then use the
four ports after its own. Each entry adds the full tool set to every session
(about 9,700 tokens), so one server with `browsers_select` is the cheaper default.

## Multiple Server Instances

You can run multiple FoxMCP servers on different ports:

```bash
# Server 1 - Default ports
python server/server.py

# Server 2 - Custom ports, clear of the session ports 8766-8769
python server/server.py --port 8775 --mcp-port 3001

# Server 3 - WebSocket only
python server/server.py --port 8785 --no-mcp
```

## Logging Configuration

### Basic Logging

```python
import logging

# Set log level
logging.basicConfig(level=logging.INFO)

# Start server with logging
server = FoxMCPServer()
```

### Advanced Logging

Importing `server.server` calls `logging.basicConfig(level=logging.INFO)` at module
level, so a later `basicConfig()` call does nothing. Reconfigure the root logger
instead:

```python
import logging
from server.server import FoxMCPServer

root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.addHandler(logging.FileHandler('foxmcp.log'))

server = FoxMCPServer()
```

## Tuning

`FoxMCPServer` takes only `host`, `port`, `mcp_port`, `start_mcp`, `disabled_tool_groups`,
`enabled_tools` and `use_stdio`. WebSocket frame
sizes, ping intervals, and MCP concurrency are not exposed: the server accepts the
`websockets` and FastMCP defaults, apart from a 64 MB limit on a message from the
extension (`MAX_MESSAGE_BYTES`), which a full-window screenshot needs.

Two things you can change:

**Request timeout.** `send_request_and_wait` waits 30 seconds by default. Pass
`timeout` to override it for a slow call:

```python
response = await server.send_request_and_wait(request, timeout=60.0)
```

A timeout returns an error dict rather than raising, so callers must check for
`"error"` in the response.

**Extension reconnection.** `CONFIG` at the top of `extension/background.js` sets
`retryInterval` (5000 ms) and `maxRetries` (`-1`). Both are also editable on the
add-on's settings page, under Connection. Changing the source values requires a
rebuild.

`-1` retries forever: 50 attempts at `retryInterval`, then one every 30 seconds
(`FAST_RETRIES` and `SLOW_RETRY_INTERVAL` in the same file). A positive value stops
after that many attempts, until you press **Reconnect** in the popup or restart the
extension. This matters most under `--stdio`, where the server comes and goes with
your client: see [The reconnect gap](#the-reconnect-gap).

## Troubleshooting Configuration

### Common Issues

1. **Port already in use**: a server that finds 8765 taken moves to 8766 to 8769
   by itself (see [Several sessions](#several-sessions)), so this error means all
   five are taken, or a non-default `--port` and the four after it are.
   ```bash
   # Check what's using the port
   lsof -i :8765

   # Use a port outside the session range
   python server/server.py --port 8775
   ```

2. **Extension can't connect**:
   - Check server is running: `curl http://localhost:8765`
   - Verify extension configuration matches server ports
   - Check browser console for connection errors

3. **MCP client connection issues**:
   ```bash
   # Reach the MCP server
   curl http://localhost:3000
   ```

   The server logs to stderr, so run it in the foreground to watch connections
   arrive. Under `--stdio` your client keeps that output with its MCP server logs.

### Debug Logging

The server logs at `INFO`. There is no verbosity flag: the level is set in
`server/server.py`.

For extension-side detail, set `ENABLE_DEBUG_LOGGING_TO_SERVER = true` near the top of
`extension/background.js`. The extension then forwards its logs over the WebSocket and
the server prints them under `----- EXTENSION DEBUG LOG -----`. Rebuild after the edit,
and set it back to `false` before committing:

```bash
make clean && make package && rm -rf dist/profile-cache/*
```

## Checking Server Health

There are no health, status, or metrics HTTP endpoints. Three things tell you whether
the pair is connected:

| Check | How | Tells you |
|---|---|---|
| `debug_websocket_status` | Call the MCP tool | Whether the extension is connected right now |
| Extension popup | Click the toolbar icon | Connection state from the browser's side |
| `make status` | From the project root | Whether port 8765 is in use |

The server logs connections and disconnections to stderr, so running it in the
foreground is the quickest way to watch the socket come and go.