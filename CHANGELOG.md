# Changelog

All notable changes to FoxMCP will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`content_read` and `content_wait_for`.** The text of one element, and a wait for an element, text or title, which took a script or a whole page of text before.
- **`content_dialogs`, `content_select_option` and `page_fetch_json`.** Open dialogs and what is stuck over a page, custom dropdowns, and a GET on the page's own origin, without a script.
- **An agent profile.** Tick "This Firefox is for agents" in a separate Firefox profile and agents act there unless one is chosen by name. In its own process, real typing never touches the windows you are using, with no blink and no wait. See `docs/configuration.md`.

### Added
- **`page_realm` on `content_execute_script`**, for page objects the isolated realm can't iterate.
- **`scripts/clean-automation-prefs.py`** removes the test prefs `--remote-debugging-port` leaves in a profile.

- **`content_snapshot` says when a long list has more rows than are drawn**, where the page gives the count. `browsers_list` says whether real input is on.

### Fixed
- **A page's own Copy button counts as the agent copying.** `content_paste` refused what it put on the clipboard as the person's. On Windows, with real input.
- **Another add-on's page says it can't be captured.** It returned a blurred thumbnail that read as a capture.
- **Real input stops when its target does.** The add-on is asked before every click, key and batch of text whether the tab still shows in its window and the agent isn't paused, and the foreground window is checked again. Once input has gone in, a stop is reported as a stop and never redone by another route. If something else switches the tab mid-text, at most 10 characters can reach the new tab.
- **One real input at a time across all servers on a computer**, so one session handing activation back can't send another's text to the person's window.
- **Keys no longer click a field that already has the focus.** `content_press_key` with a target clicked it first, every time, so `Control+a` followed by `Backspace` deleted one character instead of the selection.
- **"Show me the tab the agent works in" off now means it.** Real input brought the agent's tab forward anyway, because window input only reaches the tab showing. With the setting off, tabs the agent opens go in a window of their own, opened without focus, and a tab it uses in your window is shown only for the action, then yours comes back. The agent's window has `[Agent]` in its title. If you are using another window of that Firefox, keys wait for you to pause for 2 s, for up to 10 s, then borrow its activation for just that action.

### Fixed
- **A hung page is named.** A request to a page that stopped answering said only "Request timed out". It now says which tab, that other tabs aren't affected, and how to clear it.
- **`focus_window` says when Windows refused.** It reported "focused successfully" whenever the request was accepted, including when the window stayed behind.

### Fixed
- **A profile keeps its default name on every server.** A profile with no name set was named "Firefox N" by the order it connected, so the same profile could be "Firefox 1" on one server and "Firefox 2" on another. The name now comes from the profile's own id. Names you set are unchanged.
- **`browsers_select` fails as a failure.** An unknown name came back as an ordinary answer, which repeats the name, so a check on the reply could pass.
### Added
- **`content_scroll` scrolls what a wheel would.** Feeds that scroll in an inner container, such as Facebook's, didn't move for keys or the document. It finds the nearest container that can still go that way at an element or point, and says what moved and how much is left. The on-page cursor shows a mouse with its wheel while it scrolls.
### Changed
- **Calls are about 80 ms faster, all of them.** A reply woke its waiting tool only on the HTTP server's next 100 ms tick: `content_execute_script` went from 108 ms to 25 ms.
- **Clicks take about half as long.** They wait for the page's reaction only as long as they need to, 80 to 300 ms instead of a fixed 400, and cursor moves are capped at 400 ms instead of 900. A median click went from 720 ms to 374 ms.
- **Fewer tokens.** Tool schemas leave out what they repeated, and results are no longer wrapped twice. `content_get_text` stops at 20000 characters by default and says how to get more. Its reply and `content_execute_script`'s no longer start with a line about the page.
- **Screenshots you look at are in CSS pixels.** A point read off one goes straight into `content_click(x, y)`, and it costs far fewer tokens at high display scaling. Saved files stay at full resolution.

## [1.13.0] - 2026-10-30

### Fixed
- **Navigation tools answer once the new page has loaded.** A read right after `navigation_go_to_url`, back, forward or reload could return the page before it, so refs from it failed. They wait up to 15 s, then say the page is still loading.
- **Real keys work with Firefox in the background.** Keys posted to a window Firefox believed inactive went nowhere while the tool said "Pressed (real input)". Firefox now gets a window-level activation for the action, so the page takes trusted keys and combinations such as Control+a and Shift+Enter, while the window you are using stays in front and keeps your keyboard. It steps down after 20 s idle. If you are using another window of that Firefox, it leaves yours alone and says so. A covered window gets real input too, which used to be refused for as long as it stayed covered. A minimised one takes keys, and refuses clicks because they can't be aimed.
- **`content_type` waits for the field to settle before calling it partial.** Editors apply typed text on their own schedule, so a check right after typing could report "Only part of the text arrived" while the rest was still coming, and typing again doubled it.
- **`content_type` with no ref types at the caret.** With real input it types into the focused field and leaves its text in place, instead of failing with `'NoneType' object is not subscriptable`. If no editable field has the focus, it says so.
- **`content_type(clear=False)` adds to the end.** The click that focuses the field landed mid-text, so the new text went in the middle.

## [1.12.0] - 2026-10-30

### Added
- **`content_type(commit=True)` leaves the field after typing.** Chip and token inputs, such as Google Analytics' "Enter email addresses", turn the text into an entry only when focus moves away, and Enter submitted the form first, so Add answered "You must enter at least one email address". The field gets `blur`, `focusout` and `change` as a click elsewhere would give it, also in a Firefox window that is not in front, where `blur()` alone fires nothing. The result says whether the field emptied itself, which is how such inputs show they took the entry.

### Fixed
- **Parallel clients no longer click each other's tabs.** Real input acts on whichever tab is showing, and nothing held the tab in place, so clicks reported as real could land on another client's tab: 18 clicks across 3 tabs arrived as 0, 6 and 10. Real actions now take turns. The lookup also waits a moment for Firefox to finish switching tabs, which is what caused most "no real input right now" refusals, and the refusal now says which case it is: setting off, browser disconnected, or tab not showing.
- **The agent overlay keeps its look on pages with a strict style CSP.** Its styles were a `<style>` element and style attributes, which pages such as Teams block, so the "Working on this page" label showed as plain serif text in the top left and the cursor was drawn black and huge (a third of the window). The styles now come from a constructed stylesheet, which the page's CSP does not cover.
- **Browsers come back sooner after `/mcp` restarts the server.** The add-on keeps retrying every 5 seconds while another session's server is still connected, instead of every 30 seconds once its first 50 attempts are used up. Right after the server starts, `browsers_list` waits up to 5 seconds for a browser and then says it is waiting for browsers to reconnect, rather than "No browser is connected". In a test with the slow interval reached, a restarted server had its browser back after 11.7 s instead of 20 s.
- **The add-on's console stays quiet while the server is down.** It logs one line when the server is missing, one when the connection drops (with its port and close code) and one when it connects, instead of four lines for every attempt. Firefox's own "can't establish a connection" line for each attempt still shows.
- **Browsers come back sooner after `/mcp` restarts the server.** When the connection to another session's server drops, the add-on tries that port every 3 seconds for the next minute, on its own, instead of waiting for the scan of every extra port that runs every 15 seconds. In three restarts on Windows, browsers were back 0.2 to 2.3 s after the server was up, against 4.7 to 9.6 s before. Right after the server starts, `browsers_list` waits up to 5 seconds for a browser to say hello and then says it is waiting for browsers to reconnect, rather than "No browser is connected".
- **The add-on's console stays quiet while the server is down.** It logs one line when the server is missing, one when a connection drops (with its port and close code) and one when it connects, instead of four lines for every attempt. Firefox's own "can't establish a connection" line for each attempt still shows.
- **Real input that shows no sign of reaching the page says so.** Window input could report a click, key press or typed text as done while the page got nothing (a Firefox window behind another one, or one Firefox does not deliver to), and typing then failed with a misleading "Only part of the text arrived". The add-on now watches for the real key and pointer events the page receives. When there were none, `content_click` and `content_press_key` add a warning to their result, and `content_type` (when the text is not in the field) says the keys most likely did not arrive. Nothing is done again automatically: the input may have landed where the page cannot see it, such as an iframe, the browser itself or another window.
- **Elements are scrolled to at once before an action.** On pages with smooth scrolling the element was still moving when its position was read.
- **Click by text looks in the open dialog first.** With a modal dialog open in front, `content_click(text=...)` and the other name lookups prefer what is inside it over the page behind it, which a person cannot reach. Menus, listboxes and dialogs that are hidden or not in front do not count. "Editor" had matched a table cell behind a role picker instead of the option in it.
- **A click says when something covers its target.** When the element at the target's centre is another one (a fixed header, or an invisible backdrop left by a modal stuck mid-fade), the result says what covers it, whether it is invisible, and which dialog is open in front. A click in a field's own label is not a cover, and a link that wraps is checked on its first line. A scripted click still reaches the element, but a person's click would land on the cover, and clicks on a covered Save button had been reported as done while nothing saved.
- **`content_type` on a `<select>` picks an option.** It chooses the option whose value or label is the text and fires `input` and `change`; a list without that option fails and names the options it has. Labels are matched before values, since a value can be another option's label. Disabled lists, options and option groups are refused. It used to fail with "'set value' called on an object that does not implement interface HTMLInputElement". With real input on, selects use the scripted path, also under `--real-input-only`, because a real click opens the browser's own list.
- **`content_press_key` with a printable key types it.** A single character goes into a text field unless the page cancels the key or handles it itself (writes it, or moves focus on). Read-only, disabled and date or time fields get only the key events. A target that cannot take focus fails instead of typing into the field that has it. Pressing `m` in a focused input used to add nothing.
- **Each `content_execute_script` call has its own scope.** A `const` from one call no longer makes the next fail with "redeclaration of const", and a top-level `return` or `await` works (the script then runs in an async function and its `return` is the result). Scripts that end in an expression return it as before.
- **Rich text fields get the whole text.** `content_type` puts the text into a contenteditable field in one step instead of a few characters at a time. YouTube Studio's title and description re-render while they suggest @mentions, and the pieces typed after that were lost: 36 of 78 characters reached the title. Inputs and textareas are still typed visibly.
- **`clear` replaces what a rich text field holds.** It selects the whole editor's contents instead of running `selectAll`, which did not stay inside YouTube Studio's title, so the new title was added after the old one. A target that is one paragraph of an editor clears the whole editor, as before. A clear that leaves other text in the field is now reported as an error, with real input too; zero-width filler characters that editors keep beside the caret do not count.
- **Appending to an input or textarea starts at the end.** With `clear=False` the first characters went in at the start of the field, where focus leaves the caret, and the rest at the end.

## [1.11.1] - 2026-10-14

### Fixed
- **The chosen browser stays chosen.** When the browser picked with `browsers_select` disconnects, tools no longer move to another connected profile. Calls fail with its name and the browsers that are connected until it comes back or another is selected. After an add-on update restarted one profile, tab ids from it had been sent to the person's main profile.
- **Screenshots show the agent's tab.** `tabs_capture_screenshot` with no tab or window captures the tab the agent last acted on, even a background one, instead of whichever window had focus. It also takes a `tab_id`.

## [1.11.0] - 2026-10-14

### Added
- **The person's clipboard stays theirs.** Paste keys and `content_paste` go through only while the clipboard holds what the agent itself copied; otherwise they fail and say why. Copy keys report whether anything was copied. On Windows the server compares the clipboard's change counter, which never reveals the contents; elsewhere the add-on compares a hash. A failed copy followed by a paste had put the person's own messages on a shared Miro board. `allow_clipboard=True` pastes the person's clipboard when they ask for it.
- **Click, hover and type by what a person sees.** `content_click(text=...)`, `content_hover(text=...)` and `content_type(field=...)` find the element by its label, aria-label, placeholder or text.
- **Clicks say what they changed:** a new address, a menu or dialog opening or closing, the caret moving into a field, the element expanding or getting checked, or none of these.

### Changed
- A selector that matches several elements acts on the first visible one (ChatGPT keeps a hidden second composer).
- `tabs_capture_screenshot` without a filename returns the image itself instead of a cut-off data URL, and a bare filename is saved in the temporary folder instead of the server's working folder.

### Fixed
- Real input on Windows could go to another Firefox window at the same spot when the tab's own window was not found, and reported success when the tab was not showing. It now requires a window showing the page's title and a showing tab, and otherwise falls back to scripted events.

## [1.10.0] - 2026-10-14

### Added
- **Drag with the middle or right button.** `content_drag` takes `button` (`"left"`, `"middle"` or `"right"`), to pan the view of canvas apps: Miro pans with the right button, Figma, draw.io and many maps with the middle one. Works with real input on Windows (window messages) and through WebDriver BiDi, and scripted.
- **The agent's cursor takes the page's cursor shapes.** Arrow, pointing hand, I-beam, open and closed hand, move, resize in four directions, not-allowed, crosshair, zoom in and out, and busy, help, copy and link marks, following the CSS `cursor` of whatever is under it. Drags show the open hand on the way, the closed hand while held, a plus or a cross over places that take or refuse a dropped item, and the open hand again on release.

### Fixed
- A real drag on Windows let go of the button while Firefox still read it as held, so pages got `mouseup` but no `pointerup`.
- The cursor showed "thinking" in the middle of a real drag, because the agent's next state arrived before the drag ended. It now waits until the button is let go.

## [1.9.0] - 2026-10-14

### Added
- **Real input on Windows without remote control.** The server posts mouse and key messages straight to the tab's Firefox window, which turns them into trusted events. Firefox no longer needs `--remote-debugging-port`, so pages no longer see `navigator.webdriver` (ChatGPT had started showing a Cloudflare check), the person's mouse pointer never moves, and the window can stay behind others. The tab is brought to the front of its window first. Shortcuts such as `Alt+2` and `Control+a` work; text with emoji falls back to remote control or scripted typing. On other systems real input still goes through WebDriver BiDi.
- **The agent's cursor moves like a resting hand between actions.** It mostly stays put, now and then drifts a little, and while the agent reads it follows the lines, after studies of the pointer during reading and search. Only the drawn cursor moves; pages get no mouse events. "Move the cursor like a resting hand" on the settings page turns it off.

## [1.8.1] - 2026-10-14

### Added
- **A Claude Code plugin, and setup by asking Claude.** The repo is now a plugin marketplace: `claude plugin marketplace add MashdorDev/mcp-bridge-for-firefox` and `claude plugin install firefox@mcp-bridge-for-firefox` (or the `/plugin` menu) set the server up, and Claude Code starts it in every session. With nothing to connect to, the add-on's popup now offers a request to copy into Claude Code, "Set up MCP Bridge for Firefox from github.com/MashdorDev/mcp-bridge-for-firefox", and the README carries the steps an agent follows, so nobody has to type a command.

## [1.8.0] - 2026-10-14

### Added
- **Drag, hover and click at a point.** `content_drag` drags from an element or a point to another, for boards and canvases (Miro, Excalidraw), reordering, dropping a card in a column and sliders. `content_hover` moves the mouse without clicking, for hover menus and buttons that only appear on hover. `content_click` takes `x` and `y` to click a point on a canvas. With real input these are real mouse moves along a hand-like path. Firefox's remote control starts a page's own drag-and-drop but cannot drop, so draggable elements, links and images get the scripted drag, whose drop events pages accept.

## [1.7.2] - 2026-10-14

### Added
- **The server installs and registers in one command.** It is now a Python package, so `claude mcp add firefox -- uvx --from git+https://github.com/MashdorDev/mcp-bridge-for-firefox@v1.7.2 mcp-bridge-for-firefox --stdio` sets it up on a new computer with no clone or virtualenv. Installing only the add-on left a second machine with nothing to connect to and no hint why; the toolbar popup now shows that command, pinned to the add-on's version and with a Copy button, whenever it is not connected.

## [1.7.1] - 2026-10-14

### Added
- **Real input through WebDriver BiDi.** Events an extension dispatches are untrusted, and pages that check ignore them: Miro's canvas and comment menus, native context menus. With Firefox started with `--remote-debugging-port=PORT` (the bridge finds the port on 9222-9229 by itself; a different one can be set under Real input), `content_click`, `content_type` and `content_press_key` send real mouse and keyboard input the page cannot tell from a person's; `content_press_key` also takes combinations such as `Control+a`, and `content_click` gains `right_click`. Every real action still passes the person's settings first (pause, permissions, blocked sites), real input can be switched off, and anything BiDi cannot reach falls back to scripted events. While remote control is on, other programs on the computer can drive Firefox through that port too.
- **Settings for what the agent may do and how it looks.** A pause switch, also in the toolbar popup, refuses every request and frees the agent's tabs. Ten groups of actions (reading pages, clicking and typing, scripts, uploads, clipboard, tabs, windows and groups, history, bookmarks, network monitoring) can each be turned off, sites can be blocked along with their subdomains, and private windows are off limits unless allowed. A refused request tells the agent why. On the page, the overlay, its glowing border, its status line and position, the cursor, its movement and speed, the typing animation, the ring on the tab's icon and the colour can all be changed, as can how long an idle tab is held, the toolbar badge, and how many session ports are scanned. Settings export to a file and import into another profile. All of them are per profile.
- **A new popup and options page.** Firefox's own font with light and dark colours. The popup shows the connection and any other sessions, this browser's name, the tabs an agent is working in (go to or release each), the follow and pause switches. The options page saves each change as it is made and sits in about:addons. The toolbar button shows "off" when nothing is connected, "stop" when paused, and otherwise how many tabs the agent is in.
- **The cursor moves like a hand on a mouse**, after the motor-control research on pointing: Fitts' law timing, a bell-shaped speed along a slight arc, a first move that usually stops a little short, and a small correction.
- **`content_copy_image` and `content_paste`, the context menu's "Copy Image" and "Paste".** The browser's own right-click menu cannot be opened or driven by an extension, and canvas apps like Miro ignore synthetic right-clicks. `content_copy_image` puts an image from a page, or a local image file, on the system clipboard as PNG. `content_paste` runs a real paste, so the page gets a trusted paste event carrying the clipboard's contents; Firefox allows that only into a textarea, so with any other target the paste goes to the page as a whole. Miro ignores pastes it did not get from a person; images go onto a Miro board through `content_upload_files`. The extension now asks for the `clipboardRead` and `clipboardWrite` permissions.
- **The agent says what it is doing, and the overlay stays until it is done.** Every tool takes an optional `status`, a few words in the first person ("Reading the board") shown on the overlay of the tab it works in; without one the overlay names the action. The overlay no longer fades a few seconds after the last action: it stays until the agent calls the new `tabs_release`, its server goes away, or five minutes pass with nothing done in the tab. The cursor stays on screen and shows whether the agent is thinking, reading, clicking, typing or waiting, and the tab's icon gets a pulsing ring in the tab strip.
- **"Follow the agent" option.** When on, the tab the agent acts in comes to the front. Off by default.
- **Several sessions on one browser.** A server that finds its port taken moves to one of the next four, and the extension connects to all of them, so two Claude sessions can drive the same Firefox at once. Each reply goes back to the server that asked.
- **Native tab groups: `tabs_group`, `tabs_ungroup`, `tabs_groups_update`, `tabs_groups_query`.** Group tabs, optionally with a title and colour; take them out again; restyle or collapse a group; and list groups with their IDs. None of them activates a tab or focuses a window. If the tabs are grouped but the title or colour cannot be set, `tabs_group` still reports the group and names what failed. Contributed by kernel-oops in [#8](https://github.com/ThinkerYzu/foxmcp/pull/8). Requires Firefox 139+, and the extension now asks for the `tabGroups` permission.

### Fixed
- **Real typing puts a line break in as Shift+Enter.** A plain Enter sent the message in chat boxes: two lines typed into a Miro comment reply posted the first one.
- **A failed step reaches the client as an error.** Tools reported failures as ordinary text ("Failed to type: ..."), which clients took for success. They now raise, so the client sees isError.
- **Tools pass on the extension's reason.** 33 tools answered "Unable to ..." where the extension had said why.
- **The tool list costs about 4,200 fewer tokens per session.** The status line is offered only on the 18 tools that act in a tab, with a shorter description.
- **On Windows a second server no longer takes over port 8765 from the first.** `SO_REUSEADDR` lets two sockets bind the same port there; the free-port test now uses `SO_EXCLUSIVEADDRUSE`.
- **`content_type` checks that the whole text arrived.** Rich editors could replace earlier pieces of a long text and report success with part of it in the field; the caret is now kept at the end between pieces and a short result is reported as an error.
- **Uploaded files carry their MIME type, and are rebuilt in the page's own realm.** They used to reach the page with an empty type, which ChatGPT copes with by reading the bytes and Miro's "Upload from device" does not: it showed "Uploading..." and never finished.
- **Typing into a background tab no longer times out.** Firefox throttles a hidden tab's timers, and the typing animation stalled there; the text now goes in at once when nobody can see the tab.
- **Typed text stays in the editor's last paragraph.** In ProseMirror editors (ChatGPT) each piece of a long text became a paragraph of its own.
- **`content_upload_files` refuses a disabled file input** instead of reporting files attached that the page dropped.
- **An overlay left by an older version is rebuilt** after an update, instead of breaking every action in tabs that stayed open.
- **The agent overlay shows from the agent's first action.** It used to appear only once the agent took a snapshot, clicked or typed, so a session that opened a tab, navigated and read the page showed nothing for its first several actions. Opening a tab, navigating, reading text or HTML and running a script now show it too; after a navigation it is shown again once the new page has loaded.
- **`create_window(focused=False)` now leaves focus where it was.** Firefox focuses every new window and ignores `focused: false`, so the window opened in front regardless. The extension now hands focus back to the window that had it, and the result says `(focused)` when the new window has focus.
- **`update_window(focused=False)` is refused instead of ignored.** Firefox has no way to take focus away from a window and drops the flag without an error. The tool now says so and points to `focus_window`.
- **`tabs_move` refuses a tab ID that is not a whole, non-negative number.** It turned `1.5` into tab 1 and passed `-1` and `true` on to Firefox; all three are now reported as an invalid tab ID.
- **A request monitor started while an older one is running no longer reads zero.** The extension keeps one record per request id, shared by every monitor that matches it, and a single flag on that record meant the first monitor to match claimed the request outright. Every later monitor listed nothing for as long as the older one stayed active, and a monitor only goes away when `requests_stop_monitoring` names it. What a client sees is a monitor that captures nothing, and since the natural response is to start another one, it then captures nothing however many times it retries. Each monitor now tracks the requests it has listed for itself, so a request matching three monitors appears in all three, which is what [`docs/web-request-monitoring.md`](docs/web-request-monitoring.md#url-patterns) already described. Reported in [issue #7](https://github.com/ThinkerYzu/foxmcp/issues/7).
- **`<all_urls>` as a URL pattern now matches every request instead of none.** Patterns here are globs, not WebExtensions match patterns, so `<all_urls>` carried no wildcard and was tested against each URL as the literal string it is. It is the spelling anyone who has written a WebExtension reaches for first, and it failed silently: a monitor that starts, reports itself active, and never captures anything. It is special-cased alongside the bare `*` now.
- **One press of Reconnect no longer leaves the extension cycling its connection.** The popup's **Reconnect** button and its settings save both close the WebSocket and open a new one, but `onclose` could not tell a deliberate close from a dropped connection, so it scheduled a reconnect as well. That timer then closed the healthy socket it found and opened another, whose close scheduled the next: a connect/close cycle every **Retry Interval**, 5 seconds by default, that ran until the browser was restarted. Nothing stopped it, because the 50-attempt ceiling counts from the last successful connection and every cycle connected successfully. The visible cost is on the server, which logs a connection open and closed every few seconds, and in tool calls that land in the gap between two sockets and fail or time out. A close now only schedules a reconnect if the socket that closed is still the live one.

### Changed
- **A request monitor now ends with whoever started it.** Monitors live in the extension, which outlives every server and every client, and only the client that started one knows its `monitor_id` - so a monitor left behind could never be read or stopped again while it went on holding the extension's `webRequest` listeners up and filtering response bodies for a reader that was gone. Two things end one now, on top of `requests_stop_monitoring`: the MCP client that started it disconnecting, which the server notices within a few seconds and stops that client's monitors and no one else's; and the extension losing its connection to the server, which clears every monitor it holds along with their captured data. Under `--stdio` those are the same event, since the server lives and dies with its client. `requests_list_captured` answers `MONITOR_NOT_FOUND` for an id that has been cleared rather than an empty list, because an empty list is also what a healthy monitor returns before anything has completed. As a backstop, a monitor named in anything the extension says that no client owns is stopped on sight, which covers the two halves drifting apart - an extension too old to clear its monitors when a connection ends, against a server that expects it to. See [`docs/web-request-monitoring.md`](docs/web-request-monitoring.md#how-long-a-monitor-lives).
- **`navigation_reload` and `requests_list_captured` say when their results are ready.** Both tool descriptions now name the trap behind the fixes above: `navigation_reload` returns when the reload is issued rather than when the page has loaded, and a request reaches `requests_list_captured` only once it has completed, so listing immediately after a navigation returns an empty list from a monitor that is working.

## [1.3.0] - 2026-09-07

### Added
- **`--stdio` serves MCP on stdin and stdout, so a client can launch the server itself.** Most MCP clients — Claude Code and Claude Desktop among them — start their servers as child processes and speak to them over pipes; FoxMCP only ever listened on HTTP, so it had to be started by hand and left running, which is what [issue #3](https://github.com/ThinkerYzu/foxmcp/issues/3) asked about. `claude mcp add --scope project foxmcp -- /path/to/venv/bin/python /path/to/server/server.py --stdio` is now enough, and the server lives and dies with the client. The extension side is unchanged: the WebSocket server still listens on 8765 and every tool works as it does over HTTP. Since stdout now carries the JSON-RPC framing, all logging goes to stderr and nothing in `server/` may `print()`. `--stdio` with `--mcp-port` or `--no-mcp` is refused rather than silently ignored, and a server that cannot bind the extension's port says which port and why, then exits non-zero — only one server can serve the extension, and in stdio mode every client launches one. Two costs come with the mode, and both follow from the extension being the side that opens the WebSocket connection. Only one server can serve it, and under stdio every client launches its own, so two `claude` sessions at once leaves the second without browser tools. And tying the server's life to a client leaves a gap at each restart, which the extension gives up on after roughly four minutes with no server to reach. See [`docs/configuration.md`](docs/configuration.md#serving-mcp-over-stdio), [One session at a time](docs/configuration.md#one-session-at-a-time) and [The reconnect gap](docs/configuration.md#the-reconnect-gap) for what to set and when to prefer HTTP.
- **`--enable-tools` keeps one tool out of a group you disabled.** Disabling a group is all-or-nothing, and sometimes a single tool in it is the one worth its tokens: [issue #6](https://github.com/ThinkerYzu/foxmcp/issues/6) asked for a screenshot of the page without the five tab tools that manage tabs, for driving a single-page app. `python server/server.py --disable-tools tabs --enable-tools tabs_capture_screenshot` now does exactly that, and `FOXMCP_ENABLE_TOOLS` does the same for clients that launch the server through a wrapper. The names are the ones the client sees; one that matches no tool is an error, since the symptom would otherwise be the one tool you asked for quietly missing. `tabs_capture_screenshot` is worth naming as the example because it captures the visible tab and takes no tab ID, so it stands on its own — most other tools take a `tab_id` that in practice comes from `tabs_list`. See [`docs/configuration.md`](docs/configuration.md#keeping-one-tool-out-of-a-disabled-group).

## [1.2.0] - 2026-08-14

**This release removes `get_last_focused_window` and seven protocol action names.**
No caller can observe a behavior difference from the tool removal — the tool it
duplicated returns the same window — but a client that names it by hand now gets an
error. See **Removed** below.

### Security
- **Extension WebSocket now accepts only `moz-extension://` origins.** Previously any inbound connection was accepted as the extension. WebSocket handshakes are exempt from the same-origin policy and are never preflighted, so any web page the user visited could connect to the localhost port, displace the real extension — an arriving connection closes the existing one — and answer requests on its behalf with content of its choosing. Non-extension connections are now rejected with a 403 during the handshake, and logged.

  Test clients that stand in for the extension must send an extension origin; use `connect_as_extension()` from `tests/test_config.py`.

### Added
- **`--disable-tools` leaves a tool group unregistered, to keep it out of an MCP client's context.** Every tool description a client is offered stays in its context for the whole session whether or not the tool is ever called, and the full set costs roughly 4,700 tokens — the concern raised in [issue #4](https://github.com/ThinkerYzu/foxmcp/issues/4). The groups are `windows`, `tabs`, `bookmarks`, `navigation`, `content`, `requests`, `history` and `debug`, and all of them remain on by default, so nothing changes for an existing setup. `python server/server.py --disable-tools bookmarks,history` drops nine tools and about 1,000 tokens; `FOXMCP_DISABLE_TOOLS` does the same for clients that launch the server through a wrapper whose arguments you cannot control. An unrecognized group name is an error rather than a warning, because a typo that quietly left the group enabled would defeat the point. See [`docs/configuration.md`](docs/configuration.md#reducing-the-tool-surface) for the per-group cost.
- **`tabs_move` — move tabs to a new position, or into another window.** Takes one tab ID, a list, or a JSON string of IDs, with `index` as the destination (`-1` for the end) and an optional `window_id`. Together with `create_window` this covers gathering the tabs for one site into a window of their own, which was the request in [issue #2](https://github.com/ThinkerYzu/foxmcp/issues/2) — see [`docs/api-reference.md`](docs/api-reference.md#gathering-tabs-into-their-own-window). Firefox refuses some moves without reporting an error, so the result says `Moved {n} of {m}` rather than claiming success.
- **`tabs_list` takes a `window_id`, and reports where each tab sits.** The parameter that was supposed to scope the listing never worked — a `|| true` in the extension made the current-window filter unconditional, so tabs in other windows could not be listed at all, despite the tool documenting itself as listing every tab. An unscoped call now spans every window, as documented, and each line ends with `[window {id}, index {n}]`.
- **A regression test for the MCP port's web-unreachability.** The port has no authentication, and what keeps web pages off it — absent CORS headers, content-type enforcement, and a session id the browser cannot read — is behavior of `fastmcp`/uvicorn rather than of FoxMCP. `tests/integration/test_mcp_port_not_web_reachable.py` asserts all three, so a dependency upgrade that re-opens the port fails the suite instead of passing quietly. No CORS policy was added: the door is already shut, and hand-rolling one risks breaking legitimate MCP clients.
- **Releases are built in public, and say where they came from.** Previously the XPI and server zip were built on the maintainer's machine and uploaded, so installing them meant taking his word that they matched the source — the concern in [issue #1](https://github.com/ThinkerYzu/foxmcp/issues/1). Pushing a `v*` tag now builds them on GitHub Actions from the tagged commit and publishes a signed provenance statement alongside them, which anyone can check without trusting us: `gh attestation verify 'foxmcp@codemud.org.xpi' --repo ThinkerYzu/foxmcp`. `SHA256SUMS` ships as a release asset too. The copy served by addons.mozilla.org is not covered, because Mozilla re-signs the file when it accepts a submission; verify the GitHub asset instead.
- **Audit logging for predefined scripts.** `content_execute_predefined` is the one tool that runs a program on the host, and it previously logged nothing at all — a refused path-traversal attempt and a routine call were equally invisible. Each invocation now logs the script name, arguments and tab at `INFO`, followed by the size of the generated JavaScript and the URL it ran against; every refusal and failure logs at `WARNING`. Arguments are truncated at 120 characters, and the generated JavaScript is never written to the log. See [`docs/scripts.md`](docs/scripts.md#what-gets-logged).

### Removed
- **`release-commands.sh`, the pre-CI release script.** Since 1.2.0 a pushed tag is the entire release, so the script had no job left — but it was worse than idle: it ran `git add -A`, committed the working tree under a "Release v1.1.0" message, and tried to create the `v1.1.0` tag that already exists. It also printed installation instructions that stopped working long ago (`pip install -r requirements.txt`, a repository URL that does not resolve). Everything it did is now in `.github/workflows/release.yml` and `scripts/release-notes.sh`. Nothing referenced it.
- **Seven protocol action names the extension accepted but nothing sent.** Five were old spellings kept as fall-through aliases beside the names that replaced them — `bookmarks.remove`, `content.execute`, `content.html`, `content.text` and `navigation.go`, alongside `bookmarks.delete`, `content.execute_script`, `content.get_html`, `content.get_text` and `navigation.go_to_url`. No tool had sent the old names since before v1.0.0, and their only remaining mention was an unreferenced dispatch table in the test harness, since deleted. The sixth, `tabs.active`, had its own handler returning the active tab of the current window; `tabs_list` reaches the same `browser.tabs.query` and reports the `active` flag, so it added nothing. The seventh, `test.create_test_tabs`, was a test fixture that bulk-created tabs by going around the MCP layer; its only caller was replaced by `tabs_create` in September 2025 — which exercises the layer under test instead of bypassing it — and the handler was left behind. It is the only `test.*` action that never had a method on the server side, and no assertion has ever covered its result. `tabs.update` is deliberately kept: nothing else can change a tab's URL or pinned state in place.
- **`get_last_focused_window`, which could never have returned anything different from `get_current_window`.** Firefox's `ext-windows.js` reads `context.currentWindow || windowTracker.topWindow` for `windows.getCurrent()` and plain `windowTracker.topWindow` for `windows.getLastFocused()`, and `ExtensionParent.sys.mjs` returns `undefined` for `currentWindow` whenever the calling context's `viewType` is `"background"` — the only context this extension has. Both calls therefore evaluated the same expression, and the two tools have returned the same window since v1.0.0. Use `get_current_window`; its description now says it is also the last focused window. Spotted by the reporter of [issue #4](https://github.com/ThinkerYzu/foxmcp/issues/4), who proposed merging the two behind a fallback — the fallback turned out to already be inside `getCurrent`, and to be its only reachable branch here. The `windows.get_last_focused` protocol action is gone with it.

### Fixed
- **`debug_websocket_status` could never report a connection.** It looked for a `connected_clients` collection on the server, which `FoxMCPServer` has never had — it holds a single `extension_connection`, since an arriving extension displaces the previous one — so the tool answered "WebSocket server doesn't track connected clients" whether or not the extension was connected. That is the tool you reach for when the connection is what you doubt, and every other tool answers a missing extension with a 30-second timeout. It now says whether an extension is connected and from which address, and distinguishes "never connected" from "the last connection has closed". Found by installing the extension into a real Firefox profile by hand and asking.
- **`make run-server` ran whatever `python` was on PATH.** Every other Makefile target runs out of `venv/bin`, but this one did not, so on a machine where `python` is another virtual environment — or is not installed, which is normal now that distributions ship only `python3` — starting the server failed on an import of a dependency that was installed all along. `package.json`'s `start` script had the same bug.
- **`debug_websocket_status` was registered by the history group.** Nothing depended on this while every tool was always registered, but it meant the tool you reach for when the connection looks wrong would have disappeared along with an unrelated group. It has its own group now.
- **Response body capture never captured anything.** The extension tried to read bodies by overriding `window.fetch` and `XMLHttpRequest` from a content script, and the override silently did nothing: a content script's `window` is an Xray wrapper, so the assignment landed nowhere and the page kept the native `fetch`. `requests_get_content` therefore returned `included: false` for every request ever made. Bodies now come from `webRequest.filterResponseData`, which taps the response stream in the background script — so the document load is captured too, which the old approach could never have managed, and Firefox has already undone any `Content-Encoding` by the time the bytes arrive. This adds the `webRequestBlocking` permission, which Firefox requires for `filterResponseData`; no listener blocks, cancels or rewrites a request, and the permission carries no new install prompt. At most 200 bodies are held at a time, since a monitor on `*` sees every request the browser makes.
- **`size_bytes` reported 0 for responses whose size was simply unknown.** It was read from `Content-Length`, which compressed HTTP/2 responses routinely omit, with a fallback to `details.responseSize` that returns 0 on Beta and Developer Edition while returning the real figure on Nightly — which is why `test_response_body_capture_verification` passed on the maintainer's machine and failed on CI with no code difference between them. The value is now the bytes actually delivered, counted while the body streams through, and it is correct even when the body itself is withheld for its content type or cut short by `max_body_size`. It is `null`, never 0, when the response could not be tapped at all.
- **A response body, once captured, could not be found again.** When `requests_get_content` had no body stored under the request id it fell back to matching on URL, method and status, comparing against `requestDetail.response_status_code` — a field nothing in the extension ever sets, so the comparison could not succeed. The fallback is gone; bodies are stored under the `webRequest` request id that `requests_list_captured` reports.
- **Tool documentation that MCP clients could not see.** FastMCP builds a tool's description from the docstring summary and body and maps `Args:` onto the input schema, but **discards `Returns:`** — and five tools kept facts a caller needs there. `bookmarks_list` and `bookmarks_search` showed three words each while the ID-and-parent-ID convention that makes `bookmarks_update`/`_delete`/`_create` usable was invisible; `bookmarks_search` never mentioned that it matches bookmarks and never folders. `tabs_capture_screenshot` did not say that its return type changes shape depending on `filename`. `requests_start_monitoring` did not say the response carries the `monitor_id` that its three companion tools require. Those facts are now in the description or on the argument. Twelve other tools have a `Returns:` line that only restates the summary and were left alone.
- **`history_delete_item` raised `NameError` instead of confirming the delete.** Both the success line and the fallback line interpolated `params.url`, but the tool takes `url` directly and has no `params` — a leftover from the Pydantic-model signature the neighbouring navigation tools still use. The delete itself went through, then the tool crashed reporting it, so the caller saw a traceback for work that had already succeeded. Only the error path was under test, which is how it shipped.
- **Release archives kept files that had been deleted from the source tree.** `zip` updates an existing archive rather than replacing it, so once a file entered `foxmcp@codemud.org.xpi` it stayed there through every later build until someone ran `make clean`; the server staging directory was copied into without being cleared and had the same problem. Both are now emptied before each build. `__pycache__` from a local run is also dropped from the server archive, so a build here and a build on CI produce the same contents.

### Documentation
- **The install-from-source instructions did not work.** README told the reader to `pip install -r requirements.txt`, and there is no `requirements.txt` at the repository root — the dependencies live in `server/requirements.txt` and `tests/requirements.txt`. It now says `make install`, which creates `venv/` and installs the server dependencies into it, verified from a fresh clone.
- **Python 3.10 is the floor, and the installer now says so.** `fastmcp` 3 requires it, while `scripts/install-from-github.sh` accepted 3.8 and 3.9 and left pip to fail afterwards with an error that never mentions the Python version. `package.json` claimed `>=3.8` for the same reason. Development and CI run 3.14.
- **Installing an unsigned XPI does not work on release or beta Firefox.** Both enforce extension signing and ignore `xpinstall.signatures.required`; only Nightly, Developer Edition and unbranded builds honour it. README presented the two persistent install methods as if they worked everywhere. They are now marked for what they are, with addons.mozilla.org named as the route for everyone else.
- **`docs/web-request-monitoring.md` documented about twenty tools that were never built** — search, retention policies, storage quotas, session save and load — because the file was an implementation plan presented as reference documentation. It is now a reference for the four tools that exist, covering the pattern syntax, which options do something, where captured data lives and how long it survives.
- **Five parameters are accepted and ignored, and now say so.** `save_request_body_to` and `save_response_body_to` never wrote a file; `include_binary` never changed what came back; `drain_timeout` never delayed a stop; and of the monitoring options, `capture_request_bodies` and `sensitive_headers` are read by nothing. The tool descriptions and the reference docs mark each one rather than leaving a caller to conclude the feature is broken. `requests_get_content` also always returns an empty `request_headers`, which is now documented; no listener collects them.
- **`docs/protocol.md` never documented the `requests.*` messages**, though it presents itself as the complete wire format and the feature shipped in v1.1.0. All four — `start_monitoring`, `list_captured`, `get_content`, `stop_monitoring` — are there now, with their error codes.
- **Neither install path listed its system prerequisites, and both fail without them on a stock Ubuntu.** `make`, `zip` and Python's `venv` module are all absent from a fresh Ubuntu 24.04, so `make install` stopped at "The virtual environment was not created successfully" and `make package` could not build the XPI. README now names them. `scripts/install-from-github.sh` checked for `curl`, `unzip` and `python3` but not for the venv module it uses immediately afterwards: on a machine without `python3-venv` it downloaded and unpacked the whole release, then died on a Python error with no mention of the package to install, leaving five directories and a broken `venv/` behind. It now probes venv creation up front and exits with the apt command to run, before downloading anything.
- **`scripts/install-xpi.sh` told users the extension "should be automatically enabled". It is not.** Firefox installs an extension dropped into a profile from outside the browser in the disabled state and waits for a person to enable it in `about:addons` — verified by installing into a fresh profile and watching the extension sit at `userDisabled: true` while the server logged no connection. The script now says so, as do README's Method 3 and `docs/development.md`; `install-from-github.sh` already did.
- **The repository URL in `package.json` and the extension manifest pointed at a repository that does not exist** (`github.com/foxmcp/foxmcp` rather than `github.com/ThinkerYzu/foxmcp`).

## [1.1.0] - 2025-10-26

### Added
- **Web Request Monitoring API**: Foundation for capturing and inspecting network requests
  - `requests_start_monitoring()`: Start capturing web requests/responses
  - `requests_list_captured()`: Retrieve captured request data
  - `requests_stop_monitoring()`: Stop monitoring and cleanup
  - Two-phase workflow: start monitoring → capture data → stop monitoring
  - Extension integration with comprehensive error handling

- **Bookmark Management Enhancements**
  - Bookmark folder creation support with proper hierarchy
  - Bookmark update functionality for modifying existing bookmarks
  - Enhanced test infrastructure for bookmark operations

- **Predefined Scripts**: Ready-to-use automation scripts
  - `youtube-play-pause.sh`: Control YouTube video playback (play/pause/toggle)
  - `dom-summarize.sh`: Simplify DOM tree for AI agent understanding
  - `gcal-cal-event-js.sh`: Extract specific Google Calendar event details
  - `gcal-daily-events-js.sh`: Get all events for a specific day
  - `gcal-monthly-events-js.sh`: Extract entire month view from calendar
  - All scripts include comprehensive documentation and usage examples

- **Content API Enhancement**
  - Optional `max_length` parameter for `content_get_text` tool
  - Allows truncation of large text extractions for AI context management

- **Installation Options**
  - Firefox Add-ons store installation option documented
  - Simplified installation instructions to single command
  - Updated installation script to include all predefined scripts

- **Test Coverage**: Added comprehensive test for history query filtering
  - Verifies that non-matching entries are excluded from filtered results
  - Tests multiple distinct search terms to ensure proper filtering behavior

- **Timestamp Validation**: Added comprehensive history timestamp validation
  - New `validate_history_item_timestamp()` helper function ensures timestamps are valid
  - Validates timestamp presence, format, range, and reasonableness
  - All history tests now verify timestamps are non-null, positive, and within valid date ranges
  - End-to-end test confirms MCP tools display actual timestamps to AI agents

### Fixed
- **History Query Filtering**: Fixed parameter name mismatch that prevented history search filtering from working correctly
  - Extension now correctly reads `query` parameter (was reading non-existent `text` parameter)
  - History searches now properly filter results based on search query
  - Non-matching entries are correctly excluded from search results
  - All tests updated to use correct `query` parameter per protocol specification

- **History Timestamp Display**: Fixed MCP tools showing "Unknown time" for history items
  - MCP tools now correctly read `lastVisitTime` field (was reading non-existent `visitTime` field)
  - AI agents now see actual timestamps in milliseconds since epoch
  - Affects `history_query` and `history_get_recent` MCP tools
  - Documentation updated to reflect correct field name and format

- **Bookmark Management**: Fixed bookmark management integration test failures
  - Improved test reliability and error handling
  - Enhanced test infrastructure for bookmark operations

- **Google Calendar Scripts**: Fixed gcal-daily-events-js.sh to get current month correctly
  - Script now accurately determines the current month/year
  - Improved date handling for calendar automation

### Changed
- **Infrastructure**: Clear profile cache after packaging extension
  - Ensures clean state for extension testing
  - Prevents stale profile data from affecting tests

- **Documentation**: Enhanced CLAUDE.md with comprehensive instructions
  - Added ENABLE_DEBUG_LOGGING_TO_SERVER debugging documentation
  - Documented all available predefined scripts with usage examples
  - Updated installation instructions with predefined scripts

### Developer Experience
- 211 total tests passing (59 unit + 152 integration)
- All tests enabled and maintaining 100% pass rate
- Enhanced debugging capabilities with configurable logging
- Improved developer documentation and troubleshooting guides

## [1.0.0] - 2024-09-28

### Added
- **Complete Browser Automation**: Full Firefox browser control via MCP protocol
  - Tab management (list, create, close, switch, take screenshots)
  - Window management (list, create, close, focus, resize)
  - Navigation control (back, forward, reload, go to URL)
  - Content access (extract text, HTML, execute JavaScript)
  - History operations (query, search, delete)
  - Bookmark management (list, search, create, delete with folder support)

- **MCP Protocol Integration**: 25+ tools accessible via FastMCP server
  - Dual server architecture (WebSocket + MCP endpoints)
  - Complete request/response correlation with UUID tracking
  - Automatic reconnection and timeout handling
  - Comprehensive error handling and validation

- **Firefox Extension**: Complete WebExtensions-based implementation
  - Background script with persistent WebSocket connection
  - Content scripts for page interaction
  - Popup interface with connection status and configuration
  - Options page for server settings and preferences
  - Storage.sync integration for cross-browser preference sync

- **Development Infrastructure**: Comprehensive build and test system
  - 171 tests (29 unit + 142 integration) with 100% coverage
  - Automated test environment with Firefox integration
  - Dynamic port allocation for test isolation
  - Robust test import system with symbolic links
  - Complete Makefile with development workflow

- **Installation Tools**: Automated setup and deployment
  - Automated extension installation script (`scripts/install-xpi.sh`)
  - Firefox preference configuration (unsigned extension support)
  - Virtual environment setup and dependency management
  - Cross-platform compatibility (Linux, macOS, Windows)

- **Documentation**: Comprehensive guides and references
  - Complete API reference with all 25+ MCP tools
  - Development guide with setup and workflow instructions
  - Architecture documentation with system design
  - Protocol specification with WebSocket message formats
  - Configuration guide for server and extension setup
  - Custom scripts documentation with examples

- **Claude Code Integration**: Ready-to-use MCP client support
  - Example CLAUDE.md templates for script creation assistance
  - Predefined external script system with parameterized execution
  - Browser automation workflow integration
  - Context-aware script development support

- **Security Features**: Robust security model
  - Localhost-only server binding with security enforcement
  - Input validation and sanitization
  - Secure script execution with path validation
  - Minimal required permissions model

### Technical Details
- **WebSocket Protocol**: JSON-based bidirectional communication
- **MCP Server**: FastMCP-powered HTTP server on port 3000
- **WebSocket Server**: Extension communication on port 8765
- **Extension ID**: `foxmcp@codemud.org`
- **Supported Firefox**: All recent versions with WebExtensions support
- **Python Requirements**: Python 3.8+ with asyncio support

### Package Distribution
- **Firefox Extension**: `dist/packages/foxmcp@codemud.org.xpi`
- **Server Package**: `dist/packages/foxmcp-server.zip`
- **Source Code**: Complete repository with build system
- **License**: MIT License with proper attribution

### Performance & Reliability
- **Test Coverage**: 171 automated tests covering all functionality
- **Error Handling**: Comprehensive error recovery and reporting
- **Resource Management**: Proper cleanup and memory management
- **Connection Stability**: Automatic reconnection and health monitoring
- **Cross-Platform**: Tested on Linux, macOS, and Windows

### Initial Release Scope
This v1.0.0 release represents a complete, production-ready browser automation solution that enables AI assistants and automation tools to control Firefox browsers through the standardized Model Context Protocol (MCP).

[1.3.0]: https://github.com/ThinkerYzu/foxmcp/releases/tag/v1.3.0
[1.2.0]: https://github.com/ThinkerYzu/foxmcp/releases/tag/v1.2.0
[1.1.0]: https://github.com/ThinkerYzu/foxmcp/releases/tag/v1.1.0
[1.0.0]: https://github.com/ThinkerYzu/foxmcp/releases/tag/v1.0.0
