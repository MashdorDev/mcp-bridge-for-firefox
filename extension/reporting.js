/*
 * MCP Bridge for Firefox - opt-in error reports
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Off by default. An error message can quote a page, a URL or a file path
 * the agent sent, so no message is ever sent: a report says which action
 * failed with which code, or for a crash, the error type and where in the
 * add-on's own code it was thrown.
 */

const REPORT_ENDPOINT = 'https://glitchtip.dorzairi.com/api/12/envelope/?sentry_key=3187eccdccc64642839c1d755261afce&sentry_version=7';
const REPORT_DATA_PERMISSION = 'technicalAndInteraction';
const MAX_REPORTS_PER_SESSION = 20; // a failing action retried in a loop should not send hundreds

// Codes that mean the add-on or Firefox failed. The rest are the agent's
// mistakes (INVALID_PARAMETER), the person's settings (NOT_ALLOWED) or the
// page's own errors (SCRIPT_ERROR), which say nothing about a bug here.
const REPORTED_CODES = ['API_ERROR', 'CAPTURE_ERROR', 'UPLOAD_ERROR', 'STORAGE_ERROR', 'CLIPBOARD_ERROR'];

// SpiderMonkey frames: "fn@moz-extension://<uuid>/background.js:1:2"
// The function name and file are kept to plain names, so a line that also
// holds a page address can't ride along in either
const REPORT_FRAME = /^([\w$.<>/*]*)@moz-extension:\/\/[\w-]+\/([\w./-]+):(\d+):(\d+)$/;

function reportFrames(stack) {
  const frames = [];
  for (const line of String(stack || '').split('\n')) {
    const match = REPORT_FRAME.exec(line.trim());
    if (!match) continue; // a page's script address would say what the agent was on
    // The uuid in a moz-extension address is random per install, so it is dropped
    frames.push({ function: match[1] || '?', filename: 'app:///' + match[2], lineno: Number(match[3]), colno: Number(match[4]), in_app: true });
  }
  return frames.reverse();
}

// The event for an action that answered with an error code, or null
function describeActionError(code, action) {
  if (!REPORTED_CODES.includes(code)) return null;
  const name = /^[\w.]{1,60}$/.test(action || '') ? action : 'unknown';
  const title = `${code} ${name}`;
  return { level: 'error', message: { formatted: 'Action failed: ' + title }, fingerprint: [title], tags: { action: name, code } };
}

// The event for an uncaught error or rejection, or null
function describeCrash(error) {
  if (!error || typeof error !== 'object') return null;
  const frames = reportFrames(error.stack);
  if (!frames.length) return null; // not thrown by the add-on's own code
  const type = typeof error.name === 'string' && /^\w{1,40}$/.test(error.name) ? error.name : 'Error';
  const top = frames[frames.length - 1];
  const title = `${type} at ${top.filename.slice('app:///'.length)}:${top.lineno}`;
  return {
    level: 'error',
    message: { formatted: 'Crash: ' + title },
    fingerprint: [title],
    exception: { values: [{ type, value: title, stacktrace: { frames } }] },
  };
}

let reportsSent = 0;
const reportsSeen = new Set();

async function hasReportConsent() {
  const { errorReports } = await browser.storage.local.get({ errorReports: false });
  if (!errorReports) return false;
  // The grant can be withdrawn in about:addons at any time, which wins over the setting
  const permissions = await browser.permissions.getAll();
  return !permissions.data_collection || permissions.data_collection.includes(REPORT_DATA_PERMISSION);
}

async function sendReport(event) {
  try {
    if (!event) return;
    const key = event.fingerprint[0];
    if (reportsSent >= MAX_REPORTS_PER_SESSION || reportsSeen.has(key)) return;
    if (!await hasReportConsent()) return;
    reportsSeen.add(key);
    reportsSent++;

    const eventId = crypto.randomUUID().replace(/-/g, '');
    const sentAt = new Date().toISOString();
    await fetch(REPORT_ENDPOINT, {
      method: 'POST',
      // A CORS request would carry Origin: moz-extension://<uuid>, which is
      // random per install and would identify the device; this sends Origin: null
      mode: 'no-cors',
      referrerPolicy: 'no-referrer',
      credentials: 'omit',
      headers: { 'Content-Type': 'text/plain' },
      body: [
        JSON.stringify({ event_id: eventId, sent_at: sentAt }),
        JSON.stringify({ type: 'event' }),
        JSON.stringify({
          ...event,
          event_id: eventId,
          timestamp: sentAt,
          platform: 'javascript',
          release: 'mcp-bridge-for-firefox@' + browser.runtime.getManifest().version,
          environment: 'production',
        }),
      ].join('\n'),
    });
  } catch (error) {
    console.debug('Error report not sent', error); // a failed report must not become a second problem
  }
}

function reportActionError(code, action) {
  return sendReport(describeActionError(code, action));
}

function reportCrash(error) {
  return sendReport(describeCrash(error));
}

if (typeof module !== 'undefined') {
  module.exports = { describeActionError, describeCrash, reportFrames, REPORTED_CODES };
}
