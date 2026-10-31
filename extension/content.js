/*
 * FoxMCP Firefox Extension - Content Script
 * Copyright (c) 2024 FoxMCP Project
 * Licensed under the MIT License - see LICENSE file for details
 */


// Elements a snapshot lists. Anything clickable or editable, plus headings so
// the listing reads as an outline of the page rather than a bag of controls.
const INTERACTIVE_SELECTOR = [
  'a[href]', 'button', 'input', 'select', 'textarea', 'summary', 'label',
  '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=switch]',
  '[role=tab]', '[role=menuitem]', '[role=option]', '[role=combobox]', '[role=textbox]',
  '[role=row]', '[contenteditable=""]', '[contenteditable=true]', '[tabindex]:not([tabindex="-1"])',
  'h1', 'h2', 'h3'
].join(',');

const REF_ATTR = 'data-foxmcp-ref';
let nextRef = 1;

function isVisible(el) {
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) {
    return false;
  }
  const style = getComputedStyle(el);
  return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
}

// A ref stays on its element across snapshots, so a ref read from one
// snapshot still works after the page re-renders other parts of itself.
function refFor(el) {
  let ref = el.getAttribute(REF_ATTR);
  if (!ref) {
    ref = 'r' + nextRef++;
    el.setAttribute(REF_ATTR, ref);
  }
  return ref;
}

function describe(el) {
  const role = el.getAttribute('role') || el.tagName.toLowerCase();
  const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
    el.title || el.alt || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const extras = [];
  if (el.type && el.tagName === 'INPUT') extras.push('type=' + el.type);
  if (el.disabled) extras.push('disabled');
  if (el.checked) extras.push('checked');
  if (el.getAttribute('aria-selected') === 'true') extras.push('selected');
  return `[${refFor(el)}] ${role} "${name}"${extras.length ? ' (' + extras.join(', ') + ')' : ''}`;
}

function snapshot(maxElements) {
  const lines = [];
  for (const el of document.querySelectorAll(INTERACTIVE_SELECTOR)) {
    if (!isVisible(el)) continue;
    lines.push(describe(el));
    if (lines.length >= maxElements) {
      lines.push(`... truncated at ${maxElements} elements`);
      break;
    }
  }
  return lines.join('\n');
}

// What scrolling at this element moves: the nearest container that can still go
// that way, as a wheel would pick. Feeds such as Facebook's scroll in an inner
// container and leave the document at 0, so keys and window.scrollBy do nothing.
function scrollerFor(el, dx, dy) {
  const can = node => {
    const style = getComputedStyle(node);
    const free = value => value === 'auto' || value === 'scroll' || value === 'overlay';
    const down = free(style.overflowY) && (dy > 0 ? node.scrollTop + node.clientHeight < node.scrollHeight - 1 : dy < 0 && node.scrollTop > 0);
    const across = free(style.overflowX) && (dx > 0 ? node.scrollLeft + node.clientWidth < node.scrollWidth - 1 : dx < 0 && node.scrollLeft > 0);
    return down || across;
  };
  let innermost = null;
  for (let node = el; node && node !== document.documentElement; node = node.parentElement || node.getRootNode().host) {
    if (node === document.body) continue;
    if (can(node)) return node;
    const overflow = getComputedStyle(node).overflowY;
    if (!innermost && (overflow === 'auto' || overflow === 'scroll') && node.scrollHeight > node.clientHeight) innermost = node;
  }
  const page = document.scrollingElement || document.documentElement;
  // Nothing can go that way: answer for the container the agent was after,
  // not the page behind it, which may not scroll at all
  return page.scrollHeight > page.clientHeight ? page : innermost || page;
}

const SCROLL_SHOWN_MS = 500;

// Where the cursor goes for a scroll. Not targetPoint: bringing the element into
// view would itself scroll, so this is the part of it already on screen.
function scrollPoint(data) {
  if (hasTarget(data)) {
    const el = findTarget(data);
    const rect = el.getBoundingClientRect();
    const left = Math.max(rect.left, 0), right = Math.min(rect.right, innerWidth);
    const top = Math.max(rect.top, 0), bottom = Math.min(rect.bottom, innerHeight);
    const onScreen = right > left && bottom > top;
    return { el, x: onScreen ? (left + right) / 2 : innerWidth / 2, y: onScreen ? (top + bottom) / 2 : innerHeight / 2 };
  }
  const x = data.x ?? innerWidth / 2, y = data.y ?? innerHeight / 2;
  return { x, y, el: document.elementFromPoint(x, y) || document.body };
}

function scrollAt(data, el) {
  const dx = data.deltaX || 0, dy = data.deltaY || 0;
  const scroller = scrollerFor(el, dx, dy);
  const before = { x: scroller.scrollLeft, y: scroller.scrollTop };
  scroller.scrollBy({ left: dx, top: dy, behavior: 'instant' });
  const moved = { x: scroller.scrollLeft - before.x, y: scroller.scrollTop - before.y };
  return {
    scroller: scroller === document.scrollingElement ? 'the page' : describe(scroller),
    movedX: Math.round(moved.x), movedY: Math.round(moved.y),
    top: Math.round(scroller.scrollTop),
    remaining: Math.round(scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop),
  };
}

// Where an action happens: an element (by ref or selector), scrolled into view,
// or a point given as x and y in the viewport, for canvases. Returns the point
// and whatever element is there.
function targetPoint({ ref, selector, targetName, x, y }) {
  if (ref || selector || targetName) {
    const el = findTarget({ ref, selector, targetName });
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const rect = el.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, el, size: Math.min(rect.width, rect.height) };
  }
  if (typeof x !== 'number' || typeof y !== 'number') {
    throw new Error('Give an element (ref or selector) or a point (x and y)');
  }
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
    throw new Error(`The point ${x},${y} is outside the visible page (${innerWidth}x${innerHeight})`);
  }
  return { x, y, el: document.elementFromPoint(x, y) || document.body, size: 24 };
}

function mouseInit(point, extra = {}) {
  return { bubbles: true, cancelable: true, composed: true, view: window, clientX: point.x, clientY: point.y,
    screenX: point.x + window.screenX, screenY: point.y + window.screenY, pointerId: 1, pointerType: 'mouse',
    isPrimary: true, button: 0, ...extra };
}

// Scripted hover: what a mouse moving onto the point fires
function hoverAt(point) {
  const init = mouseInit(point);
  for (const type of ['pointerover', 'pointerenter', 'pointermove']) point.el.dispatchEvent(new PointerEvent(type, init));
  for (const type of ['mouseover', 'mouseenter', 'mousemove']) point.el.dispatchEvent(new MouseEvent(type, init));
}

// Scripted drag: the pointer and mouse events along the way, and the page's
// drag-and-drop events when the element it starts on is draggable
function usesPageDragAndDrop(el) {
  return Boolean(el.closest('[draggable="true"]') || (el.closest('a[href], img') && !el.closest('[draggable="false"]')));
}

// button: 0 left, 1 middle, 2 right. Canvas apps pan the view with the middle
// or right one held: plain pointer and mouse events, no slider or drag-and-drop.
async function dragBetween(from, to, steps = 12, button = 0) {
  const pan = button !== 0;
  const held = { button, buttons: [1, 4, 2][button] };
  // A slider ignores scripted mouse events: set the value the drop point means
  const range = pan ? null : from.el.closest('input[type="range"]');
  if (range) {
    const rect = range.getBoundingClientRect();
    const share = Math.min(1, Math.max(0, (to.x - rect.left) / rect.width));
    const min = Number(range.min || 0), max = Number(range.max || 100);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(range, String(min + share * (max - min)));
    range.dispatchEvent(new Event('input', { bubbles: true }));
    range.dispatchEvent(new Event('change', { bubbles: true }));
    return;
  }
  const transfer = new DataTransfer();
  const draggable = !pan && usesPageDragAndDrop(from.el) ? from.el.closest('[draggable="true"], a[href], img') : null;
  from.el.dispatchEvent(new PointerEvent('pointerdown', mouseInit(from, held)));
  from.el.dispatchEvent(new MouseEvent('mousedown', mouseInit(from, held)));
  if (draggable) draggable.dispatchEvent(new DragEvent('dragstart', { ...mouseInit(from), dataTransfer: transfer }));
  let over = from.el;
  for (let i = 1; i <= steps; i++) {
    const t = minimumJerk(i / steps);
    const point = { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
    const el = document.elementFromPoint(point.x, point.y) || document.body;
    el.dispatchEvent(new PointerEvent('pointermove', mouseInit(point, { buttons: held.buttons, button: -1 })));
    el.dispatchEvent(new MouseEvent('mousemove', mouseInit(point, { buttons: held.buttons })));
    if (draggable) {
      if (el !== over) el.dispatchEvent(new DragEvent('dragenter', { ...mouseInit(point), dataTransfer: transfer }));
      // A drop target says it takes the item by cancelling dragover, and the
      // cursor shows it the way the system's does
      const accepts = !el.dispatchEvent(new DragEvent('dragover', { ...mouseInit(point), dataTransfer: transfer }));
      if (settings.overlayEnabled) setCursorState(accepts ? 'drop' : 'nodrop');
    }
    over = el;
    await new Promise(resolve => setTimeout(resolve, 16));
  }
  const end = document.elementFromPoint(to.x, to.y) || document.body;
  if (draggable) {
    end.dispatchEvent(new DragEvent('drop', { ...mouseInit(to), dataTransfer: transfer }));
    draggable.dispatchEvent(new DragEvent('dragend', { ...mouseInit(to), dataTransfer: transfer }));
  }
  end.dispatchEvent(new PointerEvent('pointerup', mouseInit(to, { button })));
  end.dispatchEvent(new MouseEvent('mouseup', mouseInit(to, { button })));
}

function hasTarget(data) {
  return Boolean(data.ref || data.selector || data.targetName);
}

// Whether a person could see the element. A file input is usually hidden
// behind its button, so it counts as shown when the form or box holding it is.
function isShown(el) {
  const shown = node => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      (node.checkVisibility ? node.checkVisibility({ visibilityProperty: true }) : true);
  };
  if (shown(el)) return true;
  if (el instanceof HTMLInputElement && el.type === 'file') {
    const holder = el.closest('form') || el.parentElement;
    return Boolean(holder && shown(holder));
  }
  return false;
}

// The name a person would read off an element: its label, its text, or what
// a field says while empty
function nameOf(el) {
  const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
  const text = el.getAttribute('aria-label') || el.getAttribute('alt') || label || el.getAttribute('placeholder') ||
    (el.tagName === 'INPUT' && /^(button|submit|reset)$/.test(el.type) ? el.value : '') ||
    (el.innerText && el.innerText.length < 200 ? el.innerText : '') || el.getAttribute('title') || '';
  return text.replace(/\s+/g, ' ').trim().toLowerCase();
}

const NAMED_ELEMENTS = 'a, button, input, select, textarea, summary, label, img, [role], [aria-label], [title], ' +
  '[placeholder], [contenteditable=""], [contenteditable="true"], li, td, th, span, div, p, h1, h2, h3, h4, h5, h6';
const ACTIVE_ELEMENTS = 'a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=menuitem], ' +
  '[role=option], [role=tab], [role=checkbox], [role=radio], [role=switch], [contenteditable=""], [contenteditable="true"]';

// An element found by the name a person sees on it, the way they would point
// at it: shown before hidden, inside a modal dialog in front before the page
// behind it (a person cannot reach past it, and "Editor" matched a table cell
// behind the dialog instead of the option in it), an exact name before one that starts or merely
// contains it, something that can be clicked or typed into before plain text,
// and the innermost element before the boxes around it
// Compares two rankings place by place, the first difference deciding
const outranks = (a, b) => {
  const i = a.findIndex((value, index) => value !== b[index]);
  return i >= 0 && a[i] > b[i];
};

function findByName(name) {
  const wanted = name.replace(/\s+/g, ' ').trim().toLowerCase();
  const modals = frontDialogs(MODALS).filter(blocksPage);
  let best = null;
  for (const el of document.querySelectorAll(NAMED_ELEMENTS)) {
    const have = nameOf(el);
    if (!have) continue;
    const match = have === wanted ? 3 : have.startsWith(wanted) ? 2 : have.includes(wanted) ? 1 : 0;
    if (!match) continue;
    const inModal = modals.some(o => o.contains(el)) ? 1 : 0;
    const score = [isShown(el) ? 1 : 0, inModal, match, el.matches(ACTIVE_ELEMENTS) ? 1 : 0,
      -el.getElementsByTagName('*').length];
    if (!best || outranks(score, best.score)) best = { el, score };
  }
  if (!best) throw new Error(`Nothing on the page is named "${name}"`);
  // A label stands for the field it names
  if (best.el instanceof HTMLLabelElement && best.el.control) return best.el.control;
  return best.el;
}

// When a selector matches more than one element, the first one a person can
// see: pages keep hidden copies of composers, menus and dialogs, and acting on
// the hidden one does nothing visible (ChatGPT has two composers)
function findTarget({ ref, selector, targetName }) {
  if (ref) {
    const el = document.querySelector(`[${REF_ATTR}="${CSS.escape(ref)}"]`);
    if (!el) throw new Error(`No element with ref ${ref}; take a new snapshot`);
    return el;
  }
  if (targetName && !selector) return findByName(targetName);
  const all = [...document.querySelectorAll(selector)];
  if (!all.length) throw new Error(`No element matches ${selector}`);
  return all.find(isShown) || all[0];
}

// What a click changed that the agent cannot see in its own result: the
// address, the focus (a text editor opening), menus and dialogs, and the
// clicked element's own expanded or checked state. Canvas apps change only
// pixels, so "nothing" is reported as nothing in these.
const OVERLAYS = '[role=dialog], [role=alertdialog], [role=menu], [role=listbox], dialog[open], [aria-modal=true]';

function pageState(el) {
  const overlays = [...document.querySelectorAll(OVERLAYS)].filter(isShown);
  const focus = document.activeElement;
  return {
    url: location.href, el,
    overlays, focus,
    editing: Boolean(focus && focus !== document.body &&
      (focus.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(focus.tagName))),
    expanded: el?.getAttribute?.('aria-expanded'),
    checked: el ? el.checked ?? el.getAttribute?.('aria-checked') : undefined,
  };
}

const pendingClicks = new Map();
// Real key or pointer events the page has had since real input was prepared.
// None at all is a sign the input never reached the page (on Windows, a
// window behind another one, or one Firefox does not deliver to), which the
// result then says. Only a sign: input can land where this page cannot see it
// (an iframe, the browser itself), so it is never done again because of it.
// Windows typing posts characters without a key down, hence keypress and
// beforeinput beside keydown.
const realInputWatches = new Map();
const REAL_INPUT_WATCH_MS = 30000;
const LATE_INPUT_MS = 1000;
const REAL_EVENTS = { keys: ['keydown', 'keypress', 'beforeinput'], pointer: ['pointerdown', 'mousedown', 'click'] };

function watchRealInput(token, kind) {
  const watch = { count: 0 };
  const count = event => { if (event.isTrusted) watch.count++; };
  for (const type of REAL_EVENTS[kind]) window.addEventListener(type, count, true);
  // A question that never comes (an older server, a failed real input) stops it too
  const timer = setTimeout(() => watch.stop(), REAL_INPUT_WATCH_MS);
  watch.stop = () => {
    for (const type of REAL_EVENTS[kind]) window.removeEventListener(type, count, true);
    clearTimeout(timer);
    realInputWatches.delete(token);
  };
  realInputWatches.set(token, watch);
}

// Input aimed into a frame is seen by the frame's document, not this one
const inFrame = el => el instanceof HTMLIFrameElement || el instanceof HTMLFrameElement ||
  el instanceof HTMLObjectElement || el instanceof HTMLEmbedElement;

async function realInputArrived(token) {
  const watch = realInputWatches.get(token);
  if (!watch) return null;
  for (let waited = 0; !watch.count && waited < LATE_INPUT_MS; waited += 100) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  watch.stop();
  return watch.count > 0;
}

const DIALOGS = '[role=dialog], [role=alertdialog], dialog[open], [aria-modal=true]';
const MODALS = 'dialog:modal, [aria-modal=true]';

// Dialogs that are open and in front: what is at a dialog's own centre is part
// of it. Hit testing ignores opacity, so a dialog stuck invisible mid-fade still
// counts; `visible` leaves those out, for ranking what a person can see.
function frontDialogs(selector, visible = true) {
  return [...document.querySelectorAll(selector)].filter(dialog => {
    if (!isShown(dialog) || (visible && !dialog.checkVisibility?.({ opacityProperty: true }))) return false;
    const rect = dialog.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return Boolean(hit && dialog.contains(hit));
  });
}

// A backdrop: something laid over the whole page, in front of everything else
function coversPage(el) {
  if (el === document.documentElement || el === document.body) return false;
  const rect = el.getBoundingClientRect();
  return rect.width >= innerWidth * 0.9 && rect.height >= innerHeight * 0.9;
}

// Whether a dialog shuts out the page behind it, as a modal one does. Some
// cookie banners say aria-modal yet leave the page usable, so a corner of the
// page outside the dialog must land on it or on a backdrop.
function blocksPage(dialog) {
  if (dialog.matches('dialog:modal')) return true;
  const rect = dialog.getBoundingClientRect();
  const corner = [[2, 2], [innerWidth - 3, 2], [2, innerHeight - 3], [innerWidth - 3, innerHeight - 3]]
    .find(([x, y]) => x < rect.left || x > rect.right || y < rect.top || y > rect.bottom);
  if (!corner) return true;
  const hit = document.elementFromPoint(...corner);
  return Boolean(hit && (dialog.contains(hit) || coversPage(hit)));
}

// What a person's click on el would land on instead, said in the click's
// result: an invisible backdrop left by a modal stuck mid-fade took every
// click on a Save button, and the clicks were reported as done. Checked once
// el is scrolled into view, before the click.
function coverOf(el) {
  // A link that wraps has a box around both lines, whose centre can be other
  // text; a real click goes to the first line
  const rect = el.getClientRects()[0];
  if (!rect || !rect.width || !rect.height) return '';
  const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
  // A click anywhere in a field's label (a hidden checkbox behind a toggle) is a click on the field
  if (!hit || hit === el || el.contains(hit) || hit.contains(el) || hit.closest('label')?.control === el) return '';
  let invisible = false;
  for (let node = hit; node && !invisible; node = node.parentElement) {
    const style = getComputedStyle(node);
    invisible = style.opacity === '0' || style.visibility === 'hidden';
  }
  const kind = typeof hit.className === 'string' && hit.className.trim() ? `.${hit.className.trim().split(/\s+/)[0]}` : '';
  let note = `it is covered by ${invisible ? 'an invisible ' : ''}${describe(hit)}${kind}, ` +
    `where a person's click would land`;
  // The dialog the cover belongs to, or the one in front of a backdrop
  const dialog = frontDialogs(DIALOGS, false)
    .filter(o => !o.contains(el) && (o.contains(hit) || coversPage(hit))).pop();
  if (dialog) note += `; open in front: ${describe(dialog)}`;
  return note;
}

// What a click changed: at once if something already has, else after a moment
// for the page to react, since most clicks do nothing visible to these checks
const CLICK_EFFECT_MS = [80, 300];
async function clickEffect(before) {
  await new Promise(resolve => setTimeout(resolve, CLICK_EFFECT_MS[0]));
  const seen = pageState(before.el && before.el.isConnected ? before.el : null);
  // Only a change that is the outcome: focus and aria-expanded move first, and
  // the dialog or menu they lead to can still be on its way
  const done = seen.url !== before.url || seen.overlays.length !== before.overlays.length
    || (before.el && !before.el.isConnected);
  if (!done) await new Promise(resolve => setTimeout(resolve, CLICK_EFFECT_MS[1] - CLICK_EFFECT_MS[0]));
  const after = pageState(before.el && before.el.isConnected ? before.el : null);
  const changes = [];
  if (after.url !== before.url) changes.push(`the page went to ${after.url}`);
  const opened = after.overlays.filter(o => !before.overlays.includes(o));
  const closed = before.overlays.filter(o => !after.overlays.includes(o));
  for (const o of opened) changes.push(`opened ${describe(o)}`);
  if (closed.length) changes.push(`closed ${closed.length === 1 ? 'a' : closed.length} menu or dialog`);
  if (after.focus !== before.focus && after.editing) changes.push(`the caret is now in ${describe(after.focus)}`);
  else if (before.editing && !after.editing) changes.push('the caret left the field it was in');
  if (before.el && !before.el.isConnected) changes.push('the clicked element went away');
  if (after.expanded !== before.expanded && after.expanded != null) {
    changes.push(after.expanded === 'true' ? 'it expanded' : 'it collapsed');
  }
  if (after.checked !== before.checked && after.checked != null) {
    changes.push(after.checked === true || after.checked === 'true' ? 'it is now checked' : 'it is now unchecked');
  }
  const effect = changes.length ? changes.join('; ')
    : 'no change to the address, focus, menus or dialogs (a canvas may still have changed)';
  return before.covered ? `${before.covered}; ${effect}` : effect;
}

// Sites listen on pointerdown or mousedown as often as on click (Google Drive's
// menus open on mousedown), so the whole sequence is dispatched at the element's
// centre rather than calling el.click(). A double click repeats the sequence
// with the click count in `detail` and ends with dblclick, which is what opens
// a folder in Google Drive; a lone dblclick event is ignored there.
// clicks: 1, 2 for a double-click, or 'right' for a right-click
function clickElement(el, clicks = 1) {
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const rect = el.getBoundingClientRect();
  const init = {
    bubbles: true, cancelable: true, composed: true, view: window, button: 0,
    clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2
  };
  el.dispatchEvent(new PointerEvent('pointerover', init));
  el.dispatchEvent(new MouseEvent('mouseover', init));
  if (clicks === 'right') {
    const right = { ...init, button: 2, buttons: 2 };
    el.dispatchEvent(new PointerEvent('pointerdown', right));
    el.dispatchEvent(new MouseEvent('mousedown', right));
    el.dispatchEvent(new PointerEvent('pointerup', { ...right, buttons: 0 }));
    el.dispatchEvent(new MouseEvent('mouseup', { ...right, buttons: 0 }));
    el.dispatchEvent(new MouseEvent('contextmenu', right));
    return;
  }
  for (let detail = 1; detail <= clicks; detail++) {
    el.dispatchEvent(new PointerEvent('pointerdown', { ...init, detail, buttons: 1 }));
    el.dispatchEvent(new MouseEvent('mousedown', { ...init, detail, buttons: 1 }));
    if (typeof el.focus === 'function') el.focus();
    el.dispatchEvent(new PointerEvent('pointerup', { ...init, detail }));
    el.dispatchEvent(new MouseEvent('mouseup', { ...init, detail }));
    el.dispatchEvent(new MouseEvent('click', { ...init, detail }));
  }
  if (clicks === 2) {
    el.dispatchEvent(new MouseEvent('dblclick', { ...init, detail: 2 }));
  }
}

// insertText goes through the editor's own input path, so frameworks that track
// value through input events (React, Vue) see the change. Setting .value directly
// is the fallback for inputs where the command is refused.
//
// The text goes in a few characters at a time, spread over at most
// TYPE_ANIMATION_MS, so a person watching sees it being written. Pages get one
// input event per piece, as they would from a keyboard.
const TYPE_ANIMATION_MS = 1000;
const TYPE_STEP_MS = 25;
// How long commitField gives a page to turn typed text into a chip
const COMMIT_SETTLE_MS = 300;

// Focusing an editable element does not put the caret inside it, and
// insertText with no caret in the element is refused.
//
// The caret goes at the end of the last text, not at the end of the element:
// after the last <p> of a ProseMirror editor (ChatGPT) each piece typed there
// became a paragraph of its own, splitting words across lines.
function caretToEnd(el) {
  const range = document.createRange();
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let last = null;
  while (walker.nextNode()) last = walker.currentNode;
  if (last) {
    range.setStart(last, last.length);
  } else {
    let deepest = el;
    while (deepest.lastElementChild && deepest.lastElementChild.tagName !== 'BR') deepest = deepest.lastElementChild;
    range.selectNodeContents(deepest);
  }
  range.collapse(false);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

// The editor a target belongs to: a ref or selector can name one paragraph of
// it, and clearing or checking that paragraph alone left the rest of a draft
function editingHost(el) {
  let host = el;
  while (host.parentElement?.isContentEditable) host = host.parentElement;
  return host;
}

// A <select> takes no text, so typing into one picks the option whose label
// or value is the text, as a person choosing it from the list would. Labels
// win: they are what a snapshot shows, and a value can be another option's
// label (<option value="8">6</option><option value="10">8</option>). Setting
// it as an input's value threw "'set value' called on an object that does not
// implement interface HTMLInputElement".
function chooseOption(select, text, clear) {
  if (select.disabled) throw new Error('The list is disabled');
  const options = [...select.options];
  const label = text.trim().toLowerCase();
  const option = options.find(o => o.label.trim().toLowerCase() === label) || options.find(o => o.value === text);
  // A disabled option (sold out, say) is one a person cannot pick, and the form
  // leaves it out of what it sends
  if (option?.disabled || option?.parentElement.closest('optgroup[disabled]')) {
    throw new Error(`The option "${option.label.trim()}" is disabled`);
  }
  if (!option) {
    const names = options.map(o => `"${o.label.trim()}"`);
    throw new Error(`The list has no option "${text}". Its options: ${names.slice(0, 40).join(', ')}` +
      (names.length > 40 ? `, and ${names.length - 40} more` : ''));
  }
  select.scrollIntoView({ block: 'center' });
  select.focus();
  if (select.multiple && clear) options.forEach(o => { o.selected = false; });
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  showTypingField(select);
  return select;
}

async function typeInto(el, text, clear) {
  if (el instanceof HTMLSelectElement) return chooseOption(el, text, clear);
  changeSent.delete(el);
  // Found before typing: replacing the editor's contents can remove el itself
  const host = el.isContentEditable ? editingHost(el) : el;
  el.scrollIntoView({ block: 'center' });
  el.focus();
  if (clear) {
    if (el.isContentEditable) {
      // selectAll does not stay inside some editors (YouTube Studio's title),
      // and the new text was added after the old instead of replacing it
      const range = document.createRange();
      range.selectNodeContents(host);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    } else if (typeof el.select === 'function') {
      el.select();
    }
  } else if (el.isContentEditable) {
    caretToEnd(el);
  } else {
    // Focus leaves the caret at the start, and the first piece went in there.
    // Email and number inputs have no selection (selectionStart is null) and
    // throw on setSelectionRange.
    if (el.selectionStart != null) el.setSelectionRange(el.value.length, el.value.length);
  }
  // In a hidden tab the text goes in at once: nobody is watching, and Firefox
  // throttles its timers so hard that a 300-character text took minutes.
  // Typing animation can also be turned off in the settings
  // Rich editors get the whole text at once too: YouTube Studio's fields
  // re-render while they suggest @mentions, and every piece typed after that
  // was lost (36 of 78 characters reached the title).
  const size = document.hidden || !settings.typingAnimated || el.isContentEditable ? Math.max(1, text.length)
    : Math.max(1, Math.ceil(text.length * TYPE_STEP_MS / TYPE_ANIMATION_MS));
  const pieces = [];
  for (let i = 0; i < text.length; i += size) pieces.push(text.slice(i, i + size));
  if (pieces.length === 0) pieces.push('');
  for (let i = 0; i < pieces.length; i++) {
    if (i > 0) {
      await new Promise(resolve => setTimeout(resolve, TYPE_STEP_MS));
      // An autocomplete popup can take focus between pieces; the rest of the
      // text still belongs in this field.
      if (document.activeElement !== el && !el.contains(document.activeElement)) {
        el.focus();
      }
      // Rich editors (ProseMirror in ChatGPT) can leave the piece just inserted
      // selected, and the next piece would then replace it, so only the last
      // few dozen characters of a long text arrived.
      if (el.isContentEditable) caretToEnd(el);
      else if (el.selectionStart != null) el.setSelectionRange(el.value.length, el.value.length);
    }
    if (!document.execCommand('insertText', false, pieces[i])) {
      if (i > 0) throw new Error('The field stopped accepting text partway through typing');
      break;
    }
    showTypingField(host);
    if (i === pieces.length - 1) {
      checkTyped(host, text, clear);
      return host;
    }
  }
  if (el.isContentEditable) {
    if (clear) host.textContent = text;
    else el.textContent += text;
    host.dispatchEvent(new Event('input', { bubbles: true }));
    showTypingField(host);
    return host;
  }
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, clear ? text : el.value + text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  changeSent.add(el);
  showTypingField(el);
  return el;
}

// Whether the text that was typed is all there. An editor that drops or replaces
// part of it would otherwise report success with half a message in the field.
function checkTyped(el, text, cleared) {
  // Editors keep zero-width fillers beside the caret (CKEditor's \u2060,
  // Froala's \u200B), which \s does not match
  const normalise = value => value.replace(/[\u200B-\u200D\u2060]/g, '').replace(/\s+/g, ' ').trim();
  const now = normalise(el.isContentEditable ? editingHost(el).innerText : el.value || '');
  const wanted = normalise(text);
  if (!now.includes(wanted)) {
    throw new Error(`Only part of the text arrived in the field (it now holds ${now.length} characters, ` +
      `the text was ${wanted.length})`);
  }
  // A clear that did not take leaves the old text beside the new
  if (cleared && now !== wanted) {
    throw new Error(`The field was not cleared: it holds ${now.length} characters, the text was ${wanted.length}`);
  }
}

// Window messages arrive one at a time and editors apply them on their own
// schedule, so the text can still be coming in. Reporting it as partial then
// tempts a retry, which doubles the text. So: a verdict only once the field has
// stopped changing.
const TYPED_STILL_MS = 1000;
const TYPED_WAIT_MAX_MS = 15000;
async function checkTypedSettled(el, text, cleared) {
  const started = Date.now();
  let length = -1, changedAt = started;
  for (;;) {
    try {
      return checkTyped(el, text, cleared);
    } catch (error) {
      const now = (el.isContentEditable ? editingHost(el).innerText : el.value || '').length;
      if (now !== length) { length = now; changedAt = Date.now(); }
      if (Date.now() - changedAt >= TYPED_STILL_MS || Date.now() - started >= TYPED_WAIT_MAX_MS) throw error;
    }
    const before = Date.now();
    await new Promise(resolve => setTimeout(resolve, 100));
    // A busy page is not a still one: it may be about to apply the rest
    if (Date.now() - before > 3 * 100) changedAt = Date.now();
  }
}

// The field keys go to when content_type names none, or an error: without one
// they would land on the page's own shortcuts
function focusedField() {
  const el = document.activeElement;
  // A space on a focused checkbox or submit button would press it
  const notText = ['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'color', 'range', 'hidden'];
  if (el && (el.isContentEditable || (el instanceof HTMLTextAreaElement && !el.readOnly && !el.disabled)
      || (el instanceof HTMLInputElement && !el.readOnly && !el.disabled && !notText.includes(el.type)))) return el;
  throw new Error('No ref, selector or field was given and no editable field in this page has the focus ' +
    '(one in a frame or a shadow root cannot be seen from here)');
}

// Where added text goes: the click that focused the field landed mid-text, and
// Control+End does not move the caret on every platform or through window input
function caretToEnd(el) {
  if (!el.isContentEditable) {
    if (el.setSelectionRange && typeof el.value === 'string') {
      // Number and email inputs throw here, and keep their caret at the end anyway
      try { el.setSelectionRange(el.value.length, el.value.length); } catch (error) { /* see above */ }
    }
    return;
  }
  const selection = el.ownerDocument.getSelection();
  selection.selectAllChildren(editingHost(el));
  selection.collapseToEnd();
}

// Submits the form of a field the page replaced on blur, which a key press can
// no longer reach. Returns why it could not, for the tool to say so.
function submitReplacedField(form) {
  if (!form || !form.isConnected) return 'the page replaced the field and there is no form left to submit';
  if (!form.checkValidity()) return 'the form has a field that is not valid yet';
  form.requestSubmit();
  return null;
}

// The field typed into, or a note that the page replaced it, whose ref would
// lead nowhere
const typedField = el => el.isConnected ? describe(el) : 'a field the page has since replaced';

// Fields typeInto already sent a change to, which commitField must not repeat
const changeSent = new WeakSet();

// Leave a field the way a person clicking elsewhere does. Chip and token inputs
// (Google Analytics' "Enter email addresses") turn the text into an entry only
// when focus moves away; Enter submitted the form first. Says whether the field
// emptied itself or was replaced, which is how such inputs show they took the
// entry, or null for a <select>, which holds a choice rather than text. With
// refocus, focus goes back afterwards, so a real Enter that follows reaches the
// field as it would after a person clicked back into it.
async function commitField(el, refocus) {
  const field = el.isContentEditable ? editingHost(el) : el;
  let changed = false, blurred = false;
  const noteChange = () => { changed = true; };
  const noteBlur = () => { blurred = true; };
  field.addEventListener('change', noteChange, { once: true });
  field.addEventListener('blur', noteBlur, { once: true });
  field.blur();
  // In a window that is not in front, which is how agents mostly drive
  // Firefox, blur() fires nothing. Firefox still fires its own change while
  // it handles the blur dispatched here, so noteChange stays on until after.
  if (!blurred) {
    field.dispatchEvent(new FocusEvent('blur'));
    field.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
  }
  field.removeEventListener('change', noteChange);
  field.removeEventListener('blur', noteBlur);
  // chooseOption already sent a <select> its change
  if (field instanceof HTMLSelectElement) return null;
  // Firefox fires change on blur only for edits it counts as the user's
  if (!changed && !changeSent.has(field) && (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) {
    field.dispatchEvent(new Event('change', { bubbles: true }));
  }
  // Frameworks turn the text into a chip on their next render, not during blur
  await new Promise(resolve => setTimeout(resolve, COMMIT_SETTLE_MS));
  const emptied = !field.isConnected || !(field.isContentEditable ? field.innerText : field.value || '').trim();
  if (refocus && field.isConnected) field.focus();
  return emptied;
}

// Inputs a typed character does not go into. Date and time inputs take keys
// segment by segment, which insertText cannot do.
const NOT_TEXT_INPUTS = /^(button|checkbox|color|file|hidden|image|radio|range|reset|submit|date|datetime-local|month|time|week)$/;

const fieldText = el => el.isContentEditable ? el.textContent : el.value;

function pressKey(el, key) {
  const init = { key, bubbles: true, cancelable: true, composed: true };
  // A synthetic key event types nothing, so a printable key the page lets
  // through goes in the way content_type puts text in. Read-only fields (many
  // comboboxes) handle keys themselves.
  const typing = [...key].length === 1 && !el.readOnly && !el.disabled && (el.isContentEditable ||
    el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && !NOT_TEXT_INPUTS.test(el.type)));
  // A paragraph of an editor cannot take focus; the editor it belongs to can
  const field = el.isContentEditable ? editingHost(el) : el;
  const focused = () => document.activeElement === field || field.contains(document.activeElement);
  if (typing && !focused()) {
    field.focus();
    // A field that cannot take focus would have the key typed into whichever one has it
    if (!focused()) throw new Error(`${describe(el)} cannot take focus, so "${key}" was not typed`);
    // Focus leaves the caret at the start; a key pressed on a field goes after its text
    if (el.selectionStart != null) el.setSelectionRange(el.value.length, el.value.length);
  }
  // Focusing an editor does not move the selection into it: it stayed in the
  // last editor typed into, and the key went there
  if (typing && el.isContentEditable && !field.contains(window.getSelection().anchorNode)) caretToEnd(field);
  const before = typing ? fieldText(field) : null;
  const notCancelled = el.dispatchEvent(new KeyboardEvent('keydown', init));
  let refused = false;
  // A page that writes the key itself on keydown, or moves focus on (a box per
  // digit of a code), has handled it; typing it too would double it
  if (typing && notCancelled && focused() && fieldText(field) === before) {
    refused = !document.execCommand('insertText', false, key);
  }
  el.dispatchEvent(new KeyboardEvent('keyup', init));
  if (refused) throw new Error(`The field did not accept "${key}"`);
  // A synthetic Enter does not submit a form, so do what the browser would.
  if (key === 'Enter' && notCancelled && el.form && el.tagName !== 'TEXTAREA') {
    el.form.requestSubmit();
  }
}

// Many sites (Google Drive among them) open the file dialog by calling click()
// on an <input type=file> they create on the spot, and a native dialog is not
// something an extension can fill in. While armed, this swaps the page's own
// click() and showPicker() so the next file input is handed back here instead
// of opening the dialog. It goes through wrappedJSObject because the page's
// prototypes are invisible from the content script's side.
function captureNextFileInput(timeoutMs) {
  const proto = window.wrappedJSObject.HTMLInputElement.prototype;
  const originalClick = proto.click;
  const originalShowPicker = proto.showPicker;
  return new Promise((resolve, reject) => {
    const restore = () => {
      proto.click = originalClick;
      proto.showPicker = originalShowPicker;
    };
    const timer = setTimeout(() => {
      restore();
      reject(new Error('The trigger did not open a file dialog'));
    }, timeoutMs);
    const intercept = exportFunction(function () {
      if (this.type !== 'file') {
        return originalClick.call(this);
      }
      clearTimeout(timer);
      restore();
      // Inputs created just for the dialog are often never attached, and a
      // detached element cannot be found again by ref.
      if (!this.isConnected) {
        this.hidden = true;
        document.body.appendChild(this);
      }
      resolve(this);
    }, window);
    proto.click = intercept;
    proto.showPicker = intercept;
  });
}

// webkitRelativePath is what tells a page the files came from a folder (and
// which subfolder each sits in), so it is set where the page reads it.
async function setInputFiles(el, files, relativePaths) {
  if (el.tagName !== 'INPUT' || el.type !== 'file') {
    throw new Error('Target must be an <input type=file>');
  }
  // A page ignores files set on a disabled input (ChatGPT's, while it loads),
  // so setting them would report success with nothing attached.
  if (el.disabled) {
    throw new Error('The file input is disabled; wait for the page to finish loading and try again');
  }
  // Files built in the extension belong to it, and the page gets "Permission
  // denied" reading them. Each one is rebuilt in the page from its bytes and set
  // through the page's own DataTransfer, so it is an ordinary file as far as the
  // page can tell. A clone of the extension's File was not enough: Miro accepted
  // it, showed "Uploading..." and never finished, apparently because its
  // uploader passes the file on to a worker.
  const page = window.wrappedJSObject;
  const transfer = new page.DataTransfer();
  for (const file of files) {
    const bytes = cloneInto(await file.arrayBuffer(), window);
    const pageFile = new page.File(cloneInto([bytes], window), file.name,
      cloneInto({ type: file.type, lastModified: file.lastModified }, window));
    transfer.items.add(pageFile);
  }
  el.wrappedJSObject.files = transfer.files;
  const pageFiles = el.wrappedJSObject.files;
  // A browser only reports folder paths for inputs that pick folders, so a
  // folder given to a plain file input arrives as loose files, as it would.
  for (let i = 0; i < pageFiles.length; i++) {
    if (el.webkitdirectory && relativePaths[i]) {
      Object.defineProperty(pageFiles[i], 'webkitRelativePath', {
        value: relativePaths[i], configurable: true
      });
    }
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return Array.from({ length: pageFiles.length },
    (_, i) => pageFiles[i].webkitRelativePath || pageFiles[i].name);
}

// Overlay shown while an agent works in the tab: a glowing border, a label in
// the first person saying what it is doing, a cursor that is always on screen
// and animates the current state, and a pulsing ring on the tab's icon in the tab
// strip. It is purely visual (pointer-events: none) and stays until the agent
// releases the tab; the background script releases it when the agent's server
// goes, or after five minutes with no action.
const OVERLAY_ID = 'foxmcp-agent-overlay';
const OVERLAY_LABEL = 'AI agent is working in this tab';
// How long the ring stays on a field after the last piece of text goes in.
const FIELD_LINGER_MS = 800;
// After this long in "thinking" the cursor settles to idle.
const IDLE_AFTER_MS = 20000;
// grab: an open hand over what is about to be dragged; grabbing: the hand
// closed while the button is held (moving an item or panning a canvas); drop
// and nodrop: over a place that takes the dragged item, or one that does not
const CURSOR_STATES = ['idle', 'thinking', 'reading', 'clicking', 'typing', 'waiting', 'working',
  'grab', 'grabbing', 'drop', 'nodrop', 'scrolling'];
let fieldTimer = null;
let idleTimer = null;
let cursorPos = null;
let shownBriefly = false;
let cursorState = 'idle';
let agentLabel = OVERLAY_LABEL;

// The person's settings for what the overlay shows (settings.js), applied
// as they change them, without a reload
let settings = { ...SETTINGS_DEFAULTS };
loadSettings().then(saved => { settings = saved; applyOverlaySettings(); });
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  for (const [key, change] of Object.entries(changes)) {
    if (key in SETTINGS_DEFAULTS) settings[key] = change.newValue ?? SETTINGS_DEFAULTS[key];
  }
  applyOverlaySettings();
});

function applyOverlaySettings() {
  const host = document.getElementById(OVERLAY_ID);
  if (host) {
    host.style.setProperty('--c', settings.overlayColor);
    host.classList.toggle('no-border', !settings.overlayBorder);
    host.classList.toggle('no-label', !settings.overlayLabel);
    host.classList.toggle('no-cursor', !settings.cursorEnabled);
    host.classList.toggle('label-top', settings.labelPosition === 'top');
    if (!settings.overlayEnabled) host.style.opacity = '0';
  }
  if (!settings.tabMarker || !settings.overlayEnabled) stopTabMarker();
}

// The cursor takes the shape the system cursor would have at its spot, as the
// page's CSS sets it: an arrow, a pointing hand over links and buttons, an
// I-beam over text, an open hand over what can be dragged and a closed one
// while it is held, arrows for moving and resizing, and so on. Each shape is
// drawn on a 22px grid, outlined in white and filled (or stroked) in the
// overlay's colour; hot is the point of the shape that sits on the target.
const ARROW = '<path d="M2 1.5 L2 18 L6.6 13.9 L9.6 20.5 L12.5 19.2 L9.6 12.7 L15.8 12.7 Z"/>';
const RESIZE = '<path d="M11 1.5l4.2 5h-2.7v9h2.7l-4.2 5-4.2-5h2.7v-9H6.8z"';
const MAGNIFIER = '<circle cx="9.5" cy="9.5" r="6"/><path d="M14 14l6 6"/>';
const CURSOR_SHAPES = {
  arrow: { hot: [2, 1.5], draw: ARROW },
  point: { hot: [9.2, 1.5], draw: `<rect x="7.5" y="1.5" width="3.4" height="11" rx="1.7"/>
    <rect x="10.9" y="8" width="3.2" height="6" rx="1.6"/><rect x="14.1" y="9" width="3.2" height="5.5" rx="1.6"/>
    <rect x="2.2" y="10.6" width="3.2" height="6.4" rx="1.6" transform="rotate(-38 3.8 13.8)"/>
    <rect x="5.4" y="10.6" width="12" height="9.4" rx="4"/>` },
  open: { hot: [11, 11], draw: `<rect x="4.6" y="5.2" width="3.2" height="9" rx="1.6"/><rect x="7.8" y="2.6" width="3.2" height="10" rx="1.6"/>
    <rect x="11" y="3.2" width="3.2" height="10" rx="1.6"/><rect x="14.2" y="5" width="3.2" height="9" rx="1.6"/>
    <rect x="1.4" y="10.4" width="3.2" height="6.4" rx="1.6" transform="rotate(-38 3 13.6)"/>
    <rect x="4.6" y="9.6" width="12.8" height="9.6" rx="4.2"/>` },
  closed: { hot: [11, 13], draw: `<rect x="4.6" y="8" width="3.2" height="5" rx="1.6"/><rect x="7.8" y="7" width="3.2" height="5.4" rx="1.6"/>
    <rect x="11" y="7" width="3.2" height="5.4" rx="1.6"/><rect x="14.2" y="8" width="3.2" height="5" rx="1.6"/>
    <rect x="3.4" y="11.4" width="3" height="5" rx="1.5" transform="rotate(-25 4.9 13.9)"/>
    <rect x="4.6" y="10" width="12.8" height="9.2" rx="4.2"/>` },
  ibeam: { hot: [11, 11], line: true, draw: '<path d="M7.5 2.5h2.2c.7 0 1.3.6 1.3 1.3 0-.7.6-1.3 1.3-1.3h2.2M11 3.8v14.4M7.5 19.5h2.2c.7 0 1.3-.6 1.3-1.3 0 .7.6 1.3 1.3 1.3h2.2"/>' },
  move: { hot: [11, 11], draw: '<path d="M11 1l3.5 4h-2.3v4.8h4.8V7.5L21 11l-4 3.5v-2.3h-4.8V17h2.3L11 21l-3.5-4h2.3v-4.8H5v2.3L1 11l4-3.5v2.3h4.8V5H7.5z"/>' },
  notallowed: { hot: [11, 11], line: true, draw: '<circle cx="11" cy="11" r="7.5"/><path d="M5.7 5.7l10.6 10.6"/>' },
  crosshair: { hot: [11, 11], line: true, draw: '<path d="M11 2v7M11 13v7M2 11h7M13 11h7"/>' },
  // A mouse with its wheel, and the two ways it turns
  scroll: { hot: [11, 11], line: true, draw: '<rect x="7" y="5.5" width="8" height="11" rx="4"/><path d="M11 8v2.5"/><path d="M8.5 3L11 1l2.5 2M8.5 19l2.5 2 2.5-2"/>' },
  ns: { hot: [11, 11], draw: `${RESIZE}/>` },
  ew: { hot: [11, 11], draw: `${RESIZE} transform="rotate(90 11 11)"/>` },
  nwse: { hot: [11, 11], draw: `${RESIZE} transform="rotate(-45 11 11)"/>` },
  nesw: { hot: [11, 11], draw: `${RESIZE} transform="rotate(45 11 11)"/>` },
  zoomin: { hot: [9.5, 9.5], line: true, draw: `${MAGNIFIER}<path d="M6.5 9.5h6M9.5 6.5v6"/>` },
  zoomout: { hot: [9.5, 9.5], line: true, draw: `${MAGNIFIER}<path d="M6.5 9.5h6"/>` },
  // The arrow with a mark beside it
  progress: { hot: [2, 1.5], draw: ARROW },
  help: { hot: [2, 1.5], draw: ARROW },
  copy: { hot: [2, 1.5], draw: ARROW },
  alias: { hot: [2, 1.5], draw: ARROW },
};
// CSS cursor keywords to the shapes above
const CSS_CURSORS = {
  default: 'arrow', 'context-menu': 'arrow', none: 'arrow', pointer: 'point', text: 'ibeam', 'vertical-text': 'ibeam',
  grab: 'open', grabbing: 'closed', move: 'move', 'all-scroll': 'move', 'not-allowed': 'notallowed', 'no-drop': 'notallowed',
  crosshair: 'crosshair', cell: 'crosshair', 'ns-resize': 'ns', 'n-resize': 'ns', 's-resize': 'ns', 'row-resize': 'ns',
  'ew-resize': 'ew', 'e-resize': 'ew', 'w-resize': 'ew', 'col-resize': 'ew', 'nwse-resize': 'nwse', 'nw-resize': 'nwse',
  'se-resize': 'nwse', 'nesw-resize': 'nesw', 'ne-resize': 'nesw', 'sw-resize': 'nesw', wait: 'progress',
  progress: 'progress', help: 'help', 'zoom-in': 'zoomin', 'zoom-out': 'zoomout', copy: 'copy', alias: 'alias',
};
// While dragging, the hand: open on the way to the item and again on letting go,
// closed while the button is held
const HAND_SHAPES = { grab: 'open', grabbing: 'closed', drop: 'closed', nodrop: 'closed' };
// States that keep their own shape, whatever is under the cursor
const STATE_SHAPES = { ...HAND_SHAPES, scrolling: 'scroll' };

function cursorShapeSvg(name, { draw, line }) {
  const outline = line ? 'fill="none" stroke="#fff" stroke-width="4.4" stroke-linecap="round"'
    : 'fill="#fff" stroke="#fff" stroke-width="2.8" stroke-linejoin="round"';
  // Colour and hotspot come from the overlay's sheet: style attributes are
  // blocked on the same pages as <style> elements
  const inner = line ? 'class="line" fill="none" stroke-width="2" stroke-linecap="round"' : 'class="fill"';
  return `<svg class="cursor shape s-${name}" viewBox="0 0 22 22"
    xmlns="http://www.w3.org/2000/svg"><g ${outline}>${draw}</g><g ${inner}>${draw}</g></svg>`;
}

// The shape the system cursor would have at a point of the page
function shapeAt(x, y) {
  const el = document.elementFromPoint(x, y);
  if (!el) return 'arrow';
  const value = getComputedStyle(el).cursor.split(',').pop().trim();
  if (value !== 'auto') return CSS_CURSORS[value] || 'arrow';
  if (el.closest('a[href]')) return 'point';
  if (el.closest('textarea, [contenteditable=""], [contenteditable="true"], input:not([type=button], [type=submit], ' +
      '[type=reset], [type=checkbox], [type=radio], [type=range], [type=color], [type=file], [type=image])')) return 'ibeam';
  // Over the letters of a text, not the space beside it
  const caret = document.caretPositionFromPoint?.(x, y);
  const node = caret?.offsetNode;
  if (node && node.nodeType === Node.TEXT_NODE && node.length && node.data.trim()) {
    const range = document.createRange();
    const i = Math.min(caret.offset, node.length - 1);
    range.setStart(node, i);
    range.setEnd(node, i + 1);
    const box = range.getBoundingClientRect();
    if (x >= box.left - 2 && x <= box.right + 2 && y >= box.top && y <= box.bottom) return 'ibeam';
  }
  return 'arrow';
}

function overlayRoot() {
  let host = document.getElementById(OVERLAY_ID);
  // An overlay left by an older version of the add-on, from before it was
  // updated in a tab that stayed open, lacks the parts this one drives, or
  // has its styles in a <style> element a strict CSP blocks.
  if (host && (!host.shadowRoot?.querySelector('.pointer .s-point') || !host.shadowRoot.adoptedStyleSheets.length)) {
    host.remove();
    host = null;
  }
  if (!host) {
    host = document.createElement('div');
    host.id = OVERLAY_ID;
    host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;opacity:0;';
    const root = host.attachShadow({ mode: 'open' });
    // A constructed sheet, not a <style> element: pages whose CSP limits
    // style-src (Teams) block the element, and the overlay drew as plain
    // serif text with a cursor the size of a third of the window
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(`
        :host { transition: opacity .4s; --c: rgb(217,119,87); }
        :host(.no-border) .glow, :host(.no-label) .label, :host(.no-cursor) .pointer { display:none; }
        :host(.label-top) .label { bottom:auto; top:14px; }
        .glow { position:fixed; inset:0; box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--c) 90%, transparent), inset 0 0 22px color-mix(in srgb, var(--c) 50%, transparent);
          animation: glow 3s ease-in-out infinite; }
        @keyframes glow { 50% { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--c) 60%, transparent), inset 0 0 14px color-mix(in srgb, var(--c) 30%, transparent); } }
        .label { position:fixed; left:50%; bottom:14px; transform:translateX(-50%); display:flex; align-items:center; gap:8px;
          padding:7px 14px; border-radius:999px; background:rgba(28,25,23,.92); color:#fff; max-width:80vw;
          font:500 13px/1.2 system-ui, sans-serif; box-shadow:0 2px 10px rgba(0,0,0,.3); white-space:nowrap; }
        .label .text { overflow:hidden; text-overflow:ellipsis; }
        .dot { flex:none; width:8px; height:8px; border-radius:50%; background:var(--c); animation: dot 1.6s ease-in-out infinite; }
        @keyframes dot { 50% { opacity:.35; } }
        .pointer { position:fixed; left:0; top:0; width:22px; height:22px; }
        .cursor { width:22px; height:22px; filter:drop-shadow(0 1px 2px rgba(0,0,0,.45)); }
        .badge { position:absolute; left:18px; top:16px; display:none; }
        .pointer.idle .cursor { animation: breathe 2.4s ease-in-out infinite; }
        @keyframes breathe { 50% { opacity:.55; } }
        .pointer.thinking .badge.think { display:flex; gap:3px; padding:3px 5px; border-radius:8px; background:rgba(28,25,23,.85); }
        .think i { width:5px; height:5px; border-radius:50%; background:#fff; animation: think 1s ease-in-out infinite; }
        .think i:nth-child(2) { animation-delay:.15s } .think i:nth-child(3) { animation-delay:.3s }
        @keyframes think { 0%,100% { transform:translateY(0); opacity:.4 } 50% { transform:translateY(-3px); opacity:1 } }
        .pointer.reading .badge.read { display:block; width:26px; height:3px; border-radius:2px; background:color-mix(in srgb, var(--c) 35%, transparent); overflow:hidden; }
        .read b { display:block; width:9px; height:3px; background:var(--c); animation: read .9s linear infinite; }
        @keyframes read { from { transform:translateX(-9px) } to { transform:translateX(26px) } }
        .pointer.waiting .badge.wait, .pointer[data-shape=progress] .badge.wait { display:block; width:14px; height:14px; border-radius:50%;
          border:2px solid color-mix(in srgb, var(--c) 30%, transparent); border-top-color:var(--c); animation: spin .8s linear infinite; }
        @keyframes spin { to { transform:rotate(360deg) } }
        .pointer.typing .badge.type { display:block; width:2px; height:14px; background:var(--c); animation: blink .8s steps(1) infinite; }
        @keyframes blink { 50% { opacity:0 } }
        .pointer.working .badge.work { display:block; width:10px; height:10px; border-radius:50%; background:var(--c); animation: dot 1s ease-in-out infinite; }
        .shape { display:none; position:absolute; }
        .cursor .fill { fill:var(--c); } .cursor .line { stroke:var(--c); }
        ${Object.entries(CURSOR_SHAPES).map(([name, { hot }]) => `.s-${name} { left:${-hot[0]}px; top:${-hot[1]}px; }`).join(' ')}
        ${Object.keys(CURSOR_SHAPES).map(name => `.pointer[data-shape=${name}] .s-${name}`).join(', ')} { display:block; }
        .pointer.grab .shape { animation: breathe 2.4s ease-in-out infinite; }
        .pointer.scrolling .s-scroll { animation: scrollnudge .5s ease-in-out infinite; }
        @keyframes scrollnudge { 50% { transform: translateY(var(--scroll-nudge, 2px)); } }
        .badge.mark { left:8px; top:8px; width:12px; height:12px; border-radius:50%; border:1.5px solid #fff;
          box-shadow:0 1px 2px rgba(0,0,0,.45); align-items:center; justify-content:center; color:#fff; font:700 10px/1 system-ui, sans-serif; }
        .pointer[data-shape=help] .badge.help, .pointer[data-shape=copy] .badge.copy, .pointer[data-shape=alias] .badge.alias {
          display:flex; left:12px; top:12px; background:var(--c); }
        .pointer.drop .badge.drop { display:flex; background:var(--c); }
        .pointer.nodrop .badge.nodrop { display:flex; background:#57534e; }
        .ripple { position:fixed; width:28px; height:28px; margin:-14px 0 0 -14px; border-radius:50%;
          border:2px solid var(--c); animation:ripple .45s ease-out forwards; }
        @keyframes ripple { from { transform:scale(.3); opacity:1; } to { transform:scale(1.4); opacity:0; } }
        .field { position:fixed; display:none; border-radius:6px; box-shadow:0 0 0 2px var(--c), 0 0 14px 2px color-mix(in srgb, var(--c) 55%, transparent);
          animation:field-pulse 1.2s ease-in-out infinite; }
        @keyframes field-pulse { 50% { box-shadow:0 0 0 2px var(--c), 0 0 4px 1px color-mix(in srgb, var(--c) 30%, transparent); } }
        @media (prefers-reduced-motion: reduce) { * { animation:none !important; } }
    `);
    root.adoptedStyleSheets = [sheet];
    root.innerHTML = `
      <div class="glow"></div>
      <div class="field"></div>
      <div class="label"><span class="dot"></span><span class="text"></span></div>
      <div class="pointer idle" data-shape="arrow">
        ${Object.entries(CURSOR_SHAPES).map(([name, shape]) => cursorShapeSvg(name, shape)).join('')}
        <span class="badge mark drop">+</span>
        <span class="badge mark nodrop">&#215;</span>
        <span class="badge mark help">?</span>
        <span class="badge mark copy">+</span>
        <span class="badge mark alias">&#8599;</span>
        <span class="badge think"><i></i><i></i><i></i></span>
        <span class="badge read"><b></b></span>
        <span class="badge wait"></span>
        <span class="badge type"></span>
        <span class="badge work"></span>
      </div>`;
    root.querySelector('.label .text').textContent = agentLabel;
    document.documentElement.appendChild(host);
    applyOverlaySettings();
  }
  return host;
}

// The cursor stays where the agent last acted; before any click or typing it
// rests a little right of the middle of the page.
function placeCursor(root) {
  if (!cursorPos) cursorPos = { x: Math.round(innerWidth * 0.62), y: Math.round(innerHeight * 0.42) };
  root.querySelector('.pointer').style.transform = `translate(${cursorPos.x}px, ${cursorPos.y}px)`;
}

// While a button is held the hand stays closed: the state the agent moves on
// to meanwhile (thinking, from the background script, during a real drag)
// waits until it lets go
let dragHeld = false;
let stateAfterDrag = null;
let releaseTimer = null;
let pageShape = 'arrow';
const RELEASE_MS = 700;

function setCursorState(state) {
  if (dragHeld && !(state in HAND_SHAPES)) {
    stateAfterDrag = state;
    return;
  }
  if (state === 'grabbing') dragHeld = true;
  const pointer = overlayRoot().shadowRoot.querySelector('.pointer');
  for (const name of CURSOR_STATES) pointer.classList.toggle(name, name === state);
  clearTimeout(idleTimer);
  clearTimeout(releaseTimer);
  if (state === 'thinking') idleTimer = setTimeout(() => setCursorState('idle'), IDLE_AFTER_MS);
  if (state !== cursorState) reading = null;
  cursorState = state;
  pointer.dataset.shape = STATE_SHAPES[state] || pageShape;
  scheduleIdleMotion();
}

// The shape follows what is under the cursor
function followPage(point) {
  if (!point || !settings.overlayEnabled) return;
  pageShape = shapeAt(point.x, point.y);
  if (!(cursorState in STATE_SHAPES)) overlayRoot().shadowRoot.querySelector('.pointer').dataset.shape = pageShape;
}

// The button goes up: the hand opens, then the cursor takes the page's shape
// and whatever state came in during the drag
function releaseDrag() {
  if (!settings.overlayEnabled) return;
  dragHeld = false;
  setCursorState('grab');
  releaseTimer = setTimeout(() => {
    if (cursorState !== 'grab') return;
    followPage(cursorPos);
    setCursorState(stateAfterDrag || 'idle');
    stateAfterDrag = null;
  }, RELEASE_MS);
}

function setLabel(text) {
  agentLabel = text || OVERLAY_LABEL;
  overlayRoot().shadowRoot.querySelector('.label .text').textContent = agentLabel;
}

// state: what the cursor shows; label: the line under it. Both are optional,
// so a page action can show the overlay without changing what was said.
function showOverlay(state, label) {
  if (!settings.overlayEnabled) return;
  const host = overlayRoot();
  host.style.opacity = '1';
  host.style.visibility = 'visible';
  placeCursor(host.shadowRoot);
  if (state) setCursorState(state);
  if (label) setLabel(label);
  if (settings.tabMarker) startTabMarker();
}

function hideOverlay() {
  const host = document.getElementById(OVERLAY_ID);
  if (host) {
    host.style.opacity = '0';
    host.shadowRoot.querySelector('.field').style.display = 'none';
    host.shadowRoot.querySelector('.label .text').textContent = agentLabel;
  }
  clearTimeout(idleTimer);
  clearTimeout(fieldTimer);
  clearTimeout(idleMotionTimer);
  clearTimeout(releaseTimer);
  dragHeld = false;
  stateAfterDrag = null;
  stopTabMarker();
}

// Ring the field being typed into and name it in the label. Called after each
// piece of text, so the ring follows a field that grows or moves as it fills.
function showTypingField(el) {
  const root = overlayRoot().shadowRoot;
  const rect = el.getBoundingClientRect();
  const field = root.querySelector('.field');
  Object.assign(field.style, {
    display: 'block', left: rect.left + 'px', top: rect.top + 'px',
    width: rect.width + 'px', height: rect.height + 'px',
  });
  const name = (el.getAttribute('aria-label') || el.placeholder || el.labels?.[0]?.innerText ||
    el.name || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  root.querySelector('.label .text').textContent = name ? `Typing into "${name}"` : 'Typing';
  clearTimeout(fieldTimer);
  fieldTimer = setTimeout(() => {
    field.style.display = 'none';
    root.querySelector('.label .text').textContent = agentLabel;
  }, FIELD_LINGER_MS);
}

// The tab's icon in the tab strip gets a pulsing ring while the agent works
// here, so the tab stands out in a row of tabs. Drawn on a canvas from the
// page's own icon (fetched, so a cross-origin icon does not taint the canvas)
// and swapped in as the page's icon a few times a second; the original icon
// links go back on release.
const MARKER_FRAME_MS = 140;
const MARKER_FRAMES = 14;
let markerTimer = null;
let markerLinks = null;
let markerFrame = 0;

async function loadPageIcon() {
  const link = document.querySelector('link[rel~="icon"]:not([data-foxmcp-marker])');
  const url = link ? link.href : new URL('/favicon.ico', location.href).href;
  try {
    const response = await fetch(url, { credentials: 'omit' });
    if (!response.ok) return null;
    return await createImageBitmap(await response.blob());
  } catch (error) {
    return null; // no icon, or one createImageBitmap cannot decode (SVG): the ring is drawn alone
  }
}

async function buildMarkerFrames() {
  const icon = await loadPageIcon();
  const frames = [];
  for (let i = 0; i < MARKER_FRAMES; i++) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 64;
    const ctx = canvas.getContext('2d');
    const pulse = (Math.sin(i / MARKER_FRAMES * Math.PI * 2) + 1) / 2;
    if (icon) {
      ctx.drawImage(icon, 14, 14, 36, 36);
    } else {
      ctx.fillStyle = settings.overlayColor;
      ctx.beginPath(); ctx.arc(32, 32, 12, 0, Math.PI * 2); ctx.fill();
    }
    ctx.lineWidth = 6 + pulse * 3;
    ctx.globalAlpha = 0.55 + pulse * 0.45;
    ctx.strokeStyle = settings.overlayColor;
    ctx.beginPath(); ctx.arc(32, 32, 27, 0, Math.PI * 2); ctx.stroke();
    frames.push(canvas.toDataURL('image/png'));
  }
  return frames;
}

async function startTabMarker() {
  if (markerTimer || !document.head) return;
  markerTimer = -1; // frames being built
  const frames = await buildMarkerFrames();
  if (markerTimer !== -1) return; // released meanwhile
  markerLinks = [...document.querySelectorAll('link[rel~="icon"]')];
  for (const link of markerLinks) link.remove();
  const marker = document.createElement('link');
  marker.rel = 'icon';
  marker.setAttribute('data-foxmcp-marker', '');
  document.head.appendChild(marker);
  markerTimer = setInterval(() => {
    marker.href = frames[markerFrame++ % frames.length];
  }, MARKER_FRAME_MS);
}

function stopTabMarker() {
  if (markerTimer && markerTimer !== -1) clearInterval(markerTimer);
  markerTimer = null;
  document.querySelectorAll('link[data-foxmcp-marker]').forEach(link => link.remove());
  if (markerLinks && document.head) {
    for (const link of markerLinks) document.head.appendChild(link);
  }
  markerLinks = null;
}

// The cursor moves the way a hand on a mouse does, after the motor-control
// research on pointing:
// - Fitts' law: the move takes longer the farther and smaller the target,
//   a + b * log2(distance / width + 1).
// - A fast first submovement with a bell-shaped speed (minimum jerk), skewed so
//   it speeds up quicker than it slows down, along a slight arc; on longer moves
//   it usually stops a little short of the target and now and then past it.
// - One small corrective submovement onto the target, a faint tremor, and a
//   short pause to check the target before a click.
const FITTS_A_MS = 120;
const FITTS_B_MS = 110;
const MIN_MOVE_MS = 100;
const MAX_MOVE_MS = 400;
const CORRECTION_SHARE = 0.22; // of the move's time, spent on the correction
const CHECK_PAUSE_MS = [30, 70];

const minimumJerk = t => t * t * t * (10 - 15 * t + 6 * t * t);
const between = ([low, high]) => low + Math.random() * (high - low);

function humanPath(from, to, targetSize) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const distance = Math.hypot(dx, dy);
  const human = settings.cursorMotion !== 'direct';
  const duration = (CURSOR_SPEEDS[settings.cursorSpeed] || 1) * Math.min(MAX_MOVE_MS, Math.max(MIN_MOVE_MS,
    FITTS_A_MS + FITTS_B_MS * Math.log2(distance / Math.max(targetSize, 8) + 1)));
  // Where the first submovement ends: short of the target 70% of the time.
  // "Direct" motion (a setting) goes straight there in one smooth move.
  const corrects = human && distance > 80;
  const miss = corrects ? (Math.random() < 0.7 ? -1 : 1) * between([0.03, 0.08]) : 0;
  const sway = corrects ? Math.min(6, targetSize / 4) : 0;
  const land = {
    x: to.x + dx * miss + (Math.random() - 0.5) * sway,
    y: to.y + dy * miss + (Math.random() - 0.5) * sway,
  };
  // The arc: a control point off the straight line, to either side
  const bend = human ? (Math.random() < 0.5 ? -1 : 1) * distance * between([0.04, 0.12]) : 0;
  const control = {
    x: (from.x + land.x) / 2 - (dy / distance) * bend,
    y: (from.y + land.y) / 2 + (dx / distance) * bend,
  };
  const points = [];
  const primaryShare = corrects ? 1 - CORRECTION_SHARE : 1;
  const steps = Math.max(12, Math.round(duration * primaryShare / 16));
  for (let i = 0; i <= steps; i++) {
    const s = minimumJerk(Math.pow(i / steps, 0.85));
    const tremor = human && i < steps ? 0.6 : 0;
    points.push({
      x: (1 - s) * (1 - s) * from.x + 2 * (1 - s) * s * control.x + s * s * land.x + (Math.random() - 0.5) * tremor,
      y: (1 - s) * (1 - s) * from.y + 2 * (1 - s) * s * control.y + s * s * land.y + (Math.random() - 0.5) * tremor,
      offset: (i / steps) * primaryShare,
    });
  }
  if (corrects) {
    const fix = Math.max(4, Math.round(duration * CORRECTION_SHARE / 16));
    for (let i = 1; i <= fix; i++) {
      const s = minimumJerk(i / fix);
      points.push({
        x: land.x + (to.x - land.x) * s,
        y: land.y + (to.y - land.y) * s,
        offset: primaryShare + (i / fix) * CORRECTION_SHARE,
      });
    }
  }
  points[points.length - 1] = { ...to, offset: 1 };
  return { points, duration };
}

async function moveCursorTo(el, press) {
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const rect = el.getBoundingClientRect();
  const to = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  return moveCursorToPoint(to, Math.min(rect.width, rect.height), press);
}

// The same move to a point in the viewport, for clicks and drags on a canvas
async function moveCursorToPoint(to, targetSize, press) {
  const from = cursorPos || to;
  cursorPos = to;
  const root = overlayRoot().shadowRoot;
  const pointer = root.querySelector('.pointer');
  pointer.getAnimations().forEach(animation => animation.cancel());
  pointer.style.transition = 'none';
  pointer.style.transform = `translate(${to.x}px, ${to.y}px)`;
  // Nobody sees a hidden tab, and Firefox holds its timers back for seconds. A
  // tab shown only for this action goes back at once, so every moment shows
  if (document.hidden || shownBriefly || !settings.overlayEnabled || !settings.cursorEnabled) return;
  if (Math.hypot(to.x - from.x, to.y - from.y) >= 2) {
    const { points, duration } = humanPath(from, to, targetSize);
    const motion = pointer.animate(
      points.map(({ x, y, offset }) => ({ transform: `translate(${x}px, ${y}px)`, offset })),
      { duration, easing: 'linear' });
    const follow = setInterval(() => {
      const at = new DOMMatrix(getComputedStyle(pointer).transform);
      followPage({ x: at.e, y: at.f });
    }, 60);
    await motion.finished.catch(() => {});
    clearInterval(follow);
  }
  followPage(to);
  if (press) {
    if (settings.cursorMotion !== 'direct') await new Promise(resolve => setTimeout(resolve, between(CHECK_PAUSE_MS)));
    const ripple = document.createElement('div');
    ripple.className = 'ripple';
    ripple.style.left = to.x + 'px';
    ripple.style.top = to.y + 'px';
    root.appendChild(ripple);
    setTimeout(() => ripple.remove(), 500);
  }
}

// Where the viewport sits on screen, for real input posted to the window
// (server/win_input.py): its top left in CSS pixels, the scale to physical
// pixels, and the title Firefox gives the window
function screenInfo() {
  return { x: window.mozInnerScreenX, y: window.mozInnerScreenY, dpr: window.devicePixelRatio, title: document.title,
    hidden: document.hidden };
}

// Between actions the cursor moves the way a hand resting on a mouse does.
// Studies of the pointer during reading and search (Rodden et al., CHI 2008;
// Huang, White and Buscher, CHI 2012) find it parked most of the time. When it
// moves, it follows the eyes: sideways along the line being read, then back to
// the start of the next one, or it hesitates in small moves near where it
// rests. The pauses between moves are long and uneven (log-normal here).
const IDLE_MOTION = {
  idle: { pause: 4000, spread: 0.6, drift: [3, 16], reposition: 0.12 },
  thinking: { pause: 1800, spread: 0.5, drift: [3, 10], reposition: 0.05 },
  waiting: { pause: 2400, spread: 0.5, drift: [3, 12], reposition: 0.05 },
  reading: { pause: 700, spread: 0.4 },
};
const READ_STEP = [22, 70];      // px per eye-following move along a line
const READ_LINE = [180, 420];    // how far along a line before the return sweep
const LINE_HEIGHT = [18, 28];
const EDGE = 24;
let idleMotionTimer = null;
let reading = null;

function gaussian() {
  let u = 0;
  while (!u) u = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * Math.random());
}

function scheduleIdleMotion() {
  clearTimeout(idleMotionTimer);
  const style = IDLE_MOTION[cursorState];
  if (style) idleMotionTimer = setTimeout(idleStep, style.pause * Math.exp(style.spread * gaussian()));
}

const clampToView = point => ({
  x: Math.min(innerWidth - EDGE, Math.max(EDGE, point.x)),
  y: Math.min(innerHeight - EDGE, Math.max(EDGE, point.y)),
});

function nextReadingPoint() {
  if (!reading) reading = { startX: cursorPos.x, y: cursorPos.y, length: between(READ_LINE) };
  if (cursorPos.x - reading.startX >= reading.length || cursorPos.x > innerWidth - EDGE * 3) {
    // Return sweep to the start of the next line, back up the page at the bottom
    reading.y += between(LINE_HEIGHT);
    if (reading.y > innerHeight * 0.85) reading.y = innerHeight * 0.3;
    reading.length = between(READ_LINE);
    return { point: clampToView({ x: reading.startX + (Math.random() - 0.5) * 12, y: reading.y }), ms: between([320, 480]) };
  }
  return { point: clampToView({ x: cursorPos.x + between(READ_STEP), y: reading.y + (Math.random() - 0.5) * 3 }),
    ms: between([150, 260]) };
}

function nextRestingPoint(style) {
  const far = Math.random() < style.reposition;
  const distance = far ? between([40, 120]) : between(style.drift);
  const angle = Math.random() * 2 * Math.PI;
  return { point: clampToView({ x: cursorPos.x + Math.cos(angle) * distance, y: cursorPos.y + Math.sin(angle) * distance }),
    ms: far ? between([380, 650]) : between([220, 420]) };
}

async function idleStep() {
  const style = IDLE_MOTION[cursorState];
  const host = document.getElementById(OVERLAY_ID);
  if (!style || !host || host.style.opacity !== '1') return;
  if (!document.hidden && cursorPos && settings.cursorEnabled && settings.cursorIdleMotion &&
      !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const { point, ms } = cursorState === 'reading' ? nextReadingPoint() : nextRestingPoint(style);
    const from = cursorPos;
    cursorPos = point;
    const pointer = host.shadowRoot.querySelector('.pointer');
    pointer.style.transform = `translate(${point.x}px, ${point.y}px)`;
    const frames = [];
    for (let i = 0; i <= 10; i++) {
      const s = minimumJerk(i / 10);
      frames.push({ transform: `translate(${from.x + (point.x - from.x) * s}px, ${from.y + (point.y - from.y) * s}px)` });
    }
    await pointer.animate(frames, { duration: ms, easing: 'linear' }).finished.catch(() => {});
    followPage(point);
  }
  scheduleIdleMotion();
}

// Listen for messages from background script
browser.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const { action, data } = request;

  console.log('📨 Content script received message:', action, data);

  try {
    switch (action) {
      case 'extractText':
        showOverlay('reading');
        sendResponse({ text: document.body.innerText });
        break;

      case 'extractHTML':
        showOverlay('reading');
        sendResponse({ html: document.documentElement.outerHTML });
        break;

      // What the agent is doing in this tab, sent by the background script
      // before and after each action, and when a page it opened finishes loading.
      case 'agentActive':
        showOverlay(data && data.state, data && data.label);
        sendResponse({ ok: true });
        break;

      // The agent is done with this tab (tabs_release, its server went, or
      // it has been idle for a while).
      case 'agentRelease':
        hideOverlay();
        sendResponse({ ok: true });
        break;

      case 'getPageTitle':
        sendResponse({ title: document.title });
        break;

      case 'getPageURL':
        sendResponse({ url: window.location.href });
        break;

      case 'snapshot':
        showOverlay('reading');
        sendResponse({ snapshot: snapshot(data.maxElements || 300), url: location.href, title: document.title });
        break;

      case 'click': {
        // A click at a point (a canvas) rather than on an element
        if (!hasTarget(data)) {
          const point = targetPoint(data);
          const before = pageState(point.el);
          showOverlay('clicking');
          moveCursorToPoint(point, point.size, true)
            .then(() => {
              const init = mouseInit(point, data.rightClick ? { button: 2, buttons: 2 } : { buttons: 1 });
              for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
                point.el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, init));
              }
              point.el.dispatchEvent(new MouseEvent(data.rightClick ? 'contextmenu' : 'click', init));
              if (data.doubleClick && !data.rightClick) point.el.dispatchEvent(new MouseEvent('dblclick', { ...init, detail: 2 }));
              return clickEffect(before);
            })
            .then(effect => sendResponse({ clicked: `${describe(point.el)} at ${point.x},${point.y}`, effect }))
            .catch(error => sendResponse({ error: error.message }));
          break;
        }
        const el = findTarget(data);
        showOverlay('clicking');
        // A bare "Clicking" from the background gets the element's name
        if (agentLabel === 'Clicking') {
          const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '')
            .replace(/\s+/g, ' ').trim().slice(0, 40);
          if (name) setLabel(`Clicking "${name}"`);
        }
        const before = pageState(el);
        moveCursorTo(el, true)
          .then(() => {
            before.covered = coverOf(el);
            clickElement(el, data.rightClick ? 'right' : data.doubleClick ? 2 : 1);
            return clickEffect(before);
          })
          .then(effect => sendResponse({ clicked: describe(el), effect }))
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'type': {
        const el = findTarget(data);
        showOverlay('typing');
        moveCursorTo(el, true)
          .then(() => typeInto(el, data.text, data.clear !== false))
          .then(async field => {
            const form = field.form;
            const emptied = data.commit ? await commitField(field, data.submit) : undefined;
            let enterMissed = null;
            if (data.submit) {
              // A chip input that replaced the field leaves this one detached
              if (field.isConnected) pressKey(field, 'Enter');
              else enterMissed = submitReplacedField(form);
            }
            sendResponse({ typed: typedField(field), committed: Boolean(data.commit), emptied, enterMissed });
          })
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      // Before real input (server/win_input.py or bidi.py): move the cursor
      // there, say where the page is on screen, and mark the page so the server
      // can find this tab among BiDi's browsing contexts, whose ids are
      // unrelated to the extension's tab ids.
      case 'realInputPrep': {
        shownBriefly = Boolean(data.switched);
        const el = hasTarget(data) ? findTarget(data) : data.purpose === 'type' ? focusedField() : null;
        if (data.purpose === 'click') pendingClicks.set(data.token, pageState(el));
        const watched = { click: el && 'pointer', key: !inFrame(el || document.activeElement) && 'keys',
          type: el && !(el instanceof HTMLSelectElement) && 'keys' }[data.purpose];
        if (watched && !(el && inFrame(el))) watchRealInput(data.token, watched);
        showOverlay(data.purpose === 'type' ? 'typing' : 'clicking');
        if (el && data.purpose === 'click' && agentLabel === 'Clicking') {
          const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '')
            .replace(/\s+/g, ' ').trim().slice(0, 40);
          if (name) setLabel(`Clicking "${name}"`);
        }
        (el ? moveCursorTo(el, data.purpose === 'click') : Promise.resolve())
          .then(() => {
            document.documentElement.setAttribute('data-foxmcp-tab', data.token);
            if (el && data.purpose === 'click') pendingClicks.get(data.token).covered = coverOf(el);
            // The first line of a link that wraps, as for the cover check
            const rect = el && (el.getClientRects()[0] || el.getBoundingClientRect());
            // A key for a field that has the focus needs no click, which would
            // lose the selection and move the caret: Control+a, then Backspace
            const keyOnly = data.purpose === 'key' && el && (el === document.activeElement || el.contains(document.activeElement));
            sendResponse(el ? {
              selector: keyOnly ? undefined : `[${REF_ATTR}="${refFor(el)}"]`, target: describe(el), screen: screenInfo(),
              // Real input would click a <select> open into the browser's own
              // list, where typed keys do not reach the page
              scripted: data.purpose === 'type' && el instanceof HTMLSelectElement,
              point: { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) },
            } : { target: 'the page', screen: screenInfo() });
          })
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'scroll': {
        showOverlay('scrolling');
        const at = scrollPoint(data);
        overlayRoot().shadowRoot.querySelector('.pointer').style.setProperty('--scroll-nudge', (data.deltaY || 0) < 0 ? '-2px' : '2px');
        moveCursorToPoint(at, 40, false)
          .then(async () => {
            const scrolled = scrollAt(data, at.el);
            // Long enough to see, unless nobody can
            if (!document.hidden && settings.overlayEnabled) await new Promise(resolve => setTimeout(resolve, SCROLL_SHOWN_MS));
            sendResponse(scrolled);
          })
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'hover': {
        const point = targetPoint(data);
        showOverlay('clicking');
        moveCursorToPoint(point, point.size, false)
          .then(() => {
            hoverAt(point);
            sendResponse({ hovered: point.el === document.body ? `${point.x},${point.y}` : describe(point.el) });
          })
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'drag': {
        const from = targetPoint(data.from);
        const to = targetPoint(data.to);
        showOverlay('grab');
        moveCursorToPoint(from, from.size, true)
          .then(() => {
            showOverlay('grabbing');
            return Promise.all([dragBetween(from, to, 12, data.button || 0), moveCursorToPoint(to, to.size, false)]);
          })
          .then(() => {
            releaseDrag();
            sendResponse({ dragged: `${describe(from.el)} to ${describe(to.el)}` });
          })
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      // Points for a real hover, point click or drag: the cursor moves to
      // the start, and the server gets viewport coordinates to send to Firefox
      case 'realPointerPrep': {
        shownBriefly = Boolean(data.switched);
        try {
          const from = targetPoint(data.from);
          const to = data.to ? targetPoint(data.to) : null;
          const drag = data.purpose === 'drag';
          if (data.purpose === 'click') {
            pendingClicks.set(data.token, pageState(from.el));
            if (!inFrame(from.el)) watchRealInput(data.token, 'pointer');
          }
          const dragAndDrop = drag && !data.button && usesPageDragAndDrop(from.el);
          showOverlay(drag ? 'grab' : 'clicking');
          moveCursorToPoint(from, from.size, data.purpose === 'click' || drag)
            .then(() => {
              document.documentElement.setAttribute('data-foxmcp-tab', data.token);
              // The cursor follows the real drag on screen, the hand closed
              // until it lets go. A page's drag-and-drop is scripted instead
              // (see content_drag), which draws its own.
              if (to && !dragAndDrop) {
                showOverlay('grabbing');
                moveCursorToPoint(to, to.size, false).then(releaseDrag);
              }
              sendResponse({
                from: { x: Math.round(from.x), y: Math.round(from.y) },
                to: to && { x: Math.round(to.x), y: Math.round(to.y) },
                target: describe(from.el) + (to ? ` to ${describe(to.el)}` : ''),
                screen: screenInfo(),
                // Firefox's remote control starts the page's drag-and-drop but
                // cannot drop, so those drags are scripted (see content_drag)
                pageDragAndDrop: dragAndDrop,
              });
            })
            .catch(error => sendResponse({ error: error.message }));
        } catch (error) {
          sendResponse({ error: error.message });
        }
        break;
      }

      // After a real click the server asks what it changed, against the page
      // as it was when the click was prepared
      case 'clickEffect': {
        const before = pendingClicks.get(data.token);
        pendingClicks.delete(data.token);
        realInputArrived(data.token).then(async arrived => ({
          effect: before ? await clickEffect(before) : '',
          notArrived: arrived === false,
        })).then(sendResponse, error => sendResponse({ error: error.message }));
        break;
      }

      case 'realInputArrived': {
        realInputArrived(data.token).then(arrived => sendResponse({ arrived }));
        break;
      }

      case 'caretToEnd': {
        caretToEnd(findTarget(data));
        sendResponse({ moved: true });
        break;
      }

      case 'checkTypedText': {
        const el = findTarget(data);
        realInputArrived(data.token).then(async arrived => {
          try {
            // Adding text the field already holds passes the check below, so
            // keys that went nowhere have to be caught here
            if (arrived === false && !data.clear) throw new Error('no key presses');
            await checkTypedSettled(el, data.text, data.clear);
          } catch (error) {
            throw arrived === false ? new Error('The page got no real key presses, so the text most likely ' +
              'did not arrive (is the Firefox window behind another one?). Check the page before typing again: ' +
              'a field that hands focus to an editor in a frame gets keys this page cannot see') : error;
          }
          showTypingField(el);
          const form = el.form;
          const emptied = data.commit ? await commitField(el, data.submit) : undefined;
          // A chip input that replaced the field leaves nothing for a real
          // Enter to reach, so the form is submitted from here instead
          const submitted = Boolean(data.submit && !el.isConnected);
          const enterMissed = submitted ? submitReplacedField(form) : null;
          sendResponse({ typed: typedField(el), committed: Boolean(data.commit), emptied, submitted, enterMissed });
        }).catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'pressKey': {
        const el = hasTarget(data) ? findTarget(data) : (document.activeElement || document.body);
        showOverlay('typing');
        try {
          pressKey(el, data.key);
          sendResponse({ pressed: data.key });
        } catch (error) {
          sendResponse({ error: error.message });
        }
        break;
      }

      case 'captureFileInput': {
        const trigger = findTarget({ ref: data.triggerRef, selector: data.triggerSelector });
        showOverlay('clicking');
        const captured = captureNextFileInput(data.timeoutMs || 3000);
        moveCursorTo(trigger, true)
          .then(() => clickElement(trigger))
          .then(() => captured)
          .then(input => sendResponse({ ref: refFor(input), multiple: input.multiple, directory: input.webkitdirectory }))
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      case 'setFiles': {
        const el = findTarget(data);
        showOverlay('waiting');
        setInputFiles(el, data.files, data.relativePaths || [])
          .then(paths => sendResponse({ count: paths.length, paths: paths.slice(0, 5), target: describe(el) }))
          .catch(error => sendResponse({ error: error.message }));
        break;
      }

      // The bytes of an image on the page, for "Copy Image". Read with the
      // page's own fetch so a same-origin image comes with the page's cookies
      // and a blob: URL resolves; a cross-origin one the page may not read is
      // left to the background script, by its URL.
      case 'imageSource': {
        const el = findTarget(data);
        const img = el.tagName === 'IMG' ? el : el.querySelector('img');
        if (!img) {
          sendResponse({ error: `${describe(el)} is not an image and holds none` });
          break;
        }
        showOverlay('reading');
        moveCursorTo(img, false)
          .then(() => window.wrappedJSObject.fetch(img.currentSrc || img.src))
          .then(response => response.ok ? response.arrayBuffer() : null)
          // copied out of the page's realm, so the message can carry it
          .then(buffer => buffer && new Uint8Array(buffer).slice().buffer)
          .catch(() => null)
          .then(bytes => sendResponse({ src: img.currentSrc || img.src, bytes, target: describe(img) }));
        break;
      }

      // A real paste of the clipboard into the element given, or into the
      // page. The page gets a trusted paste event carrying what is on the
      // clipboard, as from Ctrl+V; a synthetic event with the same data is
      // ignored by canvas apps such as Miro.
      //
      // Firefox runs a content script's paste only into a <textarea>. For any
      // other target, or none, a textarea is added off screen for the moment
      // of the paste; the paste event bubbles from it to the document, where
      // canvas apps listen.
      case 'paste': {
        const el = hasTarget(data) ? findTarget(data) : null;
        const previous = document.activeElement;
        showOverlay('typing');
        (el ? moveCursorTo(el, true) : Promise.resolve()).then(() => {
          let into = el;
          let scratch = null;
          if (!(el instanceof HTMLTextAreaElement)) {
            scratch = document.createElement('textarea');
            scratch.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;';
            document.body.appendChild(scratch);
            into = scratch;
          }
          into.focus();
          const pasted = document.execCommand('paste');
          if (scratch) {
            scratch.remove();
            previous?.focus?.();
          }
          sendResponse(pasted ? { pasted: scratch ? 'the page' : describe(el) } : { error: 'Firefox refused the paste' });
        });
        break;
      }

      // Hidden while a screenshot is taken, so the capture shows the page as
      // it is rather than with the agent's border and cursor on top.
      case 'overlayVisibility': {
        const host = document.getElementById(OVERLAY_ID);
        if (host) host.style.visibility = data.visible ? 'visible' : 'hidden';
        sendResponse({ ok: true });
        break;
      }

      default:
        sendResponse({ error: `Unknown content action: ${action}` });
    }
  } catch (error) {
    sendResponse({ error: error.message });
  }

  return true; // Keep message channel open for async response
});
// A page the agent opened or navigated to shows the overlay as soon as this
// script runs, instead of waiting for the tab's load to complete.
browser.runtime.sendMessage({ action: 'isAgentTab' })
  .then(reply => { if (reply && reply.active) showOverlay(reply.state, reply.label); })
  .catch(error => console.warn('Could not ask whether an agent is working in this tab:', error.message));
