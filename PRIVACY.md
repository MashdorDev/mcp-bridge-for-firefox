# Privacy Policy: MCP Bridge for Firefox

Last updated: 2026-11-02

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

## What we never do

- We do not run any server that receives your data. The developer of this add-on receives nothing.
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
