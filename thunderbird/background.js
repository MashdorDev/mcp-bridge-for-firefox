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

// Pairing: this add-on reaches mail, so it answers only a server that proves
// it holds the token from ~/.foxmcp/pairing-token (pairing_proof in server.py).
const pairing = new WeakMap(); // socket -> { nonce, paired, problem }

const hex = (bytes) => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, '0')).join('');

async function proof(token, role, nonce) {
  const encode = (text) => new TextEncoder().encode(text);
  const key = await crypto.subtle.importKey('raw', encode(token), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', key, encode(`${role}|${nonce}`)));
}

async function sayHello(socket) {
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)));
  pairing.set(socket, { nonce, paired: false, problem: 'waiting for the server to prove the pairing token' });
  send(socket, { id: `hello_${Date.now()}`, type: 'hello', action: 'browser.hello',
    data: { ...(await identity()), client_nonce: nonce } });
}

// The token typed into the options wins; otherwise the server's own file,
// which is on this machine whenever the server is
async function pairingToken() {
  const { pairingToken: typed } = await browser.storage.local.get({ pairingToken: '' });
  return typed.trim() || (await browser.mcpChrome.readPairingToken()) || '';
}

async function handleHelloAck(socket, data) {
  const state = pairing.get(socket);
  if (!state) return;
  const token = await pairingToken().catch((error) => {
    state.problem = error.message;
    return null;
  });
  if (token === null) return;
  if (!token) {
    state.problem = 'no pairing token found in ~/.foxmcp/pairing-token or the options';
  } else if (!data.proof) {
    state.problem = 'the server has no pairing token; update the MCP Bridge server';
  } else if (data.proof !== await proof(token, 'server', state.nonce)) {
    state.problem = 'the server proved a different pairing token, so Thunderbird does not answer it';
  } else {
    state.paired = true;
    state.problem = '';
    send(socket, { id: `auth_${Date.now()}`, type: 'auth', action: 'browser.auth',
      data: { proof: await proof(token, 'client', data.server_nonce) } });
  }
}

async function handleMessage(socket, message) {
  const { id, type, action, data = {} } = message;
  if (type === 'hello_ack') return handleHelloAck(socket, data);
  if (type !== 'request') return;
  if (action === 'ping') {
    reply(socket, id, 'ping', { message: 'pong', timestamp: new Date().toISOString() });
    return;
  }
  const state = pairing.get(socket);
  if (!state?.paired) {
    fail(socket, id, 'NOT_PAIRED', `Thunderbird is not paired: ${state?.problem || 'no hello yet'}.`);
    return;
  }
  try {
    switch (action) {
      case 'thunderbird.captureWindow':
        reply(socket, id, action, await browser.mcpChrome.captureWindow(data.scale || 1));
        break;
      case 'thunderbird.snapshot':
        reply(socket, id, action, await browser.mcpChrome.snapshot(data.maxElements || 200));
        break;
      case 'mail.listAccounts':
        reply(socket, id, action, await listAccounts());
        break;
      case 'mail.listFolders':
        reply(socket, id, action, await listFolders(data));
        break;
      case 'mail.listMessages':
        reply(socket, id, action, await listMessages(data));
        break;
      case 'mail.search':
        reply(socket, id, action, await searchMessages(data));
        break;
      case 'mail.getMessage':
        reply(socket, id, action, await getMessage(data));
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
      await sayHello(socket);
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

// A new name or token reaches every server, and pairing starts over
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !(changes.browserName || changes.pairingToken)) return;
  for (const socket of sockets.values()) sayHello(socket);
});

// The options page shows whether each server is paired
browser.runtime.onMessage.addListener((message) => {
  if (message !== 'pairingStatus') return undefined;
  return Promise.resolve([...sockets].map(([port, socket]) => ({
    port, open: socket.readyState === WebSocket.OPEN, ...(pairing.get(socket) || {}), nonce: undefined,
  })));
});

connect(PORT); // a failed connection's onclose schedules the next try
setInterval(scanExtraPorts, EXTRA_SCAN_MS);
