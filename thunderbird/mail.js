// Read-only mail access through Thunderbird's own WebExtension APIs. Nothing
// here sends, moves, deletes or marks anything.

const MAX_MESSAGES = 200;

async function listAccounts() {
  const accounts = await browser.accounts.list(false);
  return {
    accounts: accounts.map((a) => ({
      id: a.id, name: a.name, type: a.type, identities: (a.identities || []).map((i) => i.email),
    })),
  };
}

async function listFolders({ accountId } = {}) {
  const folders = (await browser.folders.query({}))
    // An account's root (path /) holds the folders but no mail
    .filter((f) => !f.isVirtual && f.path !== '/' && (!accountId || f.accountId === accountId));
  const out = [];
  for (const f of folders) {
    const info = await browser.folders.getFolderInfo(f.id).catch(() => ({}));
    out.push({ id: f.id, accountId: f.accountId, path: f.path, name: f.name,
      specialUse: f.specialUse || [], unread: info.unreadMessageCount, total: info.totalMessageCount });
  }
  return { folders: out };
}

function summary(m) {
  return { id: m.id, date: new Date(m.date).toISOString(), author: m.author, subject: m.subject,
    read: m.read, flagged: m.flagged, tags: m.tags, folder: m.folder?.path, accountId: m.folder?.accountId };
}

// Newest first. A folder's list comes in database order, so everything since
// the cut-off date is fetched before sorting.
async function queryMessages(queryInfo, limit) {
  let page = await browser.messages.query(queryInfo);
  const found = [...page.messages];
  while (page.id) {
    page = await browser.messages.continueList(page.id);
    found.push(...page.messages);
  }
  found.sort((a, b) => b.date - a.date);
  return { total: found.length, messages: found.slice(0, Math.min(limit || 25, MAX_MESSAGES)).map(summary) };
}

async function listMessages({ folderId, sinceDays = 30, unreadOnly = false, limit = 25 }) {
  const queryInfo = { folderId, fromDate: new Date(Date.now() - sinceDays * 86400000) };
  if (unreadOnly) queryInfo.unread = true;
  return queryMessages(queryInfo, limit);
}
