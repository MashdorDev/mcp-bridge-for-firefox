#!/usr/bin/env python3
"""
Real (trusted) mouse and keyboard input on Windows, without remote control

Firefox turns the window messages Windows delivers to it (WM_LBUTTONDOWN,
WM_CHAR and the rest) into trusted DOM events, whoever posted them. Posting
them to the tab's window gives the same real input as WebDriver BiDi, with two
differences that matter: Firefox does not have to run with
--remote-debugging-port, which sets navigator.webdriver for every page, and
the person's own mouse pointer never moves, so they can keep working while
the agent clicks. The window does not need to be in front either; only the
tab has to be the one showing in it, which the extension sees to.

Keys reach only a window Firefox believes is active, and that belief comes
from the WM_ACTIVATE it receives, not from Windows. Posting one gives the page
focus while the person's own window stays in front and keeps their keyboard.

Modifier keys are the one catch: Firefox reads them from the keyboard state of
its input thread, not from the messages. So for a combination such as Alt+2
this thread joins Firefox's input queue for a moment and sets that state.

Licensed under the MIT License - see LICENSE file for details
"""

import asyncio
import atexit
import os
import re
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

AVAILABLE = sys.platform == 'win32'

if AVAILABLE:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32')
    # Screen positions in physical pixels, as the content script reports them
    # (CSS pixels times devicePixelRatio), on every monitor
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per monitor v2
    except (AttributeError, OSError):
        pass
    user32.VkKeyScanW.restype = ctypes.c_short
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

    kernel32.GetTickCount.restype = wintypes.DWORD

WM_ACTIVATE, WA_INACTIVE, WA_ACTIVE = 0x006, 0, 1
SW_SHOWNOACTIVATE, HWND_BOTTOM = 4, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x207, 0x208
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x102, 0x104, 0x105
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x1, 0x2, 0x10

VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B
MODIFIERS = {'Shift': VK_SHIFT, 'Control': VK_CONTROL, 'Ctrl': VK_CONTROL, 'Alt': VK_MENU,
             'Meta': VK_LWIN, 'Command': VK_LWIN}
# Left and right variants Firefox may check alongside the generic key
SIDES = {VK_SHIFT: (0xA0, 0xA1), VK_CONTROL: (0xA2, 0xA3), VK_MENU: (0xA4, 0xA5), VK_LWIN: (0x5C,)}

# KeyboardEvent.key names to virtual-key codes
SPECIAL_KEYS = {
    'Enter': 0x0D, 'Return': 0x0D, 'Tab': 0x09, 'Escape': 0x1B, 'Esc': 0x1B,
    'Backspace': 0x08, 'Delete': 0x2E, 'Space': 0x20, ' ': 0x20,
    'ArrowLeft': 0x25, 'ArrowUp': 0x26, 'ArrowRight': 0x27, 'ArrowDown': 0x28,
    'Home': 0x24, 'End': 0x23, 'PageUp': 0x21, 'PageDown': 0x22, 'Insert': 0x2D,
    **{f'F{n}': 0x70 + n - 1 for n in range(1, 13)},
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, VK_LWIN}

# Pauses that let Firefox take one step before the next: between the parts of
# a click, after a key with modifiers (whose state must hold until Firefox has
# read it), and every so many characters of typed text
STEP_S = 0.03
MODIFIER_HOLD_S = 0.15
CHARS_PER_BATCH = 40
# In a window the person may be looking at: whoever switches its tab mid-text
# catches what was already posted, so no more than this can reach their tab
CHARS_PER_BATCH_THEIRS = 10
# A drag, as in bidi.py: the move, its steps, and the hold at each end
DRAG_S = 0.6
DRAG_STEPS = 20
DRAG_HOLD_S = 0.12
HOVER_STEPS = 6


class WindowInputUnavailable(Exception):
    """Not on Windows, or the tab's Firefox window was not found"""


class WindowNotShowing(WindowInputUnavailable):
    """The tab is not in its window yet: Firefox updates the title and the
    tab's visibility a moment after a tab is activated, so this can pass"""


def clipboard_sequence() -> int:
    """Windows' count of clipboard changes: it goes up whenever anything is
    copied, by anyone, and says nothing about what was copied"""
    return user32.GetClipboardSequenceNumber()


def firefox_windows(minimised: bool = False) -> List[int]:
    """Visible Firefox windows, front to back, by default without minimised ones"""
    found = []

    @EnumWindowsProc
    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if name.value == 'MozillaWindowClass' and user32.IsWindowVisible(hwnd) and (minimised or not user32.IsIconic(hwnd)):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def window_title(hwnd: int) -> str:
    title = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, title, 1024)
    return title.value


def window_contains(hwnd: int, x: int, y: int) -> bool:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def mouse_lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def key_lparam(vk: int, up: bool = False, alt: bool = False) -> int:
    lparam = 1 | (user32.MapVirtualKeyW(vk, 0) << 16)
    if vk in EXTENDED_KEYS:
        lparam |= 1 << 24
    if alt:
        lparam |= 1 << 29
    if up:
        lparam |= (1 << 30) | (1 << 31)
    return lparam


# How long Firefox takes to act on a posted WM_ACTIVATE before keys can follow it
ACTIVATE_SETTLE_S = 0.1
# How long a background window stays active after the last action. Pages close
# open menus when the window blurs, so stepping down after each action would
# undo what the agent just opened before its next call.
ACTIVE_FOR_S = 20.0
# Swapping activation with the person's window of the same Firefox: how long they
# must have been idle, how long to wait for that, and how long Firefox takes
IDLE_BEFORE_SWAP_S = 2.0
IDLE_WAIT_MAX_S = 10.0
IDLE_POLL_S = 0.25
SWAP_SETTLE_S = 0.05
SWAP_SETTLE_STEPS = 5
AGENT_WINDOW_PREFACE = '[Agent] '
# One action at a time across every server on this computer: how long to wait
MACHINE_TURN_WAIT_S = 30.0
MACHINE_TURN_POLL_S = 0.05
# Background windows made active, and the timer that steps each one down
activated: Dict[int, asyncio.TimerHandle] = {}
# Windows holding activation borrowed from the person's, and that window
on_loan: Dict[int, int] = {}


def seconds_idle() -> float:
    """Since the person last touched the keyboard or mouse. Posted messages don't count."""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) / 1000.0


def same_firefox(one: int, other: int) -> bool:
    """Firefox keeps one active window per process, so activating any window of
    the one the person is in would lower the window they are using"""
    pids = [wintypes.DWORD(), wintypes.DWORD()]
    user32.GetWindowThreadProcessId(one, ctypes.byref(pids[0]))
    user32.GetWindowThreadProcessId(other, ctypes.byref(pids[1]))
    # No foreground window at all, for a moment while it changes, is nobody's
    return pids[0].value == pids[1].value != 0


async def wait_for_idle(waited: float = 0.0) -> float:
    """Until the person pauses, from a budget per action: someone tapping a key
    every few seconds must not keep an action going for minutes. Says how much
    of it has gone."""
    while seconds_idle() < IDLE_BEFORE_SWAP_S:
        if waited >= IDLE_WAIT_MAX_S:
            raise WindowInputUnavailable('the person is using this Firefox, where keys for another window or tab '
                                         f'would reach them, and did not pause for {IDLE_WAIT_MAX_S:.0f} s')
        await asyncio.sleep(IDLE_POLL_S)
        waited += IDLE_POLL_S
    return waited


async def wait_to_change_tabs():
    """Showing a tab takes away the one the person may be typing in"""
    if user32.GetForegroundWindow() in firefox_windows():
        await wait_for_idle()


@asynccontextmanager
async def machine_turn():
    """Activation belongs to a Firefox, and one add-on serves several servers: one
    giving a window back mid-way through another's text would send the rest to
    the person. The operating system drops the lock if its holder dies."""
    if not AVAILABLE:
        yield
        return
    with open(os.path.join(tempfile.gettempdir(), 'mcp-bridge-real-input.lock'), 'a+b') as lock:
        deadline = time.monotonic() + MACHINE_TURN_WAIT_S
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WindowInputUnavailable(f'another agent has been using real input for {MACHINE_TURN_WAIT_S:.0f} s') from None
                await asyncio.sleep(MACHINE_TURN_POLL_S)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def hand_back(hwnd: int, theirs: int):
    foreground = user32.GetForegroundWindow()
    user32.PostMessageW(hwnd, WM_ACTIVATE, WA_INACTIVE, 0)
    # The borrowing window holds the foreground because Firefox raised it. Anywhere
    # else, Windows has told Firefox already, and theirs must not read as active
    if foreground in (hwnd, theirs):
        user32.PostMessageW(theirs, WM_ACTIVATE, WA_ACTIVE, 0)


def step_down(hwnd: int):
    activated.pop(hwnd, None)
    # Once the person has come to this Firefox, its active state is theirs
    if not same_firefox(user32.GetForegroundWindow(), hwnd):
        user32.PostMessageW(hwnd, WM_ACTIVATE, WA_INACTIVE, 0)


def step_all_down():
    """A window left active reports focus to its pages until someone uses it"""
    for hwnd, timer in list(activated.items()):
        timer.cancel()
        step_down(hwnd)
    for hwnd, theirs in list(on_loan.items()):
        del on_loan[hwnd]
        hand_back(hwnd, theirs)


if AVAILABLE:
    atexit.register(step_all_down)


def shows_page(window: str, page: str) -> bool:
    """Whether a window's title is this page's title and nothing else but the
    browser's name, after the profile's if it has one ("Page — Work — Mozilla
    Firefox"): a tab called "Inbox - Draft" must not pass for "Inbox" """
    if not page or not window.startswith(page):
        return False
    rest = window[len(page):]
    return not rest or re.fullmatch(r' (— [^—]+( — [^—]+)?|[–-] [^—–-]*)', rest) is not None


def hidden_window(title: str, x: int, y: int) -> int:
    """The window of a tab that reads as hidden. A covered one is still where it
    was, so position and title both have to agree, as for a visible one. A
    minimised one has no position, so only a minimised window can qualify, never
    one the person may be using, and the title has to pick out exactly one."""
    covered = [hwnd for hwnd in firefox_windows() if window_contains(hwnd, x, y) and shows_page(window_title(hwnd), title)]
    if covered:
        return covered[0]
    minimised = [hwnd for hwnd in firefox_windows(minimised=True)
                 if user32.IsIconic(hwnd) and shows_page(window_title(hwnd), title)]
    if len(minimised) != 1:
        raise WindowNotShowing("the tab's Firefox window could not be told apart from the others")
    return minimised[0]


class WindowInput:
    """Input for one tab, through its Firefox window: `async with WindowInput(prep) as win:`

    prep is what the content script answered: where the page's viewport sits
    on screen ("screen": x, y in CSS pixels, dpr, title) and, for an element,
    its middle in the viewport ("point"). The methods match BidiSession's, so
    the tools hand either one the same steps.
    """

    def __init__(self, prep: Dict[str, Any], still_showing=None):
        if not AVAILABLE:
            raise WindowInputUnavailable('Window input works on Windows only')
        screen = prep.get('screen')
        if not screen:
            raise WindowInputUnavailable('The page did not say where it is on screen')
        # A covered or minimised window reads as hidden and still takes input
        self.hidden = bool(screen.get('hidden'))
        self.origin = (screen['x'], screen['y'])
        self.dpr = screen['dpr'] or 1
        self.title = screen.get('title') or ''
        # The add-on prefixes the agent window's title, so a page's title on its
        # own can't match this to a window of the person's, or the reverse
        self.agent_window = bool(prep.get('agentWindow'))
        if self.agent_window and self.title:
            self.title = AGENT_WINDOW_PREFACE + self.title
        self.point = prep.get('point')
        self.hwnd = 0
        self.thread = 0
        self.buttons = 0
        self.attached = False
        self.made_active = False
        self.lent_by = 0
        self.lent_at = 0.0
        self.idle_waited = 0.0
        self.input_sent = False
        # Asks the add-on whether the tab is still the one in this window
        self.still_showing = still_showing

    async def __aenter__(self):
        x, y = self.screen_point(0, 0)
        if self.hidden:
            # The tab may not have come up yet, and hidden no longer says so
            await asyncio.sleep(ACTIVATE_SETTLE_S)
            self.hwnd = hidden_window(self.title, x, y)
            self.thread = user32.GetWindowThreadProcessId(self.hwnd, None)
            return self
        windows = [hwnd for hwnd in firefox_windows() if window_contains(hwnd, x, y)]
        # Firefox names its window after the page showing in it, which picks
        # the right one when two Firefox windows overlap there
        # Only a window showing this page's title: another Firefox window at
        # the same spot (another profile, the person's own) must never get
        # the agent's clicks and keys
        if self.title:
            windows = [hwnd for hwnd in windows if shows_page(window_title(hwnd), self.title)]
        else:
            # A page with no title yet says nothing, so it takes the spot being
            # beyond doubt
            if self.agent_window:
                windows = [hwnd for hwnd in windows if window_title(hwnd).startswith(AGENT_WINDOW_PREFACE)]
            if len(windows) > 1:
                raise WindowNotShowing('the page has no title to tell its Firefox window from the others there')
        hwnd = (windows or [0])[0]
        if not hwnd:
            raise WindowNotShowing("the tab's Firefox window was not found (minimised?)")
        self.hwnd = hwnd
        self.thread = user32.GetWindowThreadProcessId(hwnd, None)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.release()
        if self.lent_by:
            self.give_back()
        elif self.made_active:
            activated[self.hwnd] = asyncio.get_running_loop().call_later(ACTIVE_FOR_S, step_down, self.hwnd)
        # Falling back from here would do it all again on top of what went in
        if isinstance(exc, WindowInputUnavailable) and self.input_sent:
            raise RuntimeError(f'Stopped after part of the input went in, because {exc}. '
                               'Check the page before doing it again') from exc

    async def make_active(self):
        """Let this window take keys, checked again before every key and batch of
        text: a key posted here lands in whichever window of this Firefox is
        active, and the person can change window or tab at any moment. Posted
        once per action rather than remembered: leaving Firefox after using it,
        or a detach from its input queue, makes it inactive again with nothing
        here to see that."""
        await self.check_showing()
        foreground = user32.GetForegroundWindow()
        if self.lent_by and foreground in (self.hwnd, self.lent_by):
            if seconds_idle() > time.monotonic() - self.lent_at:
                self.stay_under(self.lent_by)
                return
            foreground = self.lent_by
        if same_firefox(foreground, self.hwnd) and foreground != self.hwnd:
            # They came back, or moved to another window: theirs until they pause
            if self.lent_by:
                self.give_back()
            self.idle_waited = await wait_for_idle(self.idle_waited)
            await self.check_showing()
            foreground = user32.GetForegroundWindow()
        if foreground == self.hwnd:
            self.end_loan()
        elif same_firefox(foreground, self.hwnd):
            await self.borrow_activation(foreground)
        else:
            # Another program is in front, so what was borrowed needs no returning
            self.end_loan()
            if not self.made_active:
                self.made_active = True
                pending = activated.pop(self.hwnd, None)
                if pending:
                    pending.cancel()
                self.post(WM_ACTIVATE, WA_ACTIVE, 0)
                await asyncio.sleep(ACTIVATE_SETTLE_S)

    async def borrow_activation(self, theirs: int):
        """The person is in another window of this Firefox, which has one active
        window per process, and keys posted anywhere else land in theirs. So this
        window borrows it for the action, only while they aren't using the
        computer: a key they press during the swap would land here instead. Firefox
        also raises a window it activates while it holds the foreground, and so
        this one holds the foreground for the action."""
        self.lent_by = theirs
        on_loan[self.hwnd] = theirs
        self.lent_at = time.monotonic()
        self.made_active = True
        user32.PostMessageW(theirs, WM_ACTIVATE, WA_INACTIVE, 0)
        self.post(WM_ACTIVATE, WA_ACTIVE, 0)
        # In steps, as Firefox raises it within this time and it should not show
        for _ in range(SWAP_SETTLE_STEPS):
            await asyncio.sleep(SWAP_SETTLE_S / SWAP_SETTLE_STEPS)
            self.stay_under(theirs)

    def stay_under(self, theirs: int):
        """What the person sees stays their window, whichever one Firefox raised"""
        user32.SetWindowPos(self.hwnd, theirs, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def end_loan(self):
        on_loan.pop(self.hwnd, None)
        self.lent_by = 0

    def give_back(self):
        hand_back(self.hwnd, self.lent_by)
        self.end_loan()

    async def check_showing(self):
        if self.still_showing and not await self.still_showing():
            raise WindowInputUnavailable('its tab stopped showing in the window, or the agent was paused')

    async def wait_if_person_shares_firefox(self):
        """Held keys and buttons apply to all of this Firefox's input, the
        person's included: their "w" under a held Control closes a tab"""
        if same_firefox(user32.GetForegroundWindow(), self.hwnd):
            self.idle_waited = await wait_for_idle(self.idle_waited)

    # Positions

    def screen_point(self, x: float, y: float):
        return round((self.origin[0] + x) * self.dpr), round((self.origin[1] + y) * self.dpr)

    def client_point(self, x: float, y: float):
        # Read now rather than on entry: the person can minimise it mid-drag
        if user32.IsIconic(self.hwnd):
            if self.agent_window:
                # The agent's own window: back behind everything, never activated,
                # then the page is asked again where it is now
                user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                raise WindowNotShowing('the agent window was just restored')
            # Windows moves it off screen, and the restored position is no help
            # for a maximised one: a click aimed from it could hit the wrong thing
            raise WindowInputUnavailable('its window is minimised, where a click cannot be aimed (keys still work)')
        sx, sy = self.screen_point(x, y)
        point = wintypes.POINT(sx, sy)
        user32.ScreenToClient(self.hwnd, ctypes.byref(point))
        # A position from before the window was restored wraps round in the
        # message's 16 bits to some other place inside it
        area = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(area))
        if not (0 <= point.x < area.right and 0 <= point.y < area.bottom):
            raise WindowInputUnavailable('the page is no longer where it said it was')
        return point.x, point.y

    def post(self, message: int, wparam: int, lparam: int):
        if not user32.PostMessageW(self.hwnd, message, wparam, lparam):
            raise RuntimeError(f'Firefox did not take the input (error {ctypes.get_last_error()})')
        if message not in (WM_ACTIVATE, WM_MOUSEMOVE):
            self.input_sent = True

    def hold(self, keys: List[int]):
        """Make Firefox's input thread see exactly these keys and buttons held,
        until release(). This thread stays joined to Firefox's input queue
        meanwhile: the state set while joined does not outlast the join."""
        if not self.attached:
            if not user32.AttachThreadInput(kernel32.GetCurrentThreadId(), self.thread, True):
                raise RuntimeError('Could not reach Firefox to hold the modifier keys')
            self.attached = True
        state = (ctypes.c_ubyte * 256)()
        user32.GetKeyboardState(state)
        for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, *MODIFIERS.values(), *(v for vs in SIDES.values() for v in vs)):
            state[vk] = 0
        for vk in keys:
            state[vk] = 0x80
            for side in SIDES.get(vk, ())[:1]:
                state[side] = 0x80
        user32.SetKeyboardState(state)

    def release(self):
        if self.attached:
            self.hold([])
            user32.AttachThreadInput(kernel32.GetCurrentThreadId(), self.thread, False)
            self.attached = False

    # The BidiSession interface

    async def find_tab(self, token: str):
        return self.hwnd

    async def element(self, context, selector: str):
        if not self.point:
            raise RuntimeError(f'No element matches {selector}')
        return self.point

    async def move(self, x: float, y: float):
        cx, cy = self.client_point(x, y)
        self.post(WM_MOUSEMOVE, self.buttons, mouse_lparam(cx, cy))
        await asyncio.sleep(STEP_S / 3)

    async def click_at(self, context, x: float, y: float, button: int = 0, clicks: int = 1):
        await self.check_showing()
        await self.move(x, y)
        cx, cy = self.client_point(x, y)
        down, up, flag, vk = ((WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON, VK_RBUTTON) if button == 2
                              else (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON, VK_LBUTTON))
        for n in range(clicks):
            self.post(WM_LBUTTONDBLCLK if n and button != 2 else down, flag, mouse_lparam(cx, cy))
            await asyncio.sleep(STEP_S)
            self.post(up, 0, mouse_lparam(cx, cy))
            await asyncio.sleep(STEP_S)

    async def click(self, context, element: Dict[str, float], button: int = 0, clicks: int = 1):
        await self.click_at(context, element['x'], element['y'], button, clicks)

    async def hover(self, context, x: float, y: float):
        for i in range(1, HOVER_STEPS + 1):
            await self.move(x - (HOVER_STEPS - i) * 2, y)

    async def drag(self, context, start: Dict[str, float], end: Dict[str, float], button: int = 0):
        # Canvas apps pan the view with the middle or right button held
        down, up, flag, vk = {1: (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON, VK_MBUTTON),
                              2: (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON, VK_RBUTTON)}.get(
                                  button, (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON, VK_LBUTTON))
        await self.wait_if_person_shares_firefox()
        await self.check_showing()
        await self.move(start['x'], start['y'])
        self.buttons = flag
        self.hold([vk])
        cx, cy = self.client_point(start['x'], start['y'])
        self.post(down, flag, mouse_lparam(cx, cy))
        await asyncio.sleep(DRAG_HOLD_S)
        for i in range(1, DRAG_STEPS + 1):
            t = i / DRAG_STEPS
            eased = t * t * t * (10 - 15 * t + 6 * t * t)  # minimum jerk
            await self.move(start['x'] + (end['x'] - start['x']) * eased,
                            start['y'] + (end['y'] - start['y']) * eased)
            await asyncio.sleep(DRAG_S / DRAG_STEPS)
        await asyncio.sleep(DRAG_HOLD_S)
        cx, cy = self.client_point(end['x'], end['y'])
        self.buttons = 0
        # Firefox reads the held buttons when the release arrives: with the
        # button still down it fires mouseup but never pointerup
        self.hold([])
        self.post(up, 0, mouse_lparam(cx, cy))
        await asyncio.sleep(STEP_S)
        self.release()

    async def press(self, context, combo: str):
        """A key, or a combination such as "Control+a" or "Shift+Tab" """
        names = [part for part in combo.split('+') if part] if len(combo) > 1 else [combo]
        *modifier_names, key = names
        modifiers = []
        for name in modifier_names:
            if name not in MODIFIERS:
                raise ValueError(f'Unknown key "{name}"')
            modifiers.append(MODIFIERS[name])
        if key in MODIFIERS:
            modifiers.append(MODIFIERS[key])
            vk = None
        elif key in SPECIAL_KEYS:
            vk = SPECIAL_KEYS[key]
        elif len(key) == 1 and not {VK_CONTROL, VK_MENU, VK_LWIN} & set(modifiers):
            # A character on its own types it, whatever the keyboard layout
            await self.type_text(context, key)
            return
        elif len(key) == 1 and key.isascii() and key.isalnum():
            # Fixed codes for shortcuts: asking the keyboard layout would fail
            # for "a" when it is a Hebrew or Russian one
            vk = ord(key.upper())
        elif len(key) == 1:
            scan = user32.VkKeyScanW(ord(key))
            if scan == -1 or (scan & 0xFF) == 0xFF:
                raise ValueError(f'No key types "{key}" on this keyboard layout')
            vk = scan & 0xFF
        else:
            raise ValueError(f'Unknown key "{key}"')
        alt = VK_MENU in modifiers
        if modifiers:
            await self.wait_if_person_shares_firefox()
        await self.make_active()
        if modifiers:
            self.hold(modifiers)
        try:
            # Only key downs and ups: Windows makes the WM_CHAR for Enter, Tab
            # and the rest (TranslateMessage) and queues it behind whatever is
            # waiting. Firefox looks for it right after the key down, so the
            # key up waits until it is there; queued behind the key up, it would
            # type a second time (two line breaks for one Enter).
            for mod in modifiers:
                self.post(WM_SYSKEYDOWN if alt else WM_KEYDOWN, mod, key_lparam(mod, alt=alt))
            if vk is not None:
                self.post(WM_SYSKEYDOWN if alt else WM_KEYDOWN, vk, key_lparam(vk, alt=alt))
                await asyncio.sleep(STEP_S)
                self.post(WM_SYSKEYUP if alt else WM_KEYUP, vk, key_lparam(vk, up=True, alt=alt))
            for mod in reversed(modifiers):
                self.post(WM_SYSKEYUP if alt and mod != VK_MENU else WM_KEYUP, mod, key_lparam(mod, up=True))
            await asyncio.sleep(MODIFIER_HOLD_S if modifiers else STEP_S)
        finally:
            self.release()

    def check_text(self, text: str):
        # Firefox drops characters outside the Basic Multilingual Plane (emoji)
        # that arrive as window messages, however they are sent, so such text
        # goes through another way before any of it is typed
        if any(ord(char) > 0xFFFF for char in text):
            raise WindowInputUnavailable('Emoji cannot be typed through window messages')

    async def type_text(self, context, text: str):
        self.check_text(text)
        await self.make_active()
        # A line break goes in as Shift+Enter, as in bidi.py: a plain Enter
        # sends the message in chat boxes
        batch = CHARS_PER_BATCH if self.agent_window else CHARS_PER_BATCH_THEIRS
        sent = 0
        for char in text:
            if char == '\n':
                await self.press(context, 'Shift+Enter')
                continue
            self.post(WM_CHAR, ord(char), 1)
            sent += 1
            if sent % batch == 0:
                await self.make_active()
                await asyncio.sleep(STEP_S)
        await asyncio.sleep(STEP_S)

