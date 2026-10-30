/*
 * FoxMCP Firefox Extension - Background Script
 * Copyright (c) 2024 FoxMCP Project
 * Licensed under the MIT License - see LICENSE file for details
 */

// SINGLE CONNECTION CONSTRAINT: Only one WebSocket connection to MCP server allowed
let websocket = null;
let isConnected = false;

// The person's settings (settings.js), kept current as they change them
let settings = { ...SETTINGS_DEFAULTS };
loadSettings().then(saved => { settings = saved; updateBadge(); });
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  for (const [key, change] of Object.entries(changes)) {
    if (key in SETTINGS_DEFAULTS) settings[key] = change.newValue ?? SETTINGS_DEFAULTS[key];
  }
  if ('paused' in changes || 'badgeEnabled' in changes) updateBadge();
  // A paused agent lets go of its tabs, so the person gets the page back
  if (changes.paused && changes.paused.newValue) {
    for (const tabId of [...agentTabs.keys()]) releaseAgentTab(tabId);
  }
});


// Debug logging configuration - set to true to send extension logs to server
const ENABLE_DEBUG_LOGGING_TO_SERVER = false;

// Enhanced console logging that sends to server when available
if (ENABLE_DEBUG_LOGGING_TO_SERVER) {
  const originalConsoleLog = console.log;
  const originalConsoleError = console.error;

  // Buffer to store logs before WebSocket connection is established
  let logBuffer = [];

  function createLogMessage(level, args) {
    return {
      id: `debug_${Date.now()}`,
      type: "debug_log",
      action: "extension.debug",
      data: {
        level: level,
        message: args.map(arg => typeof arg === 'object' ? JSON.stringify(arg) : String(arg)).join(' '),
        timestamp: new Date().toISOString()
      }
    };
  }

  function sendLogMessage(logMessage) {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
      try {
        websocket.send(JSON.stringify(logMessage));
        return true;
      } catch (e) {
        // Ignore errors sending debug logs
      }
    }
    return false;
  }

  function flushLogBuffer() {
    if (logBuffer.length > 0 && websocket && websocket.readyState === WebSocket.OPEN) {
      const bufferedLogs = [...logBuffer];
      logBuffer = [];
      bufferedLogs.forEach(logMessage => {
        sendLogMessage(logMessage);
      });
    }
  }

  function enhancedLog(...args) {
    // Always log to console
    originalConsoleLog(...args);

    const logMessage = createLogMessage("log", args);

    // Try to send immediately, otherwise buffer it
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      // Keep buffer size reasonable
      if (logBuffer.length > 100) {
        logBuffer = logBuffer.slice(-50);
      }
    }
  }

  function enhancedError(...args) {
    // Always log to console
    originalConsoleError(...args);

    const logMessage = createLogMessage("error", args);

    // Try to send immediately, otherwise buffer it
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      // Keep buffer size reasonable
      if (logBuffer.length > 100) {
        logBuffer = logBuffer.slice(-50);
      }
    }
  }

  // Replace console methods with enhanced versions
  console.log = enhancedLog;
  console.error = enhancedError;

  // Export flush function to be called when WebSocket connects
  window.flushDebugLogBuffer = flushLogBuffer;
}

// Default configuration - will be loaded from storage
let CONFIG = {
  hostname: 'localhost',
  port: 8765,
  retryInterval: 5000, // milliseconds (5 seconds default)
  maxRetries: -1, // -1 for infinite retries, or set a number
  pingTimeout: 5000 // ping timeout in milliseconds
};

let retryAttempts = 0;
// After this many attempts at retryInterval, retries slow to SLOW_RETRY_INTERVAL
// rather than stopping. Under --stdio the server is down whenever the MCP client
// is closed, and an extension that gave up would stay disconnected after the
// client came back. One localhost connection attempt every 30s costs nothing.
const FAST_RETRIES = 50;
const SLOW_RETRY_INTERVAL = 30000;
// The console gets a line when the connection comes up or drops, not one per
// failed attempt: with the server down, every attempt used to log four lines,
// and the add-on's console showed nothing else.
let loggedDown = false;

// Who this browser is, sent to the server on every connection so it can tell
// several Firefox profiles apart. Kept in storage.local, never storage.sync:
// Firefox Sync would copy a synced id or name to every profile on the account.
async function browserIdentity() {
  const stored = await browser.storage.local.get({ instanceId: '', browserName: '', realInput: true, realInputPort: 0 });
  let instanceId = stored.instanceId;
  if (!instanceId) {
    instanceId = crypto.randomUUID();
    await browser.storage.local.set({ instanceId });
  }
  return { instance_id: instanceId, name: stored.browserName, version: browser.runtime.getManifest().version,
    real_input: Boolean(stored.realInput), real_input_port: Number(stored.realInputPort) || 0 };
}

async function sendHello(socket) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const data = await browserIdentity();
  if (settings.pairingToken) {
    data.client_nonce = hexBytes(crypto.getRandomValues(new Uint8Array(16)));
    pairing.set(socket, { nonce: data.client_nonce, paired: false,
      problem: 'waiting for the server to prove the pairing token' });
  } else {
    pairing.delete(socket);
  }
  socket.send(JSON.stringify({
    id: `hello_${Date.now()}`,
    type: 'hello',
    action: 'browser.hello',
    data,
    timestamp: new Date().toISOString()
  }));
}

// Pairing, opt-in: with a token set in the options, this browser answers only
// a server that proves it holds the same token (pairing_proof in server.py),
// so another local program listening on the port first gets nothing.
const pairing = new WeakMap(); // socket -> { nonce, paired, problem }

const hexBytes = bytes => [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, '0')).join('');

async function pairingProof(role, nonce) {
  const encode = text => new TextEncoder().encode(text);
  const key = await crypto.subtle.importKey('raw', encode(settings.pairingToken.trim()),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hexBytes(await crypto.subtle.sign('HMAC', key, encode(`${role}|${nonce}`)));
}

async function handleHelloAck(socket, data) {
  const state = pairing.get(socket);
  if (!state) return;
  if (!data.proof) {
    state.problem = 'the server has no pairing token; update the MCP Bridge server';
  } else if (data.proof !== await pairingProof('server', state.nonce)) {
    state.problem = 'the server proved a different pairing token';
  } else {
    Object.assign(state, { paired: true, problem: '' });
    socket.send(JSON.stringify({ id: `auth_${Date.now()}`, type: 'auth', action: 'browser.auth',
      data: { proof: await pairingProof('client', data.server_nonce) }, timestamp: new Date().toISOString() }));
  }
}

browser.storage.onChanged.addListener((changes, area) => {
  // Every server this browser is connected to learns its new name or
  // real-input port, and pairs again with a new token
  if (area === 'local' && (changes.browserName || changes.realInput || changes.realInputPort || changes.pairingToken)) {
    sendHello(websocket);
    for (const socket of extraSockets.values()) sendHello(socket);
  }
});

function connectToMCPServer() {
  try {
    // IMPORTANT: Only one WebSocket connection is allowed at a time
    // Disconnect any existing connection first to prevent multiple connections
    if (websocket && websocket.readyState !== WebSocket.CLOSED) {
      console.log('🔌 Disconnecting existing connection before creating new one');
      disconnect();
    }

    // Compute WebSocket URL dynamically using current config
    const WS_URL = `ws://${CONFIG.hostname}:${CONFIG.port}`;
    console.debug(`Connecting to ${WS_URL} (attempt ${retryAttempts + 1})`);

    const socket = new WebSocket(WS_URL);
    websocket = socket;
    let opened = false;

    socket.onopen = () => {
      opened = true;
      loggedDown = false;
      console.log(`Connected to the MCP server on port ${CONFIG.port}`);
      isConnected = true;
      updateBadge();
      retryAttempts = 0; // Reset retry counter on successful connection
      connectionRetryAttempts = 0; // Reset connection retry counter
      sendHello(socket).catch(error => console.error('Could not send hello:', error));

      // Flush any buffered debug logs
      if (typeof window.flushDebugLogBuffer === 'function') {
        setTimeout(() => {
          window.flushDebugLogBuffer();
        }, 100);
      }
    };

    socket.onmessage = async (event) => {
      await handleMessage(JSON.parse(event.data), socket);
    };

    socket.onclose = (event) => {
      // Reconnect only if this socket is still the live one.
      //
      // close() fires this handler for a deliberate disconnect as well, and
      // every deliberate disconnect is already followed by a new connection.
      // Without the guard, that close schedules a reconnect whose timer then
      // closes the healthy socket it finds and opens another, whose close
      // schedules the next: a connect/close cycle every retryInterval that no
      // retry cap stops, since retryAttempts resets on each successful open.
      if (websocket !== socket) {
        return;
      }

      if (opened) {
        console.log(`Lost the MCP server on port ${CONFIG.port} (close code ${event.code}); reconnecting`);
      } else if (!loggedDown) {
        console.log(`No MCP server on port ${CONFIG.port} yet; retrying quietly until one starts`);
      }
      loggedDown = true;
      isConnected = false;
      updateBadge();
      clearAllMonitors('the connection to the server dropped');
      releaseAgentTabsOf(socket);
      scheduleReconnect();
    };

    // The error event carries nothing readable, and onclose follows it
    socket.onerror = () => {};
  } catch (error) {
    console.error('Failed to connect to MCP server:', error);
    scheduleReconnect();
  }
}

// More servers, one per MCP client: under --stdio every client launches its own,
// and a server that finds the configured port taken moves to one of the next
// ports (EXTRA_SERVER_PORTS in server.py; how many are scanned is a setting). Each gets a connection of its own here, so
// every Claude session can use the browser at the same time. Replies go back on
// the connection the request came in on (replySockets).
//
// Firefox opens only one WebSocket to a host at a time, and on Windows a probe
// of a localhost port nobody listens on takes seconds to fail. So the scan runs
// only while the main connection is up, and probes one port at a time; probing
// all of them at once queued the main connection behind them and it never got
// through.
const EXTRA_SERVER_SCAN_MS = 15000;
// A connection to one of them dropping is most often its session restarting
// the server (/mcp), so for a minute after that its port alone is tried every
// 3 s. With the full scan every 15 s, and each empty port taking seconds to
// fail, browsers took up to 30 s to come back to a restarted server.
const EXTRA_SERVER_RESCAN_MS = 3000;
const EXTRA_SERVER_RESCAN_FOR_MS = 60000;
const EXTRA_SERVER_HELD_MS = 10000;
const extraSockets = new Map(); // port -> socket
const extraServersLost = new Map(); // port -> when its open connection first dropped
let extraScanRunning = false;
let lastExtraScan = 0;

function connectExtraServer(port) {
  return new Promise(settled => {
    let socket;
    try {
      socket = new WebSocket(`ws://${CONFIG.hostname}:${port}`);
    } catch (error) {
      settled();
      return;
    }
    extraSockets.set(port, socket);
    let opened = false;
    let openedAt = 0;
    socket.onopen = () => {
      opened = true;
      openedAt = Date.now();
      console.log(`Connected to an additional MCP server on port ${port}`);
      updateBadge();
      sendHello(socket).catch(error => console.error('Could not send hello:', error));
      settled();
    };
    socket.onmessage = async (event) => handleMessage(JSON.parse(event.data), socket);
    socket.onclose = (event) => {
      if (opened) {
        // The first drop starts the minute: a server that accepts and drops
        // again would otherwise keep it going, probed every 3 s for good. One
        // that held for a while before dropping is a new restart, with its own.
        const held = Date.now() - openedAt > EXTRA_SERVER_HELD_MS;
        if (held || !extraServersLost.has(port)) extraServersLost.set(port, Date.now());
        console.log(`Lost the additional MCP server on port ${port} (close code ${event.code})`);
      }
      if (extraSockets.get(port) === socket) extraSockets.delete(port);
      updateBadge();
      releaseAgentTabsOf(socket);
      settled();
    };
    socket.onerror = () => { /* nothing listening there; the next scan tries again */ };
  });
}

async function scanExtraServers(ports) {
  if (extraScanRunning) return;
  extraScanRunning = true;
  try {
    for (const port of ports) {
      if (!websocket || websocket.readyState !== WebSocket.OPEN) return;
      const existing = extraSockets.get(port);
      if (!existing || existing.readyState === WebSocket.CLOSED) await connectExtraServer(port);
    }
  } finally {
    extraScanRunning = false;
  }
}

setInterval(() => {
  const now = Date.now();
  if (now - lastExtraScan >= EXTRA_SERVER_SCAN_MS && !extraScanRunning) {
    lastExtraScan = now;
    scanExtraServers(Array.from({ length: settings.extraServerPorts }, (_, i) => CONFIG.port + 1 + i));
    return;
  }
  for (const [port, lostAt] of extraServersLost) {
    if (now - lostAt >= EXTRA_SERVER_RESCAN_FOR_MS) extraServersLost.delete(port);
  }
  // The settings may have changed since the drop
  if (extraServersLost.size) scanExtraServers([...extraServersLost.keys()].filter(port => port > CONFIG.port && port <= CONFIG.port + settings.extraServerPorts));
}, EXTRA_SERVER_RESCAN_MS);

function scheduleReconnect() {
  retryAttempts++;

  // Check configured max retries
  if (CONFIG.maxRetries > 0 && retryAttempts > CONFIG.maxRetries) {
    console.error(`Configured max retry attempts (${CONFIG.maxRetries}) exceeded. Stopping reconnection attempts.`);
    return;
  }

  const delay = retryAttempts > FAST_RETRIES
    ? Math.max(CONFIG.retryInterval, SLOW_RETRY_INTERVAL)
    : CONFIG.retryInterval;
  console.debug(`Scheduling reconnection attempt ${retryAttempts} in ${delay}ms`);
  setTimeout(connectToMCPServer, delay);
}

// Function to update configuration (can be called from popup or other scripts)
function updateConfig(newConfig) {
  Object.assign(CONFIG, newConfig);
  console.log('Configuration updated:', CONFIG);
  console.log('New WebSocket URL will be:', `ws://${CONFIG.hostname}:${CONFIG.port}`);

  // Save to storage for persistence. Hostname and port are per profile, so
  // two profiles on one Firefox account can use different servers.
  browser.storage.local.set({ hostname: CONFIG.hostname, port: CONFIG.port });
  browser.storage.sync.set({
    retryInterval: CONFIG.retryInterval,
    maxRetries: CONFIG.maxRetries,
    pingTimeout: CONFIG.pingTimeout
  });

  // Reconnect with new settings if currently connected
  if (isConnected || websocket) {
    console.log('Reconnecting with new configuration...');
    disconnect();
    connectToMCPServer();
  }
}

// Load configuration from storage on startup
async function loadConfig() {
  console.log('📥 Loading configuration from storage...');

  // Load from storage - browser.storage.sync.get() always succeeds
  const result = await browser.storage.sync.get({
    hostname: 'localhost',
    port: 8765,
    retryInterval: 5000,
    maxRetries: -1,
    pingTimeout: 5000,
    // Test configuration overrides (set by test framework)
    testPort: null,
    testHostname: null
  });

  // Check if we're in a test environment waiting for configuration
  const isTestEnvironment = result.testPort !== null || result.testHostname !== null;
  const hasValidTestConfig = result.testPort && result.testPort !== 8765;

  // In test environment, wait longer for configuration to be ready
  if (isTestEnvironment && !hasValidTestConfig) {
    console.log('🧪 Test environment detected but no valid test config yet');
    console.log('📋 Will retry in test environment...');
    throw new Error('Test environment detected but configuration not ready');
  }

  // Per-profile hostname and port (storage.local) win over the synced ones
  // older versions saved; test overrides win over both.
  const local = await browser.storage.local.get({ hostname: null, port: null });
  CONFIG.hostname = result.testHostname || local.hostname || result.hostname;
  CONFIG.port = result.testPort || local.port || result.port;
  CONFIG.retryInterval = result.retryInterval;
  CONFIG.maxRetries = result.maxRetries;
  CONFIG.pingTimeout = result.pingTimeout;

  console.log('📋 Configuration loaded:', CONFIG);
  console.log('🌐 WebSocket URL will be:', `ws://${CONFIG.hostname}:${CONFIG.port}`);

  if (result.testPort || result.testHostname) {
    console.log('🧪 Test overrides active:', {
      testPort: result.testPort,
      testHostname: result.testHostname
    });
  }
}

// Disconnect function
function disconnect() {
  if (websocket) {
    websocket.close();
    websocket = null;
  }
  isConnected = false;
  clearAllMonitors('the connection to the server was closed');
}

// The connection each request came in on, so its reply goes back there. Several
// servers can be connected at once (one per MCP client, see scanExtraServers).
const replySockets = new Map(); // request id -> socket
// The agent call behind each request, for handlers that learn the tab late (tabs.create)
const agentCalls = new Map(); // request id -> { tool, status }

async function handleMessage(message, socket = websocket) {
  const { id, type, action, data } = message;

  if (type === 'hello_ack') {
    await handleHelloAck(socket, data || {});
    return;
  }
  if (type !== 'request') return;
  replySockets.set(id, socket);

  // Handle ping-pong for connection testing
  if (action === 'ping') {
    sendResponse(id, 'ping', { message: 'pong', timestamp: new Date().toISOString() });
    return;
  }

  const unpaired = settings.pairingToken && !pairing.get(socket)?.paired;
  if (unpaired) {
    sendError(id, 'NOT_PAIRED', `This Firefox is not paired: ${pairing.get(socket)?.problem || 'no hello yet'}.`);
    return;
  }

  const refusal = await refusalFor(action, data || {});
  if (refusal) {
    sendError(id, 'NOT_ALLOWED', refusal);
    return;
  }

  const activity = AGENT_ACTIVITY[action];
  const agentTab = activity && data && data.tabId != null ? data.tabId : null;
  const agentStatus = message.agent && message.agent.status;
  if (activity) agentCalls.set(id, { status: agentStatus, socket });
  if (agentTab != null) {
    await markAgentActive(agentTab, { state: activity[0], label: activity[1], agentStatus, socket });
  }

  try {
    await routeAction(id, action, data);
  } finally {
    agentCalls.delete(id);
    // Navigation keeps "waiting" until the page has loaded (see tabs.onUpdated)
    if (agentTab != null && agentTabs.has(agentTab) && activity[0] !== 'waiting') {
      markAgentActive(agentTab, { state: 'thinking' });
    }
  }
}

// Why the person's settings rule an action out, or null when it may go ahead
const PERMISSION_OF = Object.fromEntries(Object.entries(ACTION_PERMISSIONS)
  .flatMap(([permission, actions]) => actions.map(action => [action, permission])));

async function refusalFor(action, data) {
  const permission = PERMISSION_OF[action];
  if (!permission) return null;
  if (settings.paused) {
    return 'The person using this Firefox has paused the agent. They can resume it from the MCP Bridge button in the toolbar.';
  }
  if (!settings[permission]) {
    return `"${PERMISSION_NAMES[permission]}" is turned off in this browser's MCP Bridge settings.`;
  }
  if (action === 'windows.create' && data.incognito && !settings.allowPrivate) {
    return 'The agent is not allowed in private windows (MCP Bridge settings).';
  }
  const urls = [];
  if (data.url) urls.push({ url: data.url });
  let tabId = data.tabId;
  if (action === 'tabs.captureVisibleTab' && tabId == null) {
    const [active] = await browser.tabs.query({ active: true, ...(data.windowId ? { windowId: data.windowId } : { currentWindow: true }) });
    tabId = active && active.id;
  }
  if (tabId != null) {
    const tab = await browser.tabs.get(Number(tabId)).catch(() => null);
    if (tab) urls.push({ url: tab.url, incognito: tab.incognito });
  }
  for (const { url, incognito } of urls) {
    if (incognito && !settings.allowPrivate) {
      return 'This tab is in a private window, and the agent is not allowed in private windows (MCP Bridge settings).';
    }
    let host = '';
    try { host = new URL(url).hostname; } catch (error) { continue; }
    if (isBlockedHost(host, settings.blockedSites)) {
      return `${host} is on this browser's blocked sites list (MCP Bridge settings).`;
    }
  }
  return null;
}

async function routeAction(id, action, data) {
  // Route actions to appropriate handlers (all are now async)
  switch (action.split('.')[0]) {
    case 'history':
      await handleHistoryAction(id, action, data);
      break;
    case 'tabs':
      await handleTabsAction(id, action, data);
      break;
    case 'tabGroups':
      await handleTabGroupsAction(id, action, data);
      break;
    case 'content':
      await handleContentAction(id, action, data);
      break;
    case 'navigation':
      await handleNavigationAction(id, action, data);
      break;
    case 'windows':
      await handleWindowsAction(id, action, data);
      break;
    case 'bookmarks':
      await handleBookmarksAction(id, action, data);
      break;
    case 'requests':
      await handleRequestsAction(id, action, data);
      break;
    case 'test':
      await handleTestAction(id, action, data);
      break;
    default:
      sendError(id, 'UNKNOWN_ACTION', `Unknown action: ${action}`);
  }
}

// The socket a reply to this request goes out on, if it is still open
function replySocket(id) {
  const socket = replySockets.get(id) || websocket;
  replySockets.delete(id);
  return socket && socket.readyState === WebSocket.OPEN ? socket : null;
}

function sendResponse(id, action, data) {
  const socket = replySocket(id);
  if (!socket) return;

  const message = {
    id,
    type: 'response',
    action,
    data,
    timestamp: new Date().toISOString()
  };

  socket.send(JSON.stringify(message));
}

function sendError(id, code, message, details = {}) {
  const socket = replySocket(id);
  if (!socket) return;

  const errorMessage = {
    id,
    type: 'error',
    action: '',
    data: {
      code,
      message,
      details
    },
    timestamp: new Date().toISOString()
  };

  socket.send(JSON.stringify(errorMessage));
}

// The toolbar button says how things stand without opening the popup: "off"
// when no server is connected, otherwise how many tabs an agent is working in.
function updateBadge() {
  const extra = [...extraSockets.values()].filter(socket => socket.readyState === WebSocket.OPEN).length;
  const servers = (isConnected ? 1 : 0) + extra;
  if (!settings.badgeEnabled) {
    browser.browserAction.setBadgeText({ text: '' });
  } else if (settings.paused) {
    browser.browserAction.setBadgeText({ text: 'stop' });
    browser.browserAction.setBadgeBackgroundColor({ color: '#c50042' });
    browser.browserAction.setTitle({ title: 'MCP Bridge for Firefox: agent paused' });
    return;
  }
  if (!servers) {
    if (settings.badgeEnabled) browser.browserAction.setBadgeText({ text: 'off' });
    browser.browserAction.setBadgeBackgroundColor({ color: '#8f8f9d' });
    browser.browserAction.setTitle({ title: 'MCP Bridge for Firefox: not connected' });
    return;
  }
  if (settings.badgeEnabled) browser.browserAction.setBadgeText({ text: agentTabs.size ? String(agentTabs.size) : '' });
  browser.browserAction.setBadgeBackgroundColor({ color: '#d97757' });
  browser.browserAction.setTitle({ title: `MCP Bridge for Firefox: ${servers} session${servers > 1 ? 's' : ''}` +
    (agentTabs.size ? `, agent working in ${agentTabs.size} tab${agentTabs.size > 1 ? 's' : ''}` : '') });
}

// What the popup shows: the servers this browser is connected to and the
// tabs an agent is working in
async function connectionDetails() {
  const browserName = (await browser.storage.local.get({ browserName: '' })).browserName;
  const tabs = await Promise.all([...agentTabs].map(([tabId, entry]) =>
    browser.tabs.get(tabId)
      .then(tab => ({ tabId, title: tab.title || tab.url, label: entry.label, state: entry.state }))
      .catch(() => null)));
  return {
    connected: isConnected,
    retryAttempts,
    config: CONFIG,
    browserName,
    extraPorts: [...extraSockets].filter(([, socket]) => socket.readyState === WebSocket.OPEN).map(([port]) => port),
    agentTabs: tabs.filter(Boolean),
  };
}

// Handle popup requests for connection status
browser.runtime.onMessage.addListener((request, sender, sendResponse) => {

  // A page's content script asks as soon as it starts, which is seconds earlier
  // than the tab's load completing (that waits for the favicon and the rest)
  if (request.action === 'isAgentTab') {
    const entry = sender.tab && agentTabs.get(sender.tab.id);
    sendResponse(entry ? { active: true, state: entry.state, label: entry.label } : { active: false });
    return true;
  }

  if (request.action === 'getConnectionStatus') {
    connectionDetails().then(sendResponse);
    return true;
  }

  if (request.action === 'releaseAgentTab') {
    sendResponse({ released: releaseAgentTab(request.tabId) });
    return true;
  }

  // Handle options page configuration updates
  if (request.type === 'configUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }

  // Handle advanced configuration updates
  if (request.type === 'advancedConfigUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }


  // Handle connection status request from options page
  if (request.type === 'getConnectionStatus') {
    sendResponse({ connected: isConnected });
    return true;
  }


  if (request.action === 'updateConfig') {
    updateConfig(request.config);
    sendResponse({ success: true, config: CONFIG });
    return true;
  }

  if (request.action === 'forceReconnect') {
    if (websocket) {
      websocket.close();
    }
    retryAttempts = 0;
    connectToMCPServer();
    sendResponse({ success: true });
    return true;
  }
});


// History handlers
async function handleHistoryAction(id, action, data) {
  try {
    switch (action) {
      case 'history.query':
        const historyItems = await browser.history.search({
          text: data.query || '',
          startTime: data.startTime || 0,
          endTime: data.endTime || Date.now(),
          maxResults: data.maxResults || 100
        });
        sendResponse(id, action, { items: historyItems });
        break;

      case 'history.recent':
        const recentItems = await browser.history.search({
          text: '',
          maxResults: data.count || 10
        });
        sendResponse(id, action, { items: recentItems });
        break;

      case 'history.delete_item':
        if (!data.url) {
          sendError(id, 'INVALID_PARAMETER', 'URL is required for history.delete_item');
          return;
        }
        await browser.history.deleteUrl({ url: data.url });
        sendResponse(id, action, { success: true, deletedUrl: data.url });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown history action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `History API error: ${error.message}`);
  }
}

// Colours Firefox accepts for a tab group.
const TAB_GROUP_COLORS = ['blue', 'cyan', 'grey', 'green', 'orange', 'pink', 'purple', 'red', 'yellow'];

function isBrowserId(value) {
  return Number.isInteger(value) && value >= 0;
}

// The tab group properties a request sets, out of title, color and collapsed
//
// Fields the request leaves out are left out here too, so Firefox keeps the
// group's current value for them. Returns null when a field has the wrong type or
// names a colour Firefox does not offer.
function readGroupProperties(data) {
  const properties = {};
  if (data.title !== undefined) {
    if (typeof data.title !== 'string') return null;
    properties.title = data.title;
  }
  if (data.color !== undefined) {
    if (!TAB_GROUP_COLORS.includes(data.color)) return null;
    properties.color = data.color;
  }
  if (data.collapsed !== undefined) {
    if (typeof data.collapsed !== 'boolean') return null;
    properties.collapsed = data.collapsed;
  }
  return properties;
}

// Tabs handlers
async function handleTabsAction(id, action, data) {
  try {
    switch (action) {
      case 'tabs.list':
        // No windowId means every window, which is what the tool has always claimed to return.
        //
        // The query object must omit the filter entirely rather than pass a falsy one:
        // `{windowId: undefined}` matches nothing. This used to read
        // `currentWindow: data.currentWindow || true`, where the `|| true` made the filter
        // unconditional and left no way to ask for anything but the current window.
        const tabQuery = data.windowId ? { windowId: data.windowId } : {};
        const tabs = await browser.tabs.query(tabQuery);
        // Include all tabs, even about:blank for debugging
        sendResponse(id, action, {
          tabs: tabs.map(tab => ({url: tab.url, id: tab.id, title: tab.title, active: tab.active, windowId: tab.windowId, pinned: tab.pinned, index: tab.index})),
          debug: {
            totalFound: tabs.length,
            tabUrls: tabs.map(tab => tab.url)
          }
        });
        break;

      case 'tabs.create':
        const createTabOptions = {
          url: data.url,
          active: data.active || false
        };
        
        // Add windowId if provided
        if (data.windowId) {
          createTabOptions.windowId = data.windowId;
        }
        
        // Add pinned status if provided
        if (data.pinned !== undefined) {
          createTabOptions.pinned = data.pinned;
        }
        
        const newTab = await browser.tabs.create(createTabOptions);
        const call = agentCalls.get(id);
        markAgentActive(newTab.id, {
          state: 'waiting', label: AGENT_ACTIVITY['tabs.create'][1],
          agentStatus: call && call.status, socket: call && call.socket
        });
        sendResponse(id, action, { tab: newTab });
        break;

      case 'tabs.close':
        await browser.tabs.remove(data.tabId);
        sendResponse(id, action, { success: true });
        break;

      case 'tabs.update':
        const updatedTab = await browser.tabs.update(data.tabId, {
          url: data.url,
          active: data.active
        });
        sendResponse(id, action, { tab: updatedTab });
        break;

      case 'tabs.switch':
        await browser.tabs.update(data.tabId, { active: true });
        sendResponse(id, action, { success: true });
        break;

      case 'tabs.release':
        sendResponse(id, action, { released: releaseAgentTab(data.tabId) });
        break;

      case 'tabs.group': {
        // Check both APIs before mutating: grouping arrived in 138, styling in 139.
        if (typeof browser.tabs.group !== 'function' ||
            typeof browser.tabGroups?.update !== 'function') {
          sendError(id, 'UNSUPPORTED_API', 'Tab grouping requires Firefox 139+ and the tabGroups permission');
          return;
        }
        const properties = readGroupProperties(data);
        if (!Array.isArray(data.tabIds) || data.tabIds.length === 0 ||
            !data.tabIds.every(isBrowserId) ||
            (data.groupId !== undefined && !isBrowserId(data.groupId)) ||
            properties === null) {
          sendError(id, 'INVALID_PARAMETER', 'Provide non-empty tabIds, an optional non-negative groupId, string title and valid color');
          return;
        }
        const groupOptions = { tabIds: data.tabIds };
        if (data.groupId === undefined) {
          // Without a window, Firefox creates the group in the focused one.
          const firstTab = await browser.tabs.get(data.tabIds[0]);
          groupOptions.createProperties = { windowId: firstTab.windowId };
        } else {
          groupOptions.groupId = data.groupId;
        }
        const groupId = await browser.tabs.group(groupOptions);
        if (Object.keys(properties).length) {
          // The tabs are grouped by now, and nothing undoes that, so a styling
          // failure is reported alongside the group ID rather than as an error.
          try {
            await browser.tabGroups.update(groupId, properties);
          } catch (error) {
            sendResponse(id, action, { groupId, styleError: error.message });
            return;
          }
        }
        sendResponse(id, action, { groupId });
        break;
      }

      case 'tabs.ungroup':
        if (typeof browser.tabs.ungroup !== 'function') {
          sendError(id, 'UNSUPPORTED_API', 'Tab ungrouping requires Firefox 138+');
          return;
        }
        if (!Array.isArray(data.tabIds) || data.tabIds.length === 0 ||
            !data.tabIds.every(isBrowserId)) {
          sendError(id, 'INVALID_PARAMETER', 'Provide a non-empty array of non-negative tabIds');
          return;
        }
        await browser.tabs.ungroup(data.tabIds);
        sendResponse(id, action, { success: true });
        break;

      case 'tabs.move':
        if (data.tabIds === undefined || data.tabIds === null) {
          sendError(id, 'INVALID_PARAMETER', 'tabIds is required for tabs.move');
          return;
        }

        // Report how many tabs actually moved, because a refused move is not an error.
        //
        // browser.tabs.move returns [] instead of throwing when it declines — moving an
        // unpinned tab in front of a pinned one is the common case. Without the counts, a
        // caller cannot tell a silent refusal from a successful move.
        const tabIdsToMove = Array.isArray(data.tabIds) ? data.tabIds : [data.tabIds];
        const moveProperties = { index: data.index === undefined ? -1 : data.index };
        if (data.windowId) {
          moveProperties.windowId = data.windowId;
        }

        const movedResult = await browser.tabs.move(tabIdsToMove, moveProperties);
        const movedTabs = Array.isArray(movedResult) ? movedResult : [movedResult];
        sendResponse(id, action, {
          tabs: movedTabs.map(tab => ({id: tab.id, url: tab.url, title: tab.title, windowId: tab.windowId, index: tab.index, pinned: tab.pinned})),
          requested: tabIdsToMove.length,
          moved: movedTabs.length
        });
        break;

      case 'tabs.captureVisibleTab':
        const windowId = data.windowId || null;
        const options = {
          format: data.format || 'png',
          quality: data.quality || 90
        };

        // A named tab is captured itself, whichever window has focus: the
        // "current window" is wherever the person is working, which once
        // was another app's window and not the page the agent was on
        let wantedTab = null;
        if (data.tabId != null) {
          wantedTab = await browser.tabs.get(Number(data.tabId)).catch(() => null);
          if (!wantedTab && !data.tabIdOptional) {
            sendError(id, 'CAPTURE_ERROR', `No tab with id ${data.tabId}`);
            break;
          }
        }

        // The agent overlay sits on top of the page in the tab being captured;
        // hide it for the capture. Tabs without content.js simply have none.
        const [shownTab] = wantedTab ? [wantedTab] : await browser.tabs.query(
          windowId ? { active: true, windowId } : { active: true, currentWindow: true });
        const setOverlay = (visible) => shownTab
          ? browser.tabs.sendMessage(shownTab.id, { action: 'overlayVisibility', data: { visible } })
            .catch(() => {})
          : Promise.resolve();

        try {
          await setOverlay(false);
          const dataUrl = await (wantedTab
            ? browser.tabs.captureTab(wantedTab.id, options)
            : browser.tabs.captureVisibleTab(windowId, options))
            .finally(() => setOverlay(true));
          sendResponse(id, action, {
            dataUrl: dataUrl,
            format: options.format,
            quality: options.quality,
            windowId: shownTab ? shownTab.windowId : windowId,
            tabId: shownTab ? shownTab.id : null,
            title: shownTab ? shownTab.title : ''
          });
        } catch (captureError) {
          sendError(id, 'CAPTURE_ERROR', `Failed to capture screenshot: ${captureError.message}`);
        }
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown tabs action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Tabs API error: ${error.message}`);
  }
}

// Tab groups handlers
async function handleTabGroupsAction(id, action, data) {
  try {
    switch (action) {
      case 'tabGroups.update': {
        if (typeof browser.tabGroups?.update !== 'function') {
          sendError(id, 'UNSUPPORTED_API', 'tabGroups.update requires Firefox 139+ and the tabGroups permission');
          return;
        }
        const properties = readGroupProperties(data);
        if (!isBrowserId(data.groupId) || properties === null ||
            Object.keys(properties).length === 0) {
          sendError(id, 'INVALID_PARAMETER', 'Provide a non-negative groupId and at least one of title, color or collapsed');
          return;
        }
        const group = await browser.tabGroups.update(data.groupId, properties);
        sendResponse(id, action, { group });
        break;
      }

      case 'tabGroups.query': {
        if (typeof browser.tabGroups?.query !== 'function') {
          sendError(id, 'UNSUPPORTED_API', 'tabGroups.query requires Firefox 139+ and the tabGroups permission');
          return;
        }
        const queryInfo = readGroupProperties(data);
        if ((data.windowId !== undefined && !isBrowserId(data.windowId)) || queryInfo === null) {
          sendError(id, 'INVALID_PARAMETER', 'windowId must be a non-negative integer, title a string, color valid and collapsed a boolean');
          return;
        }
        if (data.windowId !== undefined) queryInfo.windowId = data.windowId;
        const groups = await browser.tabGroups.query(queryInfo);
        // Always include id: callers need it for tabs.group and tabGroups.update.
        sendResponse(id, action, {
          groups: groups.map(group => ({
            id: group.id,
            title: group.title,
            color: group.color,
            collapsed: group.collapsed,
            windowId: group.windowId
          }))
        });
        break;
      }

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown tabGroups action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Tab groups API error: ${error.message}`);
  }
}

// Helper function to get current tab URL
async function getCurrentTabUrl(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    return tab.url;
  } catch (error) {
    return "Unknown URL";
  }
}

// Tabs an agent is working in. The page shows the overlay, the cursor and a
// marker in the tab strip from the agent's first action until the agent says it
// is done (tabs_release), the server that sent the work disconnects, or
// releaseAfterMinutes (a setting) pass with no action in the tab.
//
// Each entry holds what the page shows: a state the cursor animates (reading,
// clicking, typing, waiting, working, thinking) and a first-person label, the
// agent's own status line when it gave one.
const agentTabs = new Map(); // tab id -> { socket, state, label, at }

// What each action looks like on the page when the agent gives no status line
const AGENT_ACTIVITY = {
  'content.get_text': ['reading', 'Reading this page'],
  'content.get_html': ['reading', 'Reading this page'],
  'content.snapshot': ['reading', 'Looking over this page'],
  'content.click': ['clicking', 'Clicking'],
  'content.type': ['typing', 'Typing'],
  'content.press_key': ['typing', 'Pressing a key'],
  'content.execute_script': ['working', 'Working on this page'],
  'content.capture_file_input': ['clicking', 'Opening the file picker'],
  'content.upload_begin': ['waiting', 'Uploading a file'],
  'content.upload_chunk': ['waiting', 'Uploading a file'],
  'content.upload_finish': ['waiting', 'Uploading a file'],
  'content.copy_image': ['reading', 'Copying an image'],
  'content.paste': ['typing', 'Pasting'],
  'content.real_input_prep': ['clicking', 'Clicking'],
  'content.real_pointer_prep': ['clicking', 'Moving the mouse'],
  'content.hover': ['clicking', 'Pointing'],
  'content.drag': ['clicking', 'Dragging'],
  'content.check_typed': ['typing', 'Typing'],
  'content.real_input_arrived': ['typing', 'Pressing a key'],
  'navigation.go_to_url': ['waiting', 'Opening a page'],
  'navigation.back': ['waiting', 'Going back'],
  'navigation.forward': ['waiting', 'Going forward'],
  'navigation.reload': ['waiting', 'Reloading the page'],
  'tabs.create': ['waiting', 'Opening a new tab'],
  'tabs.switch': ['working', 'Switching to this tab'],
};
const THINKING_LABEL = 'Thinking about the next step';

// A read right after navigating got the page before it, so the navigation tools
// answer once the new page has loaded. A jump within the page never starts
// loading, and a slow page must not hold the tool for long, so both are bounded.
const NAVIGATION_START_MS = 2000;
const NAVIGATION_LOAD_MS = 15000;

async function navigated(tabId, go) {
  let onUpdated, onRequest, onRemoved, timer, startWindow;
  const finished = new Promise(resolve => {
    let loading = false;
    // Begins once the browser took the navigation, which a page's "leave this
    // page?" prompt can hold up
    startWindow = () => {
      if (!loading) timer = setTimeout(() => resolve(true), NAVIGATION_START_MS);
    };
    const started = () => {
      if (loading) return;
      loading = true;
      clearTimeout(timer);
      timer = setTimeout(() => resolve(false), NAVIGATION_LOAD_MS);
    };
    // The tab's status only turns to loading once the response comes in, which
    // for a slow server is after the start window; the request is at once.
    onRequest = started;
    onUpdated = (id, change) => {
      if (id !== tabId) return;
      if (change.status === 'loading') started();
      else if (change.status === 'complete' && loading) resolve(true);
    };
    onRemoved = id => {
      if (id === tabId) resolve(false);
    };
    browser.webRequest.onBeforeRequest.addListener(onRequest, { urls: ['<all_urls>'], types: ['main_frame'], tabId });
    browser.tabs.onUpdated.addListener(onUpdated);
    browser.tabs.onRemoved.addListener(onRemoved);
  });
  try {
    await go();
    startWindow();
    return await finished;
  } finally {
    clearTimeout(timer);
    browser.webRequest.onBeforeRequest.removeListener(onRequest);
    browser.tabs.onUpdated.removeListener(onUpdated);
    browser.tabs.onRemoved.removeListener(onRemoved);
  }
}

function sendAgentState(tabId) {
  const entry = agentTabs.get(tabId);
  if (!entry) return;
  browser.tabs.sendMessage(tabId, { action: 'agentActive', data: { state: entry.state, label: entry.label } })
    .catch(() => { /* no content script yet; the load listener below covers it */ });
}

// Called before an action runs (state = what it does) and after it finishes
// (state = thinking). A label given by the agent stays across both, since one
// step of its work usually takes several calls.
async function markAgentActive(tabId, { state = 'working', label = null, agentStatus = null, socket = null } = {}) {
  if (tabId == null) return;
  const previous = agentTabs.get(tabId);
  agentTabs.set(tabId, {
    socket: socket || (previous && previous.socket) || null,
    state,
    label: agentStatus || label || (previous && previous.agentStatus) || THINKING_LABEL,
    agentStatus: agentStatus || (state === 'thinking' && previous ? previous.agentStatus : null),
    at: Date.now()
  });
  if (!previous) updateBadge();
  sendAgentState(tabId);
  // "Follow the agent" brings forward whichever tab the agent acts in next
  if (state !== 'thinking') {
    if (settings.followAgent) browser.tabs.update(tabId, { active: true }).catch(() => {});
  }
}

function releaseAgentTab(tabId) {
  if (!agentTabs.delete(tabId)) return false;
  updateBadge();
  browser.tabs.sendMessage(tabId, { action: 'agentRelease' }).catch(() => {});
  return true;
}

function releaseAgentTabsOf(socket) {
  for (const [tabId, entry] of agentTabs) {
    if (entry.socket === socket) releaseAgentTab(tabId);
  }
}

setInterval(() => {
  const now = Date.now();
  for (const [tabId, entry] of agentTabs) {
    if (now - entry.at > settings.releaseAfterMinutes * 60 * 1000) releaseAgentTab(tabId);
  }
}, 30000);

browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== 'complete' || !agentTabs.has(tabId)) return;
  const entry = agentTabs.get(tabId);
  // A page that finished loading is no longer something to wait for
  if (entry.state === 'waiting') markAgentActive(tabId, { state: 'thinking' });
  else sendAgentState(tabId);
});

browser.tabs.onRemoved.addListener(tabId => {
  if (agentTabs.delete(tabId)) updateBadge();
});

// Helper function to send message with retry logic for content script
async function sendMessageWithRetry(tabId, message, maxRetries = 5, delayMs = 2000) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      // Check if tab exists and is accessible
      const tab = await browser.tabs.get(tabId);
      if (!tab) {
        throw new Error('Tab not found');
      }

      // Skip status check for now and try to send message
      // The content script should be available on all URLs except chrome:// pages
      if (tab.url && (tab.url.startsWith('chrome://') || tab.url.startsWith('moz-extension://'))) {
        throw new Error('Cannot access content script on system pages');
      }

      const result = await browser.tabs.sendMessage(tabId, message);
      return result;
    } catch (error) {
      // Tabs that finished loading before the extension started (or was
      // reloaded) never got content.js, and no amount of waiting fixes that.
      if (attempt < maxRetries && error.message.includes('Receiving end does not exist')) {
        try {
          // settings.js may already be there, in which case loading it again throws
          await browser.tabs.executeScript(tabId, { file: '/settings.js' }).catch(() => {});
          await browser.tabs.executeScript(tabId, { file: '/content.js' });
          continue;
        } catch (injectError) {
          // Fall through to the retry delay; the tab may still be loading.
        }
      }
      if (attempt === maxRetries) {
        throw new Error(`Content script not available after ${maxRetries} attempts: ${error.message}`);
      }

      // Wait longer before retry
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }
}

// Uploads in progress, by upload ID: one entry per file with its received chunks
const pendingUploads = new Map();

// Content handlers
async function handleContentAction(id, action, data) {
  try {
    switch (action) {
      case 'content.get_text':
        const textResult = await sendMessageWithRetry(data.tabId, {
          action: 'extractText'
        });
        sendResponse(id, action, { text: textResult.text });
        break;

      case 'content.get_html':
        const htmlResult = await sendMessageWithRetry(data.tabId, {
          action: 'extractHTML'
        });
        sendResponse(id, action, { html: htmlResult.html });
        break;

      case 'content.execute_script':
        try {
          // Scripts in one tab share a scope, so a const from one call made the
          // next fail ("redeclaration of const d"). A block keeps each one's
          // names to itself and still returns its last expression. A top-level
          // return or await needs a function, and the function's promise is
          // what the result waits for.
          //
          // The retries are safe because these are all parse errors, and nothing
          // in a script that fails to parse has run.
          const script = data.script.replace(/^#!.*/, '');
          // "use strict" is a directive only at the start of the script
          const strict = /^\s*(['"])use strict\1/.test(script) ? '"use strict";' : '';
          let executeResults;
          try {
            executeResults = await browser.tabs.executeScript(data.tabId, { code: `${strict}{\n${script}\n}` });
          } catch (error) {
            if (/return not in function|await .*is only valid/.test(error.message)) {
              executeResults = await browser.tabs.executeScript(data.tabId, {
                code: `(async () => {\n${script}\n})()`
              });
            } else if (/^redeclaration of var /.test(error.message)) {
              // A var and a function of the same name are legal only outside a block
              executeResults = await browser.tabs.executeScript(data.tabId, { code: script });
            } else {
              throw error;
            }
          }
          // executeScript returns an array of results from each frame
          const result = executeResults && executeResults.length > 0 ? executeResults[0] : null;
          sendResponse(id, action, { 
            result: result,
            url: await getCurrentTabUrl(data.tabId)
          });
        } catch (scriptError) {
          sendError(id, 'SCRIPT_ERROR', `Script execution failed: ${scriptError.message}`);
        }
        break;

      case 'content.real_input_prep':
      case 'content.real_pointer_prep':
      case 'content.hover':
      case 'content.drag':
      case 'content.click_effect':
      case 'content.check_typed':
      case 'content.real_input_arrived':
      case 'content.snapshot':
      case 'content.click':
      case 'content.type':
      case 'content.press_key':
      case 'content.capture_file_input': {
        const pageAction = { 'content.snapshot': 'snapshot', 'content.click': 'click',
          'content.type': 'type', 'content.press_key': 'pressKey',
          'content.capture_file_input': 'captureFileInput', 'content.real_input_prep': 'realInputPrep',
          'content.check_typed': 'checkTypedText', 'content.real_pointer_prep': 'realPointerPrep',
          'content.real_input_arrived': 'realInputArrived',
          'content.hover': 'hover', 'content.drag': 'drag', 'content.click_effect': 'clickEffect' }[action];
        // Real input posted to the window lands in the tab showing in it
        if (data.activate) await browser.tabs.update(data.tabId, { active: true });
        const pageResult = await sendMessageWithRetry(data.tabId, { action: pageAction, data });
        if (pageResult && pageResult.error) {
          sendError(id, 'PAGE_ERROR', pageResult.error);
        } else {
          sendResponse(id, action, pageResult);
        }
        break;
      }

      // Files arrive in base64 chunks small enough for one WebSocket message
      // each, and are rebuilt here into File objects, which the content script
      // receives intact because extension messages are structured-cloned.
      case 'content.upload_begin':
        pendingUploads.set(data.uploadId, data.files.map(file => ({ ...file, parts: [] })));
        sendResponse(id, action, { ok: true });
        break;

      case 'content.upload_chunk': {
        const upload = pendingUploads.get(data.uploadId);
        if (!upload) {
          sendError(id, 'UPLOAD_ERROR', `Unknown upload ${data.uploadId}`);
          break;
        }
        upload[data.fileIndex].parts.push(Uint8Array.fromBase64
          ? Uint8Array.fromBase64(data.data)
          : Uint8Array.from(atob(data.data), c => c.charCodeAt(0)));
        sendResponse(id, action, { ok: true });
        break;
      }

      case 'content.upload_finish': {
        const upload = pendingUploads.get(data.uploadId);
        pendingUploads.delete(data.uploadId);
        if (!upload) {
          sendError(id, 'UPLOAD_ERROR', `Unknown upload ${data.uploadId}`);
          break;
        }
        const files = upload.map(file => new File(file.parts, file.name, { type: file.type || '' }));
        const pageResult = await sendMessageWithRetry(data.tabId, {
          action: 'setFiles',
          data: { files, relativePaths: upload.map(file => file.relativePath), ref: data.ref, selector: data.selector }
        });
        if (pageResult && pageResult.error) {
          sendError(id, 'PAGE_ERROR', pageResult.error);
        } else {
          sendResponse(id, action, pageResult);
        }
        break;
      }

      // "Copy Image": an image on a page, or a file sent the way uploads are,
      // goes on the system clipboard as PNG, ready for content.paste or for
      // the person to paste with Ctrl+V.
      case 'content.copy_image': {
        let blob;
        let source;
        if (data.uploadId) {
          const upload = pendingUploads.get(data.uploadId);
          pendingUploads.delete(data.uploadId);
          if (!upload) {
            sendError(id, 'UPLOAD_ERROR', `Unknown upload ${data.uploadId}`);
            break;
          }
          blob = new Blob(upload[0].parts, { type: upload[0].type || '' });
          source = upload[0].name;
        } else {
          const image = await sendMessageWithRetry(data.tabId, { action: 'imageSource', data });
          if (image && image.error) {
            sendError(id, 'PAGE_ERROR', image.error);
            break;
          }
          if (image.bytes) {
            blob = new Blob([image.bytes]);
          } else {
            const response = await fetch(image.src, { credentials: 'include' });
            if (!response.ok) {
              sendError(id, 'FETCH_ERROR', `Could not load the image (${response.status}): ${image.src}`);
              break;
            }
            blob = await response.blob();
          }
          source = image.target;
        }
        const png = await imageAsPng(blob);
        await browser.clipboard.setImageData(png.bytes, 'png');
        sendResponse(id, action, { copied: source, width: png.width, height: png.height });
        break;
      }

      case 'content.paste': {
        const pageResult = await sendMessageWithRetry(data.tabId, { action: 'paste', data });
        if (pageResult && pageResult.error) {
          sendError(id, 'PAGE_ERROR', pageResult.error);
        } else {
          sendResponse(id, action, pageResult);
        }
        break;
      }

      // A hash of what is on the clipboard, never the contents, so the server
      // can tell whether the agent's own copy is still there before it pastes.
      // On Windows the server reads the clipboard's change counter instead.
      case 'content.clipboard_fingerprint': {
        const parts = [];
        try {
          for (const item of await navigator.clipboard.read()) {
            for (const type of item.types) {
              const blob = await item.getType(type);
              parts.push(type, new Uint8Array(await blob.arrayBuffer()));
            }
          }
        } catch (error) {
          try {
            parts.push('text', await navigator.clipboard.readText());
          } catch (fallback) {
            sendError(id, 'CLIPBOARD_ERROR', `Could not read the clipboard: ${fallback.message}`);
            break;
          }
        }
        const encoder = new TextEncoder();
        const chunks = parts.map(part => typeof part === 'string' ? encoder.encode(part + ' ') : part);
        const all = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
        let offset = 0;
        for (const chunk of chunks) {
          all.set(chunk, offset);
          offset += chunk.length;
        }
        const digest = await crypto.subtle.digest('SHA-256', all);
        sendResponse(id, action, { fingerprint: [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('') });
        break;
      }

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown content action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Content API error: ${error.message}`);
  }
}

// The clipboard takes PNG or JPEG only, so a WebP or GIF is redrawn as PNG
async function imageAsPng(blob) {
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext('2d').drawImage(bitmap, 0, 0);
  const png = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
  return { bytes: await png.arrayBuffer(), width: bitmap.width, height: bitmap.height };
}

// Navigation handlers
async function handleNavigationAction(id, action, data) {
  try {
    switch (action) {
      case 'navigation.go_to_url':
        sendResponse(id, action, { success: true,
          loaded: await navigated(data.tabId, () => browser.tabs.update(data.tabId, { url: data.url })) });
        break;

      case 'navigation.back':
        sendResponse(id, action, { success: true,
          loaded: await navigated(data.tabId, () => browser.tabs.goBack(data.tabId)) });
        break;

      case 'navigation.forward':
        sendResponse(id, action, { success: true,
          loaded: await navigated(data.tabId, () => browser.tabs.goForward(data.tabId)) });
        break;

      case 'navigation.reload':
        sendResponse(id, action, { success: true,
          loaded: await navigated(data.tabId, () => browser.tabs.reload(data.tabId, { bypassCache: data.bypassCache || false })) });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown navigation action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Navigation API error: ${error.message}`);
  }
}

// Bookmarks handlers
async function handleBookmarksAction(id, action, data) {
  try {
    switch (action) {
      case 'bookmarks.list':
        let bookmarks;

        // Check if folder filtering is requested
        if (data && data.folderId) {
          try {
            // Get bookmarks from specific folder
            const folderChildren = await browser.bookmarks.getChildren(data.folderId);
            bookmarks = folderChildren.map(node => ({
              id: node.id,
              title: node.title,
              url: node.url,
              isFolder: !node.url,
              parentId: node.parentId
            }));
          } catch (folderError) {
            // Handle invalid folder ID
            sendError(id, 'INVALID_FOLDER_ID', `Invalid folder ID: ${data.folderId}. ${folderError.message}`);
            return;
          }
        } else {
          // Get all bookmarks (existing behavior)
          const bookmarkTree = await browser.bookmarks.getTree();
          // Flatten the tree structure into a flat array
          function flattenBookmarks(nodes) {
            let result = [];
            for (const node of nodes) {
              // Add current node if it's a folder or has a URL (bookmark)
              result.push({
                id: node.id,
                title: node.title,
                url: node.url,
                isFolder: !node.url,
                parentId: node.parentId
              });
              // Recursively add children
              if (node.children) {
                result = result.concat(flattenBookmarks(node.children));
              }
            }
            return result;
          }
          bookmarks = flattenBookmarks(bookmarkTree);
        }

        sendResponse(id, action, { bookmarks });
        break;

      case 'bookmarks.search':
        const searchResults = await browser.bookmarks.search(data.query);
        sendResponse(id, action, { bookmarks: searchResults });
        break;

      case 'bookmarks.create':
        const newBookmark = await browser.bookmarks.create({
          parentId: data.parentId,
          title: data.title,
          url: data.url
        });
        sendResponse(id, action, { bookmark: newBookmark });
        break;

      case 'bookmarks.createFolder':
        const newFolder = await browser.bookmarks.create({
          parentId: data.parentId,
          title: data.title
          // No URL - creates a folder
        });
        sendResponse(id, action, { folder: newFolder });
        break;

      case 'bookmarks.update':
        const updateData = {};
        if (data.title !== undefined) {
          updateData.title = data.title;
        }
        if (data.url !== undefined) {
          updateData.url = data.url;
        }
        const updatedBookmark = await browser.bookmarks.update(data.bookmarkId, updateData);
        sendResponse(id, action, { bookmark: updatedBookmark });
        break;

      case 'bookmarks.delete':
        await browser.bookmarks.remove(data.bookmarkId);
        sendResponse(id, action, { success: true });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown bookmarks action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Bookmarks API error: ${error.message}`);
  }
}

// Windows handlers
async function handleWindowsAction(id, action, data) {
  try {
    switch (action) {
      case 'windows.list':
        const windows = await browser.windows.getAll({
          populate: data.populate !== false, // default to true
          windowTypes: ['normal', 'popup', 'panel', 'devtools']
        });
        sendResponse(id, action, { windows });
        break;

      case 'windows.get':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.get');
          return;
        }
        const window = await browser.windows.get(data.windowId, {
          populate: data.populate !== false
        });
        sendResponse(id, action, { window });
        break;

      case 'windows.get_current':
        const currentWindow = await browser.windows.getCurrent({
          populate: data.populate !== false
        });
        sendResponse(id, action, { window: currentWindow });
        break;

      case 'windows.create': {
        const createOptions = {};
        if (data.url) createOptions.url = data.url;
        if (data.type) createOptions.type = data.type;
        if (data.state) createOptions.state = data.state;
        if (data.width) createOptions.width = data.width;
        if (data.height) createOptions.height = data.height;
        if (data.top) createOptions.top = data.top;
        if (data.left) createOptions.left = data.left;
        if (data.incognito !== undefined) createOptions.incognito = data.incognito;

        // Firefox always focuses a new window: its schema marks `focused: false` as
        // "Opening inactive windows is not supported" and ignores it. So for a
        // background window, hand focus back to the window that had it. If no
        // Firefox window had focus, there is nothing of ours to hand it back to.
        const previous = data.focused === false ? await browser.windows.getLastFocused() : null;
        let newWindow = await browser.windows.create(createOptions);
        if (previous?.focused) {
          await browser.windows.update(previous.id, { focused: true });
          newWindow = await browser.windows.get(newWindow.id, { populate: true });
        }
        sendResponse(id, action, { window: newWindow });
        break;
      }

      case 'windows.close':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.close');
          return;
        }
        await browser.windows.remove(data.windowId);
        sendResponse(id, action, { success: true, windowId: data.windowId });
        break;

      case 'windows.focus':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.focus');
          return;
        }
        await browser.windows.update(data.windowId, { focused: true });
        sendResponse(id, action, { success: true, windowId: data.windowId });
        break;

      case 'windows.update':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.update');
          return;
        }
        const updateOptions = {};
        if (data.state) updateOptions.state = data.state;
        if (data.focused !== undefined) updateOptions.focused = data.focused;
        if (data.width) updateOptions.width = data.width;
        if (data.height) updateOptions.height = data.height;
        if (data.top !== undefined) updateOptions.top = data.top;
        if (data.left !== undefined) updateOptions.left = data.left;
        
        const updatedWindow = await browser.windows.update(data.windowId, updateOptions);
        sendResponse(id, action, { window: updatedWindow });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown windows action: ${action}`);
    }
  } catch (error) {
    // Handle specific window errors
    if (error.message && error.message.includes('No window with id')) {
      sendError(id, 'WINDOW_NOT_FOUND', `Window with ID ${data.windowId} not found`);
    } else if (error.message && error.message.includes('Invalid window state')) {
      sendError(id, 'INVALID_WINDOW_STATE', error.message);
    } else if (error.message && error.message.includes('Invalid window type')) {
      sendError(id, 'INVALID_WINDOW_TYPE', error.message);
    } else {
      sendError(id, 'API_ERROR', `Windows API error: ${error.message}`);
    }
  }
}

// REMOVED: onStartup listener to prevent race conditions during testing
// Extension will only connect on explicit user actions or valid storage events

// Request monitoring state
const activeMonitors = new Map(); // monitor_id -> monitor config
const capturedRequests = new Map(); // monitor_id -> array of requests
const requestDetails = new Map(); // request_id -> full request details
const capturedResponseBodies = new Map(); // request_id -> response body data
const MAX_STORED_RESPONSE_BODIES = 200;

// WebRequest listener functions
let onBeforeRequestListener = null;
let onBeforeSendHeadersListener = null;
let onSendHeadersListener = null;
let onHeadersReceivedListener = null;
let onResponseStartedListener = null;
let onCompletedListener = null;
let onErrorOccurredListener = null;


function startWebRequestMonitoring(monitor) {
  console.log(`🔍 Starting WebRequest monitoring for monitor ${monitor.id}`);
  setupWebRequestListeners();
}

function setupWebRequestListeners() {
  if (onBeforeRequestListener) {
    // Already set up
    return;
  }

  console.log('🔧 Setting up WebRequest API listeners');

  // Before request - captures initial request data
  onBeforeRequestListener = function(details) {
    handleWebRequestEvent('onBeforeRequest', details);
    captureResponseBody(details);
  };

  // Headers received - captures response headers and status
  onHeadersReceivedListener = function(details) {
    handleWebRequestEvent('onHeadersReceived', details);
  };

  // Completed - captures final timing and status
  onCompletedListener = function(details) {
    handleWebRequestEvent('onCompleted', details);
  };

  // Error - captures failed requests
  onErrorOccurredListener = function(details) {
    handleWebRequestEvent('onErrorOccurred', details);
  };

  // "blocking" is what makes captureResponseBody possible, not a wish to block
  // anything: Firefox only lets filterResponseData reach a request whose
  // extension registered a blocking listener for it. The listener returns
  // nothing, so no request is delayed, cancelled or redirected.
  browser.webRequest.onBeforeRequest.addListener(
    onBeforeRequestListener,
    { urls: ["<all_urls>"] },
    ["requestBody", "blocking"]
  );

  browser.webRequest.onHeadersReceived.addListener(
    onHeadersReceivedListener,
    { urls: ["<all_urls>"] },
    ["responseHeaders"]
  );

  browser.webRequest.onCompleted.addListener(
    onCompletedListener,
    { urls: ["<all_urls>"] }
  );

  browser.webRequest.onErrorOccurred.addListener(
    onErrorOccurredListener,
    { urls: ["<all_urls>"] }
  );

  console.log('✅ WebRequest listeners registered');
}

// Drop every monitor and everything they captured.
//
// A monitor lives in this page, which outlives any one server, and only the
// client that started it knows its id. Once the connection carrying that client
// is gone the monitor can never be read or stopped again, while it goes on
// holding the webRequest listeners up and filtering response bodies for nobody.
// The captured data goes with the monitors for the same reason: the request ids
// that reach it come from requests.list_captured.
function clearAllMonitors(reason) {
  const monitorCount = activeMonitors.size;

  activeMonitors.clear();
  capturedRequests.clear();
  requestDetails.clear();
  capturedResponseBodies.clear();

  if (monitorCount > 0) {
    stopWebRequestMonitoring();
    console.log(`🧹 Cleared ${monitorCount} monitor(s): ${reason}`);
  }
}

function stopWebRequestMonitoring() {
  if (activeMonitors.size > 0) {
    // Still have active monitors
    return;
  }

  console.log('🛑 Stopping WebRequest monitoring - removing listeners');

  if (onBeforeRequestListener) {
    browser.webRequest.onBeforeRequest.removeListener(onBeforeRequestListener);
    onBeforeRequestListener = null;
  }

  if (onHeadersReceivedListener) {
    browser.webRequest.onHeadersReceived.removeListener(onHeadersReceivedListener);
    onHeadersReceivedListener = null;
  }

  if (onCompletedListener) {
    browser.webRequest.onCompleted.removeListener(onCompletedListener);
    onCompletedListener = null;
  }

  if (onErrorOccurredListener) {
    browser.webRequest.onErrorOccurred.removeListener(onErrorOccurredListener);
    onErrorOccurredListener = null;
  }

  console.log('✅ WebRequest listeners removed');
}

function handleWebRequestEvent(eventType, details) {
  // Check if any monitor should capture this request
  for (const [monitorId, monitor] of activeMonitors) {
    if (shouldCaptureRequest(monitor, details)) {
      captureRequestEvent(monitorId, eventType, details);
    }
  }
}

function shouldCaptureRequest(monitor, details) {
  // Check tab filter
  if (monitor.tab_id && details.tabId !== monitor.tab_id) {
    return false;
  }

  // Check URL patterns
  if (monitor.url_patterns && monitor.url_patterns.length > 0) {
    const url = details.url;
    return monitor.url_patterns.some(pattern => {
      // <all_urls> is not glob syntax, but it is what a WebExtensions caller
      // reaches for, and as a glob it matches nothing at all.
      if (pattern === '*' || pattern === '<all_urls>') return true;

      // Convert glob pattern to regex
      const regexPattern = pattern
        .replace(/\*/g, '.*')
        .replace(/\?/g, '.');

      try {
        return new RegExp(regexPattern).test(url);
      } catch (e) {
        console.warn(`Invalid URL pattern: ${pattern}`, e);
        return false;
      }
    });
  }

  return true; // No filters, capture all
}

function captureRequestEvent(monitorId, eventType, details) {
  const requestId = details.requestId;
  const timestamp = new Date().toISOString();

  // One record per request id, shared by every monitor that matches it.
  //
  // The WebRequest API delivers each event once however many monitors want it,
  // and requests.get_content reads the record by request id alone. Anything
  // per-monitor therefore has to be keyed by monitor: listed_by holds the
  // monitors that already have this request in capturedRequests, so each of
  // them lists it exactly once.
  let request = requestDetails.get(requestId);
  if (!request) {
    request = {
      request_id: requestId,
      url: details.url,
      method: details.method || 'GET',
      tab_id: details.tabId,
      frame_id: details.frameId,
      type: details.type,
      timestamp: timestamp,
      events: [],
      listed_by: new Set()
    };
    requestDetails.set(requestId, request);
  }

  // Add event data
  const event = {
    type: eventType,
    timestamp: timestamp,
    timeStamp: details.timeStamp
  };

  switch (eventType) {
    case 'onBeforeRequest':
      event.url = details.url;
      event.method = details.method;
      event.requestBody = details.requestBody;
      break;

    case 'onHeadersReceived':
      event.responseHeaders = details.responseHeaders;
      event.statusCode = details.statusCode;
      event.statusLine = details.statusLine;
      request.status_code = details.statusCode;
      request.response_headers = details.responseHeaders;

      // Extract content length and type from headers
      if (details.responseHeaders) {
        for (const header of details.responseHeaders) {
          if (header.name.toLowerCase() === 'content-length') {
            request.response_content_length = parseInt(header.value) || 0;
          }
          if (header.name.toLowerCase() === 'content-type') {
            request.response_content_type = header.value;
          }
        }
      }
      break;

    case 'onCompleted':
      event.statusCode = details.statusCode;
      request.status_code = details.statusCode;
      request.completed = true;
      request.duration_ms = details.timeStamp - (request.events[0]?.timeStamp || details.timeStamp);

      break;

    case 'onErrorOccurred':
      event.error = details.error;
      request.error = details.error;
      request.completed = true;
      break;
  }

  request.events.push(event);

  // If request is complete, add to captured list
  if (request.completed && !request.listed_by.has(monitorId)) {
    const captured = capturedRequests.get(monitorId) || [];
    captured.push({
      request_id: requestId,
      url: request.url,
      method: request.method,
      status_code: request.status_code,
      duration_ms: request.duration_ms || 0,
      timestamp: request.timestamp,
      tab_id: request.tab_id,
      type: request.type,
      error: request.error,
      response_size_bytes: request.response_content_length || 0,
      response_content_type: request.response_content_type || null
    });
    capturedRequests.set(monitorId, captured);
    request.listed_by.add(monitorId);
  }
}

// Keep a copy of one request's response body for requests.get_content.
//
// Call from onBeforeRequest only; the response stream can no longer be tapped
// once it has started. Does nothing unless an active monitor matches the
// request and asked for response bodies. What it keeps lands in
// capturedResponseBodies under details.requestId, the same id
// requests.list_captured reports.
//
// browser.webRequest.filterResponseData puts this code in the path of the bytes
// on their way to the page, so every byte read here has to be written back or
// the page gets a truncated response. That is why write() comes first and the
// bookkeeping cannot throw past it. Firefox has already undone any
// Content-Encoding by this point, so the bytes are the real body. Only the
// first max_body_size of them are kept, but all of them are counted, so a body
// kept in part -- or not at all, for its content type -- still reports its
// full size.
function captureResponseBody(details) {
  let monitor = null;
  for (const activeMonitor of activeMonitors.values()) {
    if (activeMonitor.options?.capture_response_bodies &&
        shouldCaptureRequest(activeMonitor, details)) {
      monitor = activeMonitor;
      break;
    }
  }

  if (!monitor) {
    return;
  }

  let filter;
  try {
    filter = browser.webRequest.filterResponseData(details.requestId);
  } catch (error) {
    // Not every request can be filtered - a cached alt-data response and a
    // redirect both refuse. The request is still captured, just without a body.
    console.log(`No response filter for ${details.url}: ${error.message}`);
    return;
  }

  const maxSize = monitor.options.max_body_size || 50000;
  const kept = [];
  let totalBytes = 0;
  let keptBytes = 0;
  let truncated = false;

  filter.ondata = (event) => {
    filter.write(event.data);

    try {
      totalBytes += event.data.byteLength;

      const room = maxSize - keptBytes;
      if (room <= 0) {
        truncated = true;
        return;
      }

      const chunk = event.data.byteLength > room ? event.data.slice(0, room) : event.data;
      if (chunk.byteLength < event.data.byteLength) {
        truncated = true;
      }
      kept.push(chunk);
      keptBytes += chunk.byteLength;
    } catch (error) {
      console.error(`Error buffering response body for ${details.url}: ${error.message}`);
    }
  };

  filter.onstop = () => {
    filter.close();

    try {
      storeResponseBody(details.requestId, monitor, kept, totalBytes, truncated);
    } catch (error) {
      console.error(`Error storing response body for ${details.url}: ${error.message}`);
    }
  };

  filter.onerror = () => {
    console.log(`Response filter for ${details.url} stopped: ${filter.error}`);
  };
}

// Record what captureResponseBody read, as text when the content type allows it.
//
// size_bytes is the whole body even when content is null, so a caller that
// cannot get the bytes still learns how many there were.
//
// Only the last MAX_STORED_RESPONSE_BODIES are kept. A monitor on "*" sees
// every request the browser makes, and holding max_body_size for each of them
// would grow without limit for as long as monitoring runs. The size recorded on
// the request itself survives eviction, so an evicted request still reports how
// large its body was.
function storeResponseBody(requestId, monitor, chunks, totalBytes, truncated) {
  const requestDetail = requestDetails.get(requestId);
  const contentType = requestDetail?.response_content_type || '';

  if (requestDetail) {
    requestDetail.response_body_size = totalBytes;
  }

  const wanted = shouldCaptureContentType(contentType, monitor.options.content_types_to_capture);

  capturedResponseBodies.set(requestId, {
    content: wanted ? decodeResponseText(chunks, contentType) : null,
    content_type: contentType || null,
    size_bytes: totalBytes,
    truncated: truncated
  });

  while (capturedResponseBodies.size > MAX_STORED_RESPONSE_BODIES) {
    capturedResponseBodies.delete(capturedResponseBodies.keys().next().value);
  }
}

// Whether a response's content type is one the monitor asked to capture.
//
// An empty captureTypes means every type. A response with no content type at
// all matches nothing, since there is no way to tell what it holds.
function shouldCaptureContentType(contentType, captureTypes) {
  if (!captureTypes || captureTypes.length === 0) {
    return true;
  }
  if (!contentType) {
    return false;
  }

  return captureTypes.some(type => {
    if (type.includes('*')) {
      return contentType.startsWith(type.split('/')[0]);
    }
    return contentType.includes(type);
  });
}

// Join response chunks into a string, or null if the body is not text.
//
// Honours the charset the response declared, falling back to UTF-8 when it
// names one TextDecoder does not know.
function decodeResponseText(chunks, contentType) {
  if (!/^\s*(text\/|application\/(json|xml|javascript|xhtml\+xml))/i.test(contentType)) {
    return null;
  }

  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(new Uint8Array(chunk), offset);
    offset += chunk.byteLength;
  }

  const charset = /charset\s*=\s*["']?([\w-]+)/i.exec(contentType)?.[1] || 'utf-8';
  try {
    return new TextDecoder(charset).decode(body);
  } catch (error) {
    return new TextDecoder('utf-8').decode(body);
  }
}

// Request monitoring handlers
async function handleRequestsAction(id, action, data) {
  try {
    switch (action) {
      case 'requests.start_monitoring':
        const monitor_id = `mon_${Date.now()}`;
        const monitor = {
          id: monitor_id,
          url_patterns: data.url_patterns || [],
          options: data.options || {},
          tab_id: data.tab_id || null,
          started_at: new Date().toISOString(),
          status: 'active'
        };

        // Register the monitor before the listeners go up, so the first request
        // through onBeforeRequest can already find it and tap its body.
        activeMonitors.set(monitor_id, monitor);
        capturedRequests.set(monitor_id, []);
        startWebRequestMonitoring(monitor);

        sendResponse(id, action, {
          monitor_id: monitor_id,
          status: 'active',
          started_at: monitor.started_at,
          url_patterns: monitor.url_patterns,
          options: monitor.options
        });
        break;

      case 'requests.stop_monitoring':
        const monitorToStop = activeMonitors.get(data.monitor_id);
        if (!monitorToStop) {
          sendError(id, 'MONITOR_NOT_FOUND', `Monitor ${data.monitor_id} not found`);
          break;
        }

        // Calculate statistics
        const captured = capturedRequests.get(data.monitor_id) || [];
        const startTime = new Date(monitorToStop.started_at).getTime();
        const stopTime = Date.now();
        const durationSeconds = (stopTime - startTime) / 1000;

        // Remove monitor
        activeMonitors.delete(data.monitor_id);

        // Stop listeners if no more monitors
        if (activeMonitors.size === 0) {
          stopWebRequestMonitoring();
        }

        sendResponse(id, action, {
          monitor_id: data.monitor_id,
          status: 'stopped',
          stopped_at: new Date().toISOString(),
          total_requests_captured: captured.length,
          statistics: {
            duration_seconds: durationSeconds,
            requests_per_second: captured.length / Math.max(durationSeconds, 1),
            total_data_size: captured.reduce((sum, req) => sum + (req.response_size_bytes || 0), 0)
          }
        });
        break;

      case 'requests.list_captured':
        // An id nobody knows is an error rather than an empty list, which reads
        // the same as a monitor that is running and has caught nothing yet.
        // Monitors do not always end where the caller ended them: losing the
        // server connection clears every one of them.
        if (!capturedRequests.has(data.monitor_id)) {
          sendError(id, 'MONITOR_NOT_FOUND',
            `Monitor ${data.monitor_id} not found. It was never started, or the connection that started it has since closed.`);
          break;
        }

        const monitorRequests = capturedRequests.get(data.monitor_id);

        sendResponse(id, action, {
          monitor_id: data.monitor_id,
          total_requests: monitorRequests.length,
          requests: monitorRequests.map(req => ({
            request_id: req.request_id,
            url: req.url,
            method: req.method,
            status_code: req.status_code,
            duration_ms: req.duration_ms,
            timestamp: req.timestamp,
            tab_id: req.tab_id,
            type: req.type,
            error: req.error
          }))
        });
        break;

      case 'requests.get_content':
        const requestDetail = requestDetails.get(data.request_id);
        if (!requestDetail) {
          sendError(id, 'REQUEST_NOT_FOUND', `Request ${data.request_id} not found`);
          break;
        }

        // Extract headers from events
        let requestHeaders = {};
        let responseHeaders = {};
        let requestBody = null;

        for (const event of requestDetail.events) {
          if (event.type === 'onBeforeRequest' && event.requestBody) {
            requestBody = event.requestBody;
          }
          if (event.type === 'onHeadersReceived' && event.responseHeaders) {
            responseHeaders = event.responseHeaders.reduce((acc, header) => {
              acc[header.name] = header.value;
              return acc;
            }, {});
          }
        }


        sendResponse(id, action, {
          request_id: data.request_id,
          request_headers: requestHeaders,
          response_headers: responseHeaders,
          request_body: {
            included: !!requestBody,
            content: requestBody ? JSON.stringify(requestBody) : null,
            content_type: requestHeaders['content-type'] || null,
            encoding: 'utf-8',
            size_bytes: requestBody ? JSON.stringify(requestBody).length : 0,
            truncated: false,
            saved_to_file: null
          },
          response_body: (() => {
            const capturedBody = capturedResponseBodies.get(data.request_id);

            if (capturedBody && capturedBody.content !== null) {
              return {
                included: true,
                content: capturedBody.content,
                content_type: capturedBody.content_type,
                encoding: 'utf-8',
                size_bytes: capturedBody.size_bytes,
                truncated: capturedBody.truncated,
                saved_to_file: null,
                note: "Response body read from the response stream"
              };
            }

            // No body to hand back, so say which of the reasons it was and give
            // the size anyway. The measured size is the body Firefox actually
            // delivered; Content-Length is only what the server claimed, and is
            // absent from most compressed responses.
            let note;
            if (!capturedBody) {
              note = "Response body not captured: monitoring did not ask for response bodies, or the response could not be filtered";
            } else if (capturedBody.content_type) {
              note = `Response body not captured: ${capturedBody.content_type} is not one of content_types_to_capture, or is not text`;
            } else {
              note = "Response body not captured: the response declared no content type";
            }

            const measured = requestDetail.response_body_size;
            const declared = requestDetail.response_content_length;

            return {
              included: false,
              content: null,
              content_type: responseHeaders['content-type'] || requestDetail.response_content_type || null,
              encoding: null,
              size_bytes: measured !== undefined ? measured : (declared !== undefined ? declared : null),
              truncated: false,
              saved_to_file: null,
              note: note
            };
          })()
        });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown requests action: ${action}`);
    }
  } catch (error) {
    sendError(id, 'API_ERROR', `Requests API error: ${error.message}`);
  }
}

// Test helper action handler
async function handleTestAction(id, action, data) {
  try {
    switch (action) {
      case 'test.get_popup_state':
        await handleGetPopupState(id, data);
        break;
        
      case 'test.get_options_state':
        await handleGetOptionsState(id, data);
        break;
        
      case 'test.get_storage_values':
        await handleGetStorageValues(id, data);
        break;
        
      case 'test.validate_ui_sync':
        await handleValidateUISync(id, data);
        break;
        
      case 'test.refresh_ui_state':
        await handleRefreshUIState(id, data);
        break;
        
      case 'test.visit_url':
        await handleVisitURL(id, data);
        break;
        
      case 'test.visit_multiple_urls':
        await handleVisitMultipleURLs(id, data);
        break;
        
      case 'test.clear_test_history':
        await handleClearTestHistory(id, data);
        break;
        

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown test action: ${action}`);
    }
  } catch (error) {
    console.error(`Error handling test action ${action}:`, error);
    sendError(id, 'TEST_ERROR', `Test action failed: ${error.message}`, { action, error: error.toString() });
  }
}

// Get current popup display state
async function handleGetPopupState(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get({
      hostname: 'localhost',
      port: 8765,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000,
      testPort: null,
      testHostname: null
    });
    
    // Calculate effective values (same logic as popup.js)
    const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
    const effectivePort = storageConfig.testPort || storageConfig.port || 8765;
    const serverUrl = `ws://${effectiveHostname}:${effectivePort}`;
    const hasTestOverrides = storageConfig.testPort !== null || storageConfig.testHostname !== null;
    
    sendResponse(id, 'test.get_popup_state', {
      serverUrl: serverUrl,
      retryInterval: storageConfig.retryInterval,
      maxRetries: storageConfig.maxRetries,
      pingTimeout: storageConfig.pingTimeout,
      hasTestOverrides: hasTestOverrides,
      effectiveHostname: effectiveHostname,
      effectivePort: effectivePort,
      testIndicatorShown: hasTestOverrides,
      storageValues: storageConfig
    });
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get popup state: ${error.message}`);
  }
}

// Get current options page display state  
async function handleGetOptionsState(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get({
      hostname: 'localhost',
      port: 8765,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000,
      testPort: null,
      testHostname: null
    });
    
    // Calculate display values (same logic as options.js)
    const displayHostname = storageConfig.testHostname || storageConfig.hostname;
    const displayPort = storageConfig.testPort || storageConfig.port;
    const webSocketUrl = `ws://${displayHostname}:${displayPort}`;
    const hasTestOverrides = storageConfig.testPort !== null || storageConfig.testHostname !== null;
    
    sendResponse(id, 'test.get_options_state', {
      displayHostname: displayHostname,
      displayPort: displayPort,
      retryInterval: storageConfig.retryInterval,
      maxRetries: storageConfig.maxRetries,
      pingTimeout: storageConfig.pingTimeout,
      webSocketUrl: webSocketUrl,
      hasTestOverrides: hasTestOverrides,
      testOverrideWarningShown: hasTestOverrides,
      storageValues: storageConfig
    });
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get options state: ${error.message}`);
  }
}

// Get raw storage values
async function handleGetStorageValues(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get();
    sendResponse(id, 'test.get_storage_values', storageConfig);
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get storage values: ${error.message}`);
  }
}

// Validate UI-storage synchronization
async function handleValidateUISync(id, data) {
  try {
    const { expectedValues } = data;
    
    // Get current storage values
    const storageConfig = await browser.storage.sync.get();
    
    // Get popup state
    const popupState = await getPopupStateForValidation(storageConfig);
    
    // Get options state  
    const optionsState = await getOptionsStateForValidation(storageConfig);
    
    // Check storage matches expected values
    let storageMatches = true;
    const issues = [];
    
    if (expectedValues) {
      for (const [key, expectedValue] of Object.entries(expectedValues)) {
        if (storageConfig[key] !== expectedValue) {
          storageMatches = false;
          issues.push(`Storage ${key}: expected ${expectedValue}, got ${storageConfig[key]}`);
        }
      }
    }
    
    // Validate popup displays correct effective values
    const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
    const effectivePort = storageConfig.testPort || storageConfig.port || 8765;
    
    const popupSyncValid = popupState.effectiveHostname === effectiveHostname && 
                          popupState.effectivePort === effectivePort;
    
    const optionsSyncValid = optionsState.displayHostname === effectiveHostname &&
                            optionsState.displayPort === effectivePort;
    
    if (!popupSyncValid) {
      issues.push(`Popup sync invalid: expected ${effectiveHostname}:${effectivePort}, got ${popupState.effectiveHostname}:${popupState.effectivePort}`);
    }
    
    if (!optionsSyncValid) {
      issues.push(`Options sync invalid: expected ${effectiveHostname}:${effectivePort}, got ${optionsState.displayHostname}:${optionsState.displayPort}`);
    }
    
    sendResponse(id, 'test.validate_ui_sync', {
      popupSyncValid: popupSyncValid,
      optionsSyncValid: optionsSyncValid,
      storageMatches: storageMatches,
      effectiveValues: {
        hostname: effectiveHostname,
        port: effectivePort
      },
      issues: issues
    });
  } catch (error) {
    sendError(id, 'VALIDATION_ERROR', `Failed to validate UI sync: ${error.message}`);
  }
}

// Trigger UI state refresh
async function handleRefreshUIState(id, data) {
  try {
    // This simulates what happens when popup/options pages refresh
    // In practice, this would trigger any cached state to be cleared
    // and force re-reading from storage
    
    // For now, we just confirm the action was received
    sendResponse(id, 'test.refresh_ui_state', {
      refreshed: true,
      popupStateUpdated: true,
      optionsStateUpdated: true,
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    sendError(id, 'REFRESH_ERROR', `Failed to refresh UI state: ${error.message}`);
  }
}

// Helper function for validation
async function getPopupStateForValidation(storageConfig) {
  const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
  const effectivePort = storageConfig.testPort || storageConfig.port || 8765;
  
  return {
    effectiveHostname,
    effectivePort,
    hasTestOverrides: storageConfig.testPort !== null || storageConfig.testHostname !== null
  };
}

// Helper function for validation
async function getOptionsStateForValidation(storageConfig) {
  const displayHostname = storageConfig.testHostname || storageConfig.hostname;
  const displayPort = storageConfig.testPort || storageConfig.port;
  
  return {
    displayHostname,
    displayPort,
    hasTestOverrides: storageConfig.testPort !== null || storageConfig.testHostname !== null
  };
}

// Test Helper: Visit a URL to create browser history
async function handleVisitURL(id, data) {
  try {
    const url = data.url;
    const waitTime = data.waitTime || 8000; // Increased default wait time

    if (!url) {
      sendError(id, 'INVALID_PARAMETERS', 'URL is required for test.visit_url');
      return;
    }

    console.log(`[FoxMCP] Starting visit to URL: ${url}`);

    // Create a new tab with the URL
    const tab = await browser.tabs.create({
      url: url,
      active: false // Don't make it active to avoid disrupting tests
    });

    console.log(`[FoxMCP] Created tab ${tab.id} for URL: ${url}`);

    // Wait for the tab to actually complete loading
    let tabLoaded = false;
    let loadStartTime = Date.now();
    const maxWaitTime = Math.max(waitTime, 10000); // At least 10 seconds

    // Set up a listener for tab updates
    const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
      if (tabId === tab.id && changeInfo.status === 'complete') {
        console.log(`[FoxMCP] Tab ${tab.id} finished loading`);
        tabLoaded = true;
      }
    };

    browser.tabs.onUpdated.addListener(tabUpdateListener);

    try {
      // Wait for either tab to load or timeout
      while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
        await new Promise(resolve => setTimeout(resolve, 500));
      }

      if (tabLoaded) {
        console.log(`[FoxMCP] Tab loaded in ${Date.now() - loadStartTime}ms`);
        // Give additional time for history to be recorded
        console.log(`[FoxMCP] Waiting additional time for history recording...`);
        await new Promise(resolve => setTimeout(resolve, 3000));
      } else {
        console.log(`[FoxMCP] Tab did not complete loading within ${maxWaitTime}ms, proceeding anyway`);
        // Still wait the original wait time as fallback
        await new Promise(resolve => setTimeout(resolve, waitTime));
      }

    } finally {
      // Clean up the listener
      browser.tabs.onUpdated.removeListener(tabUpdateListener);
    }

    console.log(`[FoxMCP] Closing tab ${tab.id}`);

    // Close the tab
    await browser.tabs.remove(tab.id);

    console.log(`[FoxMCP] Successfully visited and closed: ${url}`);

    sendResponse(id, 'test.visit_url', {
      success: true,
      url: url,
      tabId: tab.id,
      visitTime: new Date().toISOString(),
      loadTime: Date.now() - loadStartTime,
      tabLoaded: tabLoaded,
      message: `Successfully visited ${url} (loaded: ${tabLoaded})`
    });
    
  } catch (error) {
    sendError(id, 'VISIT_URL_ERROR', `Failed to visit URL: ${error.message}`);
  }
}

// Test Helper: Visit multiple URLs to create test history
async function handleVisitMultipleURLs(id, data) {
  try {
    const urls = data.urls || [];
    const waitTime = data.waitTime || 8000; // Increased time to wait at each URL
    const delayBetween = data.delayBetween || 3000; // Increased delay between visits

    if (!Array.isArray(urls) || urls.length === 0) {
      sendError(id, 'INVALID_PARAMETERS', 'urls array is required for test.visit_multiple_urls');
      return;
    }

    console.log(`[FoxMCP] Starting visit to ${urls.length} URLs`);
    const results = [];

    for (let i = 0; i < urls.length; i++) {
      const url = urls[i];
      console.log(`[FoxMCP] Visiting URL ${i + 1}/${urls.length}: ${url}`);

      try {
        // Use the same improved logic as single URL visit
        const tab = await browser.tabs.create({
          url: url,
          active: false
        });

        let tabLoaded = false;
        let loadStartTime = Date.now();
        const maxWaitTime = Math.max(waitTime, 10000);

        // Set up tab update listener
        const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
          if (tabId === tab.id && changeInfo.status === 'complete') {
            console.log(`[FoxMCP] Tab ${tab.id} finished loading URL ${i + 1}`);
            tabLoaded = true;
          }
        };

        browser.tabs.onUpdated.addListener(tabUpdateListener);

        try {
          // Wait for tab to load or timeout
          while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
            await new Promise(resolve => setTimeout(resolve, 500));
          }

          if (tabLoaded) {
            console.log(`[FoxMCP] URL ${i + 1} loaded in ${Date.now() - loadStartTime}ms`);
            // Extra wait for history recording
            await new Promise(resolve => setTimeout(resolve, 2000));
          } else {
            console.log(`[FoxMCP] URL ${i + 1} did not complete loading within ${maxWaitTime}ms`);
            await new Promise(resolve => setTimeout(resolve, waitTime));
          }

        } finally {
          browser.tabs.onUpdated.removeListener(tabUpdateListener);
        }

        // Close the tab
        await browser.tabs.remove(tab.id);

        results.push({
          url: url,
          success: true,
          tabId: tab.id,
          visitTime: new Date().toISOString(),
          loadTime: Date.now() - loadStartTime,
          tabLoaded: tabLoaded
        });
        
        // Small delay between visits
        if (i < urls.length - 1) {
          await new Promise(resolve => setTimeout(resolve, delayBetween));
        }
        
      } catch (error) {
        results.push({
          url: url,
          success: false,
          error: error.message
        });
      }
    }
    
    const successCount = results.filter(r => r.success).length;
    
    sendResponse(id, 'test.visit_multiple_urls', {
      success: true,
      totalUrls: urls.length,
      successfulVisits: successCount,
      failedVisits: urls.length - successCount,
      results: results,
      message: `Visited ${successCount}/${urls.length} URLs successfully`
    });
    
  } catch (error) {
    sendError(id, 'VISIT_MULTIPLE_URLS_ERROR', `Failed to visit multiple URLs: ${error.message}`);
  }
}

// Test Helper: Clear test history (for cleanup)
async function handleClearTestHistory(id, data) {
  try {
    const urls = data.urls || [];
    const clearAll = data.clearAll || false;
    
    if (clearAll) {
      // Clear all history (use with caution in tests)
      await browser.history.deleteAll();
      
      sendResponse(id, 'test.clear_test_history', {
        success: true,
        action: 'cleared_all',
        message: 'All browser history cleared'
      });
    } else if (urls.length > 0) {
      // Clear specific URLs
      const results = [];
      
      for (const url of urls) {
        try {
          await browser.history.deleteUrl({ url: url });
          results.push({ url: url, success: true });
        } catch (error) {
          results.push({ url: url, success: false, error: error.message });
        }
      }
      
      const successCount = results.filter(r => r.success).length;
      
      sendResponse(id, 'test.clear_test_history', {
        success: true,
        action: 'cleared_specific_urls',
        totalUrls: urls.length,
        successfulClears: successCount,
        failedClears: urls.length - successCount,
        results: results,
        message: `Cleared ${successCount}/${urls.length} URLs from history`
      });
    } else {
      sendError(id, 'INVALID_PARAMETERS', 'Either clearAll:true or urls array is required');
    }
    
  } catch (error) {
    sendError(id, 'CLEAR_HISTORY_ERROR', `Failed to clear test history: ${error.message}`);
  }
}


// ✅ INITIALIZATION: Load config and connect automatically after loading completes
// This ensures storage configuration is fully loaded before connection attempts
console.log('🚀 Extension starting - loading configuration...');

// Initialize extension: load config then connect
async function initializeExtension() {
  updateBadge();
  try {
    console.log('🚀 Initializing extension...');

    await loadConfig();

    console.log('✅ Configuration loaded successfully');

    // Connect after config load
    console.log('🔌 Connecting to MCP server...');
    connectToMCPServer();

  } catch (error) {
    console.error('❌ Failed to initialize extension:', error);
    console.log('🔄 Will retry initialization in 1 second...');
    setTimeout(() => {
      initializeExtension();
    }, 1000);
  }
}

// Start initialization
initializeExtension();
