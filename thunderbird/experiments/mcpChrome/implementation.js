/* globals ExtensionAPI, Services */
"use strict";

// Errors of this type reach the add-on with their message; anything else
// arrives as "An unexpected error occurred"
const { ExtensionError } = ChromeUtils.importESModule("resource://gre/modules/ExtensionUtils.sys.mjs").ExtensionUtils;
const XHTML = "http://www.w3.org/1999/xhtml";
const SVG = "http://www.w3.org/2000/svg";
const AGENT_COLOR = "#d97757"; // the Firefox add-on's overlay colour
// The cursor stays while the agent works, between steps too; it goes when the
// server connection closes (hideAgent) or after this long with no action
const AGENT_IDLE_MS = 120000;

function mainWindow() {
  const win = Services.wm.getMostRecentWindow("mail:3pane");
  if (!win) throw new ExtensionError("No Thunderbird main window is open");
  return win;
}

// --- Finding elements -------------------------------------------------------

const INTERACTIVE = [
  "button", "toolbarbutton", "menuitem", "menulist", "input", "textarea", "select", "a[href]", "tab",
  ...["button", "menuitem", "tab", "treeitem", "row", "checkbox", "option", "link", "searchbox", "textbox", "combobox"]
    .map((role) => `[role="${role}"]`),
].join(", ");

// Refs from the last snapshot. WeakRefs, so a closed tab's elements can go.
let refs = new Map();

// The main window, the mail tab's about:3pane and the message shown in it
// (about:message): the three documents the mail view is made of. Add-on and
// web tabs run in another process and are out of reach here.
function documents(win) {
  const docs = [win.document];
  const tabmail = win.document.getElementById("tabmail");
  for (const inner of [tabmail?.currentAbout3Pane, tabmail?.currentAboutMessage]) {
    if (inner?.document && !docs.includes(inner.document)) docs.push(inner.document);
  }
  return docs;
}

// Position in the main window, adding up the frames an element sits in
function windowRect(el) {
  const r = el.getBoundingClientRect();
  let x = r.left;
  let y = r.top;
  for (let frame = el.ownerDocument.defaultView.browsingContext.embedderElement; frame;
    frame = frame.ownerDocument.defaultView.browsingContext.embedderElement) {
    const f = frame.getBoundingClientRect();
    x += f.left;
    y += f.top;
  }
  return { x, y, width: r.width, height: r.height };
}

// On top at its own centre, and so is every frame it sits in. Size and CSS
// checks are not enough: a hidden tab's panel keeps its boxes under the
// shown one.
function onTop(el) {
  const r = el.getBoundingClientRect();
  let [x, y] = [r.left + r.width / 2, r.top + r.height / 2];
  let node = el;
  for (;;) {
    // A shadow root answers for its own tree; the document only sees the host
    const root = node.getRootNode();
    const hit = root.elementFromPoint(x, y);
    if (!hit || !(hit === node || node.contains(hit))) return false;
    if (root.host) {
      node = root.host;
      continue;
    }
    const frame = node.ownerDocument.defaultView.browsingContext.embedderElement;
    if (!frame) return true;
    const f = frame.getBoundingClientRect();
    [x, y] = [x + f.left, y + f.top];
    node = frame;
  }
}

function isShown(el, win) {
  const r = windowRect(el);
  if (r.width < 2 || r.height < 2 || r.y + r.height < 0 || r.y > win.innerHeight || r.x > win.innerWidth) return false;
  return onTop(el);
}

function labelOf(el) {
  const text = el.getAttribute("aria-label") || el.getAttribute("label") || el.getAttribute("title")
    || el.getAttribute("placeholder") || el.textContent || el.value || el.id || "";
  return text.replace(/\s+/g, " ").trim().slice(0, 80);
}

// An element that cannot be measured cannot be clicked either, so it is
// skipped; the first error is kept, since a Thunderbird update that breaks
// measuring would otherwise just look like an empty window
let measureErrors = { count: 0, first: "" };

// Thunderbird's own components (the search bar among them) keep their
// controls in shadow trees, which querySelectorAll does not enter
function interactiveIn(root) {
  const found = [...root.querySelectorAll(INTERACTIVE)];
  for (const el of root.querySelectorAll("*")) {
    const shadow = el.openOrClosedShadowRoot;
    if (shadow) found.push(...interactiveIn(shadow));
  }
  return found;
}

function shownElements(win) {
  measureErrors = { count: 0, first: "" };
  return documents(win).flatMap(interactiveIn).filter((el) => {
    try {
      return isShown(el, win) && labelOf(el);
    } catch (error) {
      measureErrors.count++;
      measureErrors.first ||= `${error.message || error} (line ${error.lineNumber || "?"})`;
      return false;
    }
  });
}

// Thunderbird turns any error that is not an ExtensionError into "An
// unexpected error occurred"; keep the message and where it came from
async function reporting(fn) {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof ExtensionError) throw error;
    let detail;
    try {
      detail = `${error?.message || error} (implementation.js:${error?.lineNumber || "?"})`;
    } catch (unreadable) {
      detail = "an error that could not be read";
    }
    throw new ExtensionError(detail);
  }
}

function findElement(win, ref, text) {
  if (ref) {
    const el = refs.get(ref)?.deref();
    if (!el?.isConnected) throw new ExtensionError(`${ref} is gone; take a new thunderbird_snapshot`);
    return el;
  }
  const wanted = (text || "").toLowerCase();
  // A whole document can carry a role too; its label is all of its text, so
  // a partial match would land on it before the control that was meant
  const shown = shownElements(win).filter((e) => e.localName !== "html" && e.localName !== "body");
  const labelled = shown.map((e) => ({ e, label: labelOf(e).toLowerCase() }));
  const el = labelled.find((x) => x.label === wanted)?.e
    || labelled.filter((x) => x.label.includes(wanted)).sort((a, b) => a.label.length - b.label.length)[0]?.e;
  if (!el) throw new ExtensionError(`Nothing shown is labelled "${text}"`);
  return el;
}

// --- The overlay: a cursor, a box and a line saying what the agent does -------

function overlay(win) {
  let root = win.document.getElementById("mcp-bridge-agent");
  if (root) return root;
  const make = (tag, css, ns = XHTML) => {
    const el = win.document.createElementNS(ns, tag);
    if (css) el.setAttribute("style", css);
    return el;
  };
  root = make("div", "position:fixed;inset:0;pointer-events:none;z-index:2147483647;opacity:0;transition:opacity .4s");
  root.id = "mcp-bridge-agent";
  const box = make("div", `position:absolute;border:2px solid ${AGENT_COLOR};border-radius:4px;opacity:0;transition:left .3s, top .3s, width .3s, height .3s, opacity .3s`);
  const ring = make("div", `position:absolute;width:28px;height:28px;margin:-14px 0 0 -14px;border-radius:50%;border:2px solid ${AGENT_COLOR};opacity:0`);
  const label = make("div", `position:absolute;left:12px;bottom:28px;padding:4px 10px;border-radius:6px;background:${AGENT_COLOR};color:white;font:12px system-ui;opacity:0;transition:opacity .3s`);
  const cursor = make("svg", "position:absolute;left:0;top:0;width:22px;height:22px;overflow:visible;transition:transform .45s cubic-bezier(.2,.8,.2,1)", SVG);
  const arrow = make("path", "", SVG);
  arrow.setAttribute("d", "M0 0 L0 17 L4.5 13 L7.5 20 L10.5 18.8 L7.5 12 L13 12 Z");
  arrow.setAttribute("fill", AGENT_COLOR);
  arrow.setAttribute("stroke", "white");
  arrow.setAttribute("stroke-width", "1.5");
  cursor.append(arrow);
  root.append(box, ring, label, cursor);
  Object.assign(root, { box, ring, label, cursor });
  win.document.documentElement.append(root);
  root.cursor.style.transform = `translate(${win.innerWidth / 2}px, ${win.innerHeight / 2}px)`;
  return root;
}

const pause = (win, ms) => new Promise((resolve) => win.setTimeout(resolve, ms));

async function showAction(win, rect, status) {
  const o = overlay(win);
  win.clearTimeout(o.hideTimer);
  o.style.opacity = "1";
  Object.assign(o.box.style, { left: `${rect.x - 3}px`, top: `${rect.y - 3}px`,
    width: `${rect.width + 2}px`, height: `${rect.height + 2}px`, opacity: "1" });
  o.label.textContent = status || "";
  o.label.style.opacity = status ? "1" : "0";
  o.cursor.style.transform = `translate(${rect.x + rect.width / 2}px, ${rect.y + rect.height / 2}px)`;
  await pause(win, 480);
}

function ripple(win, rect) {
  const o = overlay(win);
  Object.assign(o.ring.style, { left: `${rect.x + rect.width / 2}px`, top: `${rect.y + rect.height / 2}px` });
  o.ring.animate([{ transform: "scale(.4)", opacity: 1 }, { transform: "scale(1.6)", opacity: 0 }], { duration: 450 });
  o.hideTimer = win.setTimeout(() => hide(win), AGENT_IDLE_MS);
}

// A click at a point of the window, for what the chrome DOM cannot see:
// add-on tabs and other content run in their own process. The events go
// in at the widget level, as real input does, so they reach that process.
// The person's own mouse pointer does not move.
async function clickAt(win, target) {
  const { x, y } = target;
  if (x < 0 || y < 0 || x > win.innerWidth || y > win.innerHeight) {
    throw new ExtensionError(`${Math.round(x)},${Math.round(y)} is outside the window (${win.innerWidth}x${win.innerHeight})`);
  }
  const rect = { x: x - 10, y: y - 10, width: 20, height: 20 };
  await showAction(win, rect, target.status);
  const button = target.rightClick ? 2 : 0;
  const send = (type, clickCount = 1) => win.synthesizeMouseEvent(type, x, y,
    { button, buttons: type === "mousedown" ? (button === 2 ? 2 : 1) : 0, clickCount },
    { isDOMEventSynthesized: false, isWidgetEventSynthesized: false, isAsyncEnabled: true });
  send("mousemove");
  if (!target.hover) {
    for (let count = 1; count <= (target.doubleClick ? 2 : 1); count++) {
      send("mousedown", count);
      send("mouseup", count);
    }
    ripple(win, rect);
  }
  const hit = win.document.elementFromPoint(x, y);
  return { role: hit?.localName || "", label: hit ? labelOf(hit) : "" };
}

// Keys the agent may press. Delete and Backspace are left out on purpose: in
// the message list they move mail to the trash.
const KEY_CODES = { Enter: 13, Escape: 27, Tab: 9, " ": 32, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37,
  ArrowRight: 39, PageUp: 33, PageDown: 34, Home: 36, End: 35 };

// The focused element, followed into shadow trees and into the frames
// (about:3pane, about:message) the mail view is made of
function focusedElement(win) {
  let el = win.document.activeElement;
  for (;;) {
    const inner = el?.openOrClosedShadowRoot?.activeElement || el?.contentDocument?.activeElement;
    if (!inner || inner === el) return el;
    el = inner;
  }
}

function pressOn(el, key, shift) {
  const view = el.ownerDocument.defaultView;
  const init = { key, code: key === " " ? "Space" : key, keyCode: KEY_CODES[key], which: KEY_CODES[key],
    shiftKey: !!shift, bubbles: true, cancelable: true, composed: true, view };
  el.dispatchEvent(new view.KeyboardEvent("keydown", init));
  if (key === "Enter" || key === " ") el.dispatchEvent(new view.KeyboardEvent("keypress", init));
  el.dispatchEvent(new view.KeyboardEvent("keyup", init));
}

function isEditable(el) {
  return el.localName === "input" || el.localName === "textarea" || el.isContentEditable
    || ["textbox", "searchbox", "combobox"].includes(el.getAttribute("role"));
}

function hide(win) {
  const o = win.document.getElementById("mcp-bridge-agent");
  if (!o) return;
  win.clearTimeout(o.hideTimer);
  o.style.opacity = "0";
  o.box.style.opacity = "0";
}

// --- The API ----------------------------------------------------------------

var mcpChrome = class extends ExtensionAPI {
  getAPI() {
    const { extension } = this;
    return {
      mcpChrome: {
        async captureWindow(scale) {
          const win = mainWindow();
          // The overlay is for the person watching, not for screenshots
          const agent = win.document.getElementById("mcp-bridge-agent");
          if (agent) {
            agent.style.display = "none";
            // drawSnapshot paints what is on screen; let the hidden overlay
            // leave it first. A covered or minimized window gets no animation
            // frames, so do not wait on them for long.
            await new Promise((resolve) => {
              win.requestAnimationFrame(() => win.requestAnimationFrame(resolve));
              win.setTimeout(resolve, 200);
            });
          }
          try {
            // drawSnapshot composites the chrome and every tab's content, which
            // tabs.captureVisibleTab cannot: the mail view is not a content tab.
            const rect = new win.DOMRect(0, 0, win.innerWidth, win.innerHeight);
            const bitmap = await win.browsingContext.currentWindowGlobal.drawSnapshot(rect, scale, "white");
            const canvas = win.document.createElementNS(XHTML, "canvas");
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            canvas.getContext("2d").drawImage(bitmap, 0, 0);
            bitmap.close();
            return { dataUrl: canvas.toDataURL("image/png"), width: canvas.width, height: canvas.height };
          } finally {
            if (agent) agent.style.display = "";
          }
        },

        async snapshot(maxElements) {
          return reporting(async () => {
            const win = mainWindow();
            refs = new Map();
            const shown = shownElements(win);
            const elements = shown.slice(0, maxElements).map((el, i) => {
              const ref = `r${i + 1}`;
              refs.set(ref, new WeakRef(el));
              const r = windowRect(el);
              return { ref, role: el.getAttribute("role") || el.localName, label: labelOf(el),
                x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
            });
            return { elements, total: shown.length, skipped: measureErrors.count, skipReason: measureErrors.first };
          });
        },

        async click(target) {
          return reporting(async () => {
            const win = mainWindow();
            // Schema-checked objects carry null, not undefined, for what was left out
            if (typeof target.x === "number") return clickAt(win, target);
            const el = findElement(win, target.ref, target.text);
            const rect = windowRect(el);
            await showAction(win, rect, target.status);
            // The whole sequence a person's click makes, at the element's
            // centre: tabs and tree rows select on mousedown, buttons act on
            // click. (nsIDOMWindowUtils.sendMouseEvent no longer exists.)
            const r = el.getBoundingClientRect();
            const view = el.ownerDocument.defaultView;
            const button = target.rightClick ? 2 : 0;
            const fire = (type, detail) => el.dispatchEvent(new view.MouseEvent(type, {
              bubbles: true, cancelable: true, composed: true, view, detail, button,
              buttons: type === "mousedown" ? (button === 2 ? 2 : 1) : 0,
              clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
            }));
            for (let count = 1; count <= (target.doubleClick ? 2 : 1); count++) {
              fire("mousedown", count);
              fire("mouseup", count);
              fire(target.rightClick ? "contextmenu" : "click", count);
            }
            if (target.doubleClick) fire("dblclick", 2);
            ripple(win, rect);
            return { role: el.getAttribute("role") || el.localName, label: labelOf(el) };
          });
        },

        // Updates a local build of an add-on that is already installed, so a
        // developer's change reaches Thunderbird without the file dialog. It
        // cannot add an add-on, and leaves signed ones (from
        // addons.thunderbird.net) alone.
        async updateAddon(path) {
          return reporting(async () => {
            const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
            if (!/\.xpi$/i.test(path) || !PathUtils.isAbsolute(path)) throw new ExtensionError(`${path} is not an absolute .xpi path`);
            if (!(await IOUtils.exists(path))) throw new ExtensionError(`${path} does not exist`);
            const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
            file.initWithPath(path);
            const install = await AddonManager.getInstallForFile(file);
            const incoming = install?.addon;
            if (!incoming || install.state === AddonManager.STATE_DOWNLOAD_FAILED) {
              throw new ExtensionError(`${path} is not an add-on Thunderbird can read`);
            }
            const current = await AddonManager.getAddonByID(incoming.id);
            if (!current) throw new ExtensionError(`${incoming.id} is not installed; install it by hand the first time`);
            if (current.signedState > AddonManager.SIGNEDSTATE_MISSING) {
              throw new ExtensionError(`${incoming.id} is a signed add-on; only local builds are updated this way`);
            }
            const result = { id: incoming.id, name: current.name, from: current.version, to: incoming.version };
            // Updating this add-on restarts it, which would drop the reply;
            // answer first
            if (incoming.id === extension.id) {
              Services.tm.dispatchToMainThread(() => install.install());
              return { ...result, restarting: true };
            }
            await install.install();
            return result;
          });
        },

        async type(target) {
          return reporting(async () => {
            const win = mainWindow();
            const el = findElement(win, target.ref, target.text);
            if (!isEditable(el)) throw new ExtensionError(`"${labelOf(el)}" is not a field you can type into`);
            await showAction(win, windowRect(el), target.status);
            el.focus();
            const view = el.ownerDocument.defaultView;
            if (el.isContentEditable) {
              if (target.clear) el.ownerDocument.execCommand("selectAll");
              el.ownerDocument.execCommand("insertText", false, target.value);
            } else {
              el.value = (target.clear ? "" : el.value) + target.value;
              el.dispatchEvent(new view.InputEvent("input", { bubbles: true, composed: true,
                inputType: "insertText", data: target.value }));
              el.dispatchEvent(new view.Event("change", { bubbles: true }));
            }
            // Read before Enter: a search box empties itself once submitted
            const value = el.isContentEditable ? el.textContent.slice(0, 200) : el.value;
            if (target.submit) pressOn(el, "Enter");
            ripple(win, windowRect(el));
            return { label: labelOf(el), value };
          });
        },

        async pressKey(key, shift, status) {
          return reporting(async () => {
            if (!(key in KEY_CODES)) throw new ExtensionError(`${key} is not a key the agent may press`);
            const win = mainWindow();
            const el = focusedElement(win);
            if (!el) throw new ExtensionError("Nothing has focus to press a key in");
            await showAction(win, windowRect(el), status);
            pressOn(el, key, shift);
            ripple(win, windowRect(el));
            return { key: shift ? `Shift+${key}` : key, on: labelOf(el) || el.localName };
          });
        },

        // Errors and warnings from Thunderbird and its add-ons, and console
        // calls made in this (the parent) process, newest last. Add-on pages
        // log in their own process, which this store does not see.
        async consoleMessages(sinceMs, contains, errorsOnly, limit) {
          return reporting(async () => {
            const since = Date.now() - sinceMs;
            const wanted = (contains || "").toLowerCase();
            const out = [];
            for (const m of Services.console.getMessageArray() || []) {
              if ((m.timeStamp || 0) < since) continue;
              if (m instanceof Ci.nsIScriptError) {
                const level = m.flags & Ci.nsIScriptError.warningFlag ? "warning" : "error";
                out.push({ time: m.timeStamp, level, text: m.errorMessage,
                  source: m.sourceName ? `${m.sourceName}:${m.lineNumber}` : "" });
              } else {
                out.push({ time: m.timeStamp, level: "info", text: m.message, source: "" });
              }
            }
            let consoleApiEvents = [];
            try {
              consoleApiEvents = Cc["@mozilla.org/consoleAPI-storage;1"].getService(Ci.nsIConsoleAPIStorage).getEvents();
            } catch (error) {
              // no console.log store in this build: errors and warnings above still count
            }
            for (const e of consoleApiEvents) {
              if ((e.timeStamp || 0) < since) continue;
              const text = (e.arguments || []).map((a) => {
                try {
                  return typeof a === "string" ? a : JSON.stringify(a);
                } catch (error) {
                  return String(a);
                }
              }).join(" ");
              out.push({ time: e.timeStamp, level: e.level === "warn" ? "warning" : e.level, text,
                source: e.filename ? `${e.filename}:${e.lineNumber}` : "" });
            }
            const kept = out
              .filter((m) => !errorsOnly || m.level === "error")
              .filter((m) => !wanted || `${m.text} ${m.source}`.toLowerCase().includes(wanted))
              .sort((a, b) => a.time - b.time);
            return { messages: kept.slice(-limit).map((m) => ({ ...m, text: String(m.text).slice(0, 1000),
              time: new Date(m.time).toISOString() })), total: kept.length };
          });
        },

        async hideAgent() {
          for (const win of Services.wm.getEnumerator("mail:3pane")) hide(win);
        },

        // Pairs without copying the token by hand. Only this one file, at a
        // fixed path (PAIRING_TOKEN_FILE in server.py), is ever read.
        async readPairingToken() {
          const path = PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ".foxmcp", "pairing-token");
          try {
            return (await IOUtils.readUTF8(path)).trim();
          } catch (error) {
            if (DOMException.isInstance(error) && error.name === "NotFoundError") return null;
            throw new ExtensionError(`Could not read ${path}: ${error.message}`);
          }
        },
      },
    };
  }
};
