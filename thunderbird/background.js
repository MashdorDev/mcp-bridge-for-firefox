// Connects Thunderbird to the MCP Bridge server as one more "browser".
// The protocol is the Firefox add-on's (docs/protocol.md); this side answers
// only the thunderbird.* actions and ping.

const HOST = 'localhost';
const PORT = 8765;
// Under --stdio each MCP client runs its own server, on PORT or one of the
// ports after it (EXTRA_SERVER_PORTS in server.py)
const EXTRA_PORTS = 4;
const RETRY_MS = 5000;
const SLOW_RETRY_MS = 30000;
const FAST_RETRIES = 50;
const EXTRA_SCAN_MS = 15000;

const sockets = new Map(); // port -> WebSocket
let mainRetries = 0;
let scanning = false;

async function identity() {
  const stored = await browser.storage.local.get({ instanceId: '', browserName: '' });
  let instanceId = stored.instanceId;
  if (!instanceId) {
    instanceId = crypto.randomUUID();
    await browser.storage.local.set({ instanceId });
  }
  return {
    instance_id: instanceId,
    name: stored.browserName,
    version: browser.runtime.getManifest().version,
    app: 'thunderbird',
    real_input: false,
    real_input_port: 0,
  };
}

function send(socket, message) {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ ...message, timestamp: new Date().toISOString() }));
  }
}

const reply = (socket, id, action, data) => send(socket, { id, type: 'response', action, data });
const fail = (socket, id, code, message) =>
  send(socket, { id, type: 'error', action: '', data: { code, message, details: {} } });

async function handleMessage(socket, message) {
  const { id, type, action, data = {} } = message;
  if (type !== 'request') return;
  if (action === 'ping') {
    reply(socket, id, 'ping', { message: 'pong', timestamp: new Date().toISOString() });
    return;
  }
  try {
    switch (action) {
      case 'thunderbird.captureWindow':
        reply(socket, id, action, await browser.mcpChrome.captureWindow(data.scale || 1));
        break;
      default:
        fail(socket, id, 'UNKNOWN_ACTION',
          `${action} is not available in Thunderbird. browsers_select a Firefox browser for it.`);
    }
  } catch (error) {
    fail(socket, id, 'THUNDERBIRD_ERROR', error.message || String(error));
  }
}

function connect(port) {
  return new Promise((settled) => {
    let socket;
    try {
      socket = new WebSocket(`ws://${HOST}:${port}`);
    } catch (error) {
      if (port === PORT) scheduleMain();
      settled(false);
      return;
    }
    sockets.set(port, socket);
    socket.onopen = async () => {
      if (port === PORT) mainRetries = 0;
      send(socket, { id: `hello_${Date.now()}`, type: 'hello', action: 'browser.hello', data: await identity() });
      settled(true);
    };
    socket.onmessage = (event) => handleMessage(socket, JSON.parse(event.data));
    socket.onclose = () => {
      if (sockets.get(port) === socket) sockets.delete(port);
      settled(false);
      if (port === PORT) scheduleMain();
    };
    socket.onerror = () => { /* onclose follows */ };
  });
}

function scheduleMain() {
  mainRetries++;
  setTimeout(() => connect(PORT), mainRetries > FAST_RETRIES ? SLOW_RETRY_MS : RETRY_MS);
}

// One port at a time, and only while the main connection is up: Gecko opens
// one WebSocket per host at a time, so probes would queue the main one.
async function scanExtraPorts() {
  if (scanning) return;
  scanning = true;
  try {
    for (let port = PORT + 1; port <= PORT + EXTRA_PORTS; port++) {
      if (sockets.get(PORT)?.readyState !== WebSocket.OPEN) return;
      if (!sockets.has(port)) await connect(port);
    }
  } finally {
    scanning = false;
  }
}

browser.storage.onChanged.addListener(async (changes, area) => {
  if (area !== 'local' || !changes.browserName) return;
  const data = await identity();
  for (const socket of sockets.values()) {
    send(socket, { id: `hello_${Date.now()}`, type: 'hello', action: 'browser.hello', data });
  }
});

connect(PORT); // a failed connection's onclose schedules the next try
setInterval(scanExtraPorts, EXTRA_SCAN_MS);
