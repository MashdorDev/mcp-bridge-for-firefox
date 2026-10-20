const $ = (id) => document.getElementById(id);

async function showStatus() {
  const servers = await browser.runtime.sendMessage('pairingStatus');
  $('status').replaceChildren(...(servers.length ? servers : [null]).map((s) => {
    const li = document.createElement('li');
    if (!s) li.textContent = 'No MCP Bridge server is running.';
    else if (s.paired) { li.textContent = `Port ${s.port}: paired`; li.className = 'paired'; }
    else li.textContent = `Port ${s.port}: not paired, ${s.problem || 'not connected'}`;
    return li;
  }));
}

(async () => {
  const stored = await browser.storage.local.get({ browserName: '', pairingToken: '', allowInput: false,
    allowAddonUpdates: false });
  $('name').value = stored.browserName;
  $('allowInput').checked = stored.allowInput;
  $('allowAddonUpdates').checked = stored.allowAddonUpdates;
  $('token').value = stored.pairingToken;
  showStatus();
  setInterval(showStatus, 2000);
})();

$('save').addEventListener('click', async () => {
  await browser.storage.local.set({ browserName: $('name').value.trim(), pairingToken: $('token').value.trim(),
    allowInput: $('allowInput').checked, allowAddonUpdates: $('allowAddonUpdates').checked });
  $('saved').textContent = 'Saved';
  setTimeout(() => { $('saved').textContent = ''; }, 2000);
});
