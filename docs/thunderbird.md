# Thunderbird

The Thunderbird add-on lives in [`thunderbird/`](../thunderbird/). It connects to
the same MCP server as the Firefox add-on and shows up in `browsers_list` marked
`[Thunderbird]`. It needs Thunderbird 128 or later.

## What it can do

- `thunderbird_capture_window`: a screenshot of the whole main window, mail view
  and add-on tabs included. `tabs_capture_screenshot` cannot see the mail view,
  because it is part of Thunderbird's own interface, not a content tab.
- `thunderbird_snapshot`: the buttons, tabs, folders, message rows and fields in
  the main window, each with a ref. `thunderbird_click` clicks one by ref or by
  its visible text (double-click opens a message, right-click opens its menu),
  `thunderbird_type` types into a field, optionally pressing Enter, and
  `thunderbird_press_key` presses Enter, Escape, Tab, Space, the arrows,
  PageUp/PageDown or Home/End on whatever has focus. Delete and Backspace are
  refused, since in the message list they move mail to the trash. These need
  **Let the agent click** in the add-on's options. An orange cursor shows each
  action and stays until the server disconnects or 2 minutes pass without one.
  Add-on tabs run in their own process, so their content does not show in the
  snapshot: `thunderbird_click` takes `x` and `y` from a window capture there
  (with `hover` to only move the mouse), sent as widget-level mouse events. Your
  own mouse pointer does not move.
- `thunderbird_console`: recent errors and warnings from Thunderbird and its
  add-ons, and console calls from Thunderbird's own code, filtered by age, text
  and level. Add-on pages run in another process, so their console.log calls do
  not show; their uncaught errors do.
- `thunderbird_update_addon`: reinstalls an add-on from a local .xpi. Only an
  add-on that is already installed and unsigned can be updated, so nothing new is
  added and add-ons from addons.thunderbird.net are left alone. Needs **Let the
  agent update add-ons** in the options. Updating this add-on itself restarts it,
  and it reconnects within about 30 seconds.
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
installed from a file. It has fixed operations only: draw the main window, list,
click and type into its own interface elements, click at a point of the window, press the allowed keys, read the
console, update an installed unsigned add-on, and read the one token file. There is no way to run arbitrary code through it.
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
