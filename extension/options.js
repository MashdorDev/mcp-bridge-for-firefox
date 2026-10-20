/*
 * MCP Bridge for Firefox - options page
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Every element with data-setting is bound to that key of settings.js and
 * saves as it changes. Host and port also live in storage.local but go
 * through the background script, which reconnects. The reconnect timings stay
 * in storage.sync, where older versions kept them. Test overrides
 * (testPort/testHostname, set by the test suite) win over the saved host and
 * port, and the page says so.
 */

const $ = id => document.getElementById(id);

const CONNECTION_DEFAULTS = { hostname: 'localhost', port: 8765 };
const RETRY_DEFAULTS = { retryInterval: 5000, maxRetries: -1, pingTimeout: 5000 };
const TEXT_SAVE_DELAY_MS = 400;
const TOAST_MS = 1600;

let toastTimer;
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, TOAST_MS);
}

function setError(key, message) {
  const slot = document.querySelector(`[data-error="${key}"]`);
  if (slot) slot.textContent = message || '';
  const input = $(key);
  if (input) input.setAttribute('aria-invalid', message ? 'true' : 'false');
}

// A number field's value, or null after showing why it is not allowed
function checkedNumber(input) {
  const value = Number(input.value);
  const min = Number(input.min);
  const max = Number(input.max);
  const valid = input.value !== '' && Number.isInteger(value) && value >= min && value <= max;
  setError(input.id, valid ? '' : `Use a whole number from ${min} to ${max}`);
  return valid ? value : null;
}

function renderPermissions() {
  $('permissions').replaceChildren(...Object.entries(PERMISSION_NAMES).map(([key, name]) => {
    const label = document.createElement('label');
    label.className = 'check';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.setting = key;
    const text = document.createElement('span');
    text.textContent = name;
    label.append(box, text);
    return label;
  }));
}

function showSettings(values) {
  for (const input of document.querySelectorAll('[data-setting]')) {
    const value = values[input.dataset.setting];
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value;
  }
}

async function load() {
  showSettings(await loadSettings());
  const sync = await browser.storage.sync.get({ ...RETRY_DEFAULTS, hostname: null, port: null, testPort: null, testHostname: null });
  const local = await browser.storage.local.get({ hostname: null, port: null });
  // Older versions saved host and port to storage.sync
  $('hostname').value = local.hostname || sync.hostname || CONNECTION_DEFAULTS.hostname;
  $('port').value = local.port || sync.port || CONNECTION_DEFAULTS.port;
  for (const key of Object.keys(RETRY_DEFAULTS)) $(key).value = sync[key];

  const testing = sync.testPort || sync.testHostname;
  $('testNotice').hidden = !testing;
  if (testing) {
    $('testNotice').textContent = `The test suite has set ${sync.testHostname || 'the host'}` +
      `${sync.testPort ? ':' + sync.testPort : ''}, which is used instead of the host and port below.`;
  }
}

async function saveSetting(input) {
  const key = input.dataset.setting;
  let value;
  if (input.type === 'checkbox') value = input.checked;
  else if (input.type === 'number') value = checkedNumber(input);
  else value = input.value.trim();
  if (value === null) return;
  if (key === 'realInputPort' && value && value === Number($('port').value)) {
    setError(key, `${value} is the bridge server's port (under Connection). Use the port Firefox was started with, such as 9223.`);
    return;
  }
  if (key === 'browserName') value = value.slice(0, 60);
  await browser.storage.local.set({ [key]: value });
  toast('Saved');
}

function bindSettings() {
  for (const input of document.querySelectorAll('[data-setting]')) {
    if (input.type === 'text' || input.tagName === 'TEXTAREA') {
      let timer;
      input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => saveSetting(input), TEXT_SAVE_DELAY_MS);
      });
    } else {
      input.addEventListener('change', () => saveSetting(input));
    }
  }
}

async function refreshStatus() {
  const status = await browser.runtime.sendMessage({ action: 'getConnectionStatus' }).catch(() => null);
  const connected = Boolean(status && status.connected);
  const config = (status && status.config) || {};
  const extra = (status && status.extraPorts) || [];
  $('dot').classList.toggle('on', connected);
  $('statusText').textContent = connected
    ? (extra.length ? `Connected to ${1 + extra.length} sessions` : 'Connected')
    : 'Not connected';
  $('url').textContent = `ws://${config.hostname || $('hostname').value}:${config.port || $('port').value}`;
}

async function saveConnection() {
  const hostname = $('hostname').value.trim();
  setError('hostname', hostname ? '' : 'Enter a host, such as localhost');
  const port = checkedNumber($('port'));
  if (port !== null && port === Number($('realInputPort').value)) {
    setError('port', `${port} is Firefox's remote control port (under Real input). The bridge server is usually 8765.`);
    return;
  }
  if (!hostname || port === null) return;
  await browser.storage.local.set({ hostname, port });
  await browser.runtime.sendMessage({ type: 'configUpdated', config: { hostname, port } });
  toast('Saved, reconnecting');
  setTimeout(refreshStatus, 800);
}

async function saveRetry(input) {
  const value = checkedNumber(input);
  if (value === null) return;
  await browser.storage.sync.set({ [input.id]: value });
  await browser.runtime.sendMessage({ type: 'advancedConfigUpdated', config: { [input.id]: value } });
  toast('Saved');
}

async function exportSettings() {
  const data = {
    format: 'mcp-bridge-settings',
    version: browser.runtime.getManifest().version,
    // The pairing token stays in this profile; an export is a file people share
    settings: { ...(await loadSettings()), pairingToken: undefined },
    connection: await browser.storage.local.get(CONNECTION_DEFAULTS),
    reconnect: await browser.storage.sync.get(RETRY_DEFAULTS),
  };
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const name = (data.settings.browserName || 'firefox').replace(/[^\w-]+/g, '-').toLowerCase();
  link.download = `mcp-bridge-settings-${name}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// Only known keys of the right type are taken from a file, so a hand-edited
// or foreign file cannot write anything else into storage
function picked(source, defaults) {
  const out = {};
  for (const [key, fallback] of Object.entries(defaults)) {
    if (source && typeof source[key] === typeof fallback) out[key] = source[key];
  }
  return out;
}

async function importSettings(file) {
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (error) {
    toast('That file is not a settings export');
    return;
  }
  if (!data || data.format !== 'mcp-bridge-settings') {
    toast('That file is not a settings export');
    return;
  }
  const settings = picked(data.settings, SETTINGS_DEFAULTS);
  const connection = picked(data.connection, CONNECTION_DEFAULTS);
  // The name belongs to the profile it came from; keep this profile's own
  delete settings.browserName;
  delete settings.pairingToken;
  await browser.storage.local.set({ ...settings, ...connection });
  await browser.storage.sync.set(picked(data.reconnect, RETRY_DEFAULTS));
  if (connection.hostname || connection.port) {
    await browser.runtime.sendMessage({ type: 'configUpdated', config: connection });
  }
  await load();
  toast(`Imported ${Object.keys(settings).length + Object.keys(connection).length} settings`);
}

// A second press within a few seconds confirms, so a stray click cannot wipe
// the settings and no modal dialog is needed
let resetArmed;
async function reset() {
  if (!resetArmed) {
    $('reset').textContent = 'Press again to reset';
    resetArmed = setTimeout(() => {
      resetArmed = null;
      $('reset').textContent = 'Reset all settings';
    }, 4000);
    return;
  }
  clearTimeout(resetArmed);
  resetArmed = null;
  $('reset').textContent = 'Reset all settings';
  const { browserName, pairingToken } = await loadSettings();
  await browser.storage.local.set({ ...SETTINGS_DEFAULTS, browserName, pairingToken, ...CONNECTION_DEFAULTS });
  await browser.storage.sync.set(RETRY_DEFAULTS);
  await browser.runtime.sendMessage({ type: 'configUpdated', config: { ...CONNECTION_DEFAULTS, ...RETRY_DEFAULTS } });
  await load();
  toast('Settings reset');
  setTimeout(refreshStatus, 800);
}

renderPermissions();
bindSettings();
$('hostname').addEventListener('change', saveConnection);
$('port').addEventListener('change', saveConnection);
for (const key of Object.keys(RETRY_DEFAULTS)) $(key).addEventListener('change', event => saveRetry(event.target));
$('defaultColor').addEventListener('click', async () => {
  $('overlayColor').value = SETTINGS_DEFAULTS.overlayColor;
  await saveSetting($('overlayColor'));
});
$('reconnect').addEventListener('click', async () => {
  $('statusText').textContent = 'Reconnecting';
  await browser.runtime.sendMessage({ action: 'forceReconnect' });
  setTimeout(refreshStatus, 800);
});
$('export').addEventListener('click', exportSettings);
$('import').addEventListener('click', () => $('importFile').click());
$('importFile').addEventListener('change', event => {
  if (event.target.files[0]) importSettings(event.target.files[0]);
  event.target.value = '';
});
$('reset').addEventListener('click', reset);

// The popup can pause the agent while this page is open
browser.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.paused) {
    document.querySelector('[data-setting="paused"]').checked = Boolean(changes.paused.newValue);
  }
});

load().then(refreshStatus);
setInterval(refreshStatus, 3000);
