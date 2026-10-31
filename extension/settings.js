/*
 * MCP Bridge for Firefox - the settings a person can change, and their defaults
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Loaded by the background script, the content script, the popup and the
 * options page, so all four agree on names and defaults. Every value lives in
 * storage.local, per Firefox profile: Firefox Sync would otherwise copy one
 * profile's choices (its name, its blocked sites) to every other.
 */

const SETTINGS_DEFAULTS = {
  // Agent control
  paused: false,
  allowRead: true,
  allowInteract: true,
  allowScripts: true,
  allowUploads: true,
  allowClipboard: true,
  allowTabs: true,
  allowWindows: true,
  allowHistory: true,
  allowBookmarks: true,
  allowNetwork: true,
  blockedSites: '',
  allowPrivate: false,
  followAgent: false,
  releaseAfterMinutes: 5,

  // On the page
  overlayEnabled: true,
  overlayBorder: true,
  overlayLabel: true,
  labelPosition: 'bottom',
  cursorEnabled: true,
  cursorMotion: 'human',
  cursorSpeed: 'normal',
  cursorIdleMotion: true,
  typingAnimated: true,
  tabMarker: true,
  overlayColor: '#d97757',

  // Toolbar and connection
  badgeEnabled: true,
  browserName: '',
  extraServerPorts: 4,
  // Real (trusted) clicks and keys through WebDriver BiDi, when Firefox was
  // started with --remote-debugging-port. The server finds the port; set
  // realInputPort only for one outside 9222-9229 (0: find it)
  realInput: true,
  realInputPort: 0,
  // Optional: the server's ~/.foxmcp/pairing-token. When set, this browser
  // answers only a server that proves it holds the same token.
  pairingToken: '',
};

// Which permission each action needs. Actions not listed (ping, tabs.release,
// the test suite's helpers) are always allowed.
const ACTION_PERMISSIONS = {
  allowRead: ['content.get_text', 'content.get_html', 'content.snapshot', 'tabs.captureVisibleTab'],
  allowInteract: ['content.click', 'content.type', 'content.press_key', 'content.capture_file_input',
    'content.real_input_prep', 'content.check_typed', 'content.real_pointer_prep', 'content.scroll', 'content.hover', 'content.drag',
    'content.click_effect', 'content.real_input_arrived'],
  allowScripts: ['content.execute_script'],
  allowUploads: ['content.upload_begin', 'content.upload_chunk', 'content.upload_finish'],
  allowClipboard: ['content.copy_image', 'content.paste', 'content.clipboard_fingerprint'],
  allowTabs: ['tabs.list', 'tabs.create', 'tabs.close', 'tabs.switch', 'tabs.move',
    'navigation.go_to_url', 'navigation.back', 'navigation.forward', 'navigation.reload'],
  allowWindows: ['windows.list', 'windows.get', 'windows.get_current', 'windows.create', 'windows.close',
    'windows.focus', 'windows.update', 'tabs.group', 'tabs.ungroup', 'tabGroups.query', 'tabGroups.update'],
  allowHistory: ['history.query', 'history.recent', 'history.delete_item'],
  allowBookmarks: ['bookmarks.list', 'bookmarks.search', 'bookmarks.create', 'bookmarks.createFolder',
    'bookmarks.update', 'bookmarks.delete'],
  allowNetwork: ['requests.start_monitoring', 'requests.list_captured', 'requests.get_content',
    'requests.stop_monitoring'],
};

// What each permission is called on the options page and in refusals
const PERMISSION_NAMES = {
  allowRead: 'Read pages and take screenshots',
  allowInteract: 'Click, type and press keys',
  allowScripts: 'Run scripts in pages',
  allowUploads: 'Put files on upload fields',
  allowClipboard: 'Copy images and paste',
  allowTabs: 'Open, close, switch and navigate tabs',
  allowWindows: 'Manage windows and tab groups',
  allowHistory: 'Read and delete history',
  allowBookmarks: 'Read and change bookmarks',
  allowNetwork: 'Watch network requests',
};

const CURSOR_SPEEDS = { slow: 1.5, normal: 1, fast: 0.6 };

async function loadSettings() {
  return browser.storage.local.get(SETTINGS_DEFAULTS);
}

// "example.org" also blocks "www.example.org" and other subdomains; one site
// per line, and a full URL pasted in works too
function blockedSiteList(text) {
  return text.split(/[\s,]+/).map(entry => entry.trim().toLowerCase()
    .replace(/^[a-z]+:\/\//, '').replace(/[/:].*$/, '').replace(/^(\*\.|www\.)/, ''))
    .filter(Boolean);
}

function isBlockedHost(host, text) {
  host = (host || '').toLowerCase();
  return blockedSiteList(text).some(site => host === site || host.endsWith('.' + site));
}
