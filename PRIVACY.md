# Privacy Policy: MCP Bridge for Firefox

Last updated: 2026-11-09

MCP Bridge for Firefox connects your browser to an AI client that you install and run yourself. This page explains what the add-on reads, where it sends it, and what it never does.

## What the add-on reads

Only when your AI client asks for it through one of the add-on's tools, the add-on can read:

- **Website content:** the text, HTML and interactive elements of a page, and screenshots of the visible tab.
- **Browsing activity:** the URLs and titles of open tabs and windows, and your browsing history.
- **Bookmarks:** your bookmarks and bookmark folders.
- **Website activity:** network requests made by pages, including request bodies and response headers, when request monitoring is turned on.
- **Authentication information:** request bodies and response headers can contain sign-in data such as form submissions and cookies, when request monitoring is turned on.

It can also act on pages: click, type, press keys, upload files you point it at, navigate, and manage tabs, bookmarks and history.

## Where it goes

The add-on sends this data over a WebSocket to the MCP server running **on your own computer** (`localhost`, port 8765 by default). The server refuses connections that come from web pages, so only the add-on can reach it. To pair with that server, the add-on also asks a small helper the server installs on your computer for the token in `~/.foxmcp/pairing-token`, and sends the helper nothing else. The server passes the data to the AI client you connected (for example Claude Code or Cursor), and that client may send it to its AI provider under that provider's own terms.

## Error reports (off unless you turn them on)

From version 1.17.0, "Send error reports" in the add-on's options tells the developer when the add-on fails. It is off by default, and Firefox asks for your permission when you turn it on. You can withdraw that permission in `about:addons` at any time, and reports stop.

A report goes to the developer's own GlitchTip server at `glitchtip.dorzairi.com`, self-hosted on the developer's hardware behind Cloudflare, which carries the traffic and so handles the report and your IP address on the way through. No other company receives it. A report contains only:

- the add-on version;
- for an action that failed: which action it was (for example `tabs.captureVisibleTab`) and its error code (for example `CAPTURE_ERROR`);
- for a crash: the type of error and where in the add-on's own code it happened (file names, line numbers and function names).

A report never contains an error message, a page's content, a URL or page title, a file path, anything the agent sent, or anything that identifies you or your device. Like any request, it reaches the server with your IP address; the add-on does not put it in the report, and GlitchTip keeps it only with the last part removed (for example `203.0.113.0`). At most 20 reports are sent each time Firefox starts, and the same failure is sent only once.

## What we never do

- We do not run any server that receives your browsing data. With error reports off, the developer of this add-on receives nothing.
- No analytics, tracking or advertising.
- Nothing is sold or shared with anyone by the add-on.

## Storage

The add-on stores only its own settings (server address, port, retry options) in Firefox's extension storage. Request monitoring keeps captured requests in memory until monitoring stops or the connection to the server drops.

## Your control

- The add-on does nothing while no MCP server is running.
- Remove the add-on from `about:addons` to stop all access.
- Use a separate Firefox profile if you want to keep agent work away from your personal browsing.

## Contact

Questions: open an issue at https://github.com/MashdorDev/mcp-bridge-for-firefox/issues
