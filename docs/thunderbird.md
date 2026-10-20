# Thunderbird

The Thunderbird add-on lives in [`thunderbird/`](../thunderbird/). It connects to
the same MCP server as the Firefox add-on and shows up in `browsers_list` marked
`[Thunderbird]`. It needs Thunderbird 128 or later.

## What it can do

- `thunderbird_capture_window`: a screenshot of the whole main window, mail view
  and add-on tabs included. `tabs_capture_screenshot` cannot see the mail view,
  because it is part of Thunderbird's own interface, not a content tab.
- `mail_list_accounts`, `mail_list_folders`, `mail_list_messages`,
  `mail_search_messages` and `mail_get_message`: read-only mail access. Nothing
  sends, moves, deletes or marks mail.

Firefox tools (tabs, history, bookmarks and the rest) are refused while
Thunderbird is the active browser, with a note to `browsers_select` a Firefox
browser.

## Install

1. Build the XPI: zip the contents of `thunderbird/` (the files, not the folder).
2. In Thunderbird, open Add-ons and Themes, then the gear menu, then **Install
   Add-on From File**, and pick the XPI. Thunderbird installs add-ons without a
   signature. It asks for access to accounts and messages, and for full access,
   which the window capture needs.
3. Start or reconnect the MCP server. On its first start it creates
   `~/.foxmcp/pairing-token`.

The add-on reads that token file itself and pairs with every server on the
machine. Its options page shows each server's pairing state, and takes a token by
hand only if the server keeps its token somewhere else.

## How it works

The window capture and the token file read go through a small experiment API
(`thunderbird/experiments/mcpChrome/`), which Thunderbird allows in add-ons
installed from a file. It has fixed operations only: draw the main window, and
read the one token file. There is no way to run arbitrary code through it.
Experiments have no stability promise, so a Thunderbird update can break them.

Mail access uses Thunderbird's standard `accounts`, `folders` and `messages`
WebExtension APIs.

## Pairing

Any local program can listen on port 8765 before the server, or claim to be the
add-on. Thunderbird therefore answers nothing but `ping` until the server proves
it holds the token, and the server refuses Thunderbird tools until the add-on
proves it back. Both proofs are HMACs over a nonce from the other side, so the
token never crosses the socket. The Firefox add-on can pair the same way when a
token is pasted into its options; without one it works as before. Details:
[protocol.md](protocol.md#pairing).
