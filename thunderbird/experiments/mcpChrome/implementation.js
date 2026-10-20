/* globals ExtensionAPI, Services */
"use strict";

// Errors of this type reach the add-on with their message; anything else
// arrives as "An unexpected error occurred"
const { ExtensionError } = ChromeUtils.importESModule("resource://gre/modules/ExtensionUtils.sys.mjs").ExtensionUtils;
const XHTML = "http://www.w3.org/1999/xhtml";
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
    const hit = node.ownerDocument.elementFromPoint(x, y);
    if (!hit || !(hit === node || node.contains(hit))) return false;
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

function shownElements(win) {
  measureErrors = { count: 0, first: "" };
  return documents(win).flatMap((doc) => [...doc.querySelectorAll(INTERACTIVE)]).filter((el) => {
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

// --- The API ----------------------------------------------------------------

var mcpChrome = class extends ExtensionAPI {
  getAPI() {
    return {
      mcpChrome: {
        async captureWindow(scale) {
          const win = mainWindow();
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
