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

async function searchMessages({ text, author, subject, folderId, sinceDays = 90, unreadOnly = false, limit = 25 }) {
  const queryInfo = { fromDate: new Date(Date.now() - sinceDays * 86400000) };
  if (text) queryInfo.fullText = text;
  if (author) queryInfo.author = author;
  if (subject) queryInfo.subject = subject;
  if (folderId) queryInfo.folderId = folderId;
  if (unreadOnly) queryInfo.unread = true;
  return queryMessages(queryInfo, limit);
}

// Plain text when the message has it, otherwise its HTML with the tags taken out
function bodyText(part) {
  const texts = { plain: [], html: [] };
  (function walk(p) {
    if (p.body && !/attachment/i.test(p.headers?.['content-disposition']?.[0] || '')) {
      if (p.contentType === 'text/plain') texts.plain.push(p.body);
      else if (p.contentType === 'text/html') texts.html.push(p.body);
    }
    (p.parts || []).forEach(walk);
  })(part);
  if (texts.plain.length) return texts.plain.join('\n\n');
  return texts.html.join('\n\n').replace(/<style[\s\S]*?<\/style>|<script[\s\S]*?<\/script>/gi, '')
    .replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n\n').trim();
}

async function getMessage({ messageId, headersOnly = false, maxBodyChars = 20000 }) {
  const message = await browser.messages.get(messageId);
  if (!message) throw new Error(`No message with id ${messageId}`);
  const full = await browser.messages.getFull(messageId);
  const attachments = await browser.messages.listAttachments(messageId).catch(() => []);
  const result = {
    ...summary(message),
    headers: full.headers,
    attachments: attachments.map((a) => ({ name: a.name, contentType: a.contentType, size: a.size })),
  };
  if (!headersOnly) {
    const body = bodyText(full);
    result.body = body.slice(0, maxBodyChars);
    result.bodyTruncated = body.length > maxBodyChars;
  }
  return result;
}
