#!/usr/bin/env python3
"""
What server/win_input.py posts to Firefox's window, checked without a browser

Licensed under the MIT License - see LICENSE file for details
"""

import pytest

import test_imports  # Automatic path setup
from server import win_input
from server.win_input import (WindowInput, WindowInputUnavailable, WindowNotShowing, WM_CHAR, WM_KEYDOWN, WM_KEYUP,
                              WM_SYSKEYDOWN, WM_SYSKEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP,
                              WM_LBUTTONDBLCLK, WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON,
                              WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON, VK_MBUTTON, VK_RBUTTON,
                              VK_CONTROL, VK_MENU, VK_SHIFT)

pytestmark = pytest.mark.skipif(not win_input.AVAILABLE, reason='Window input is Windows only')

PREP = {'screen': {'x': 100, 'y': 50, 'dpr': 2, 'title': 'Page'}, 'point': {'x': 10, 'y': 20}}


class RecordingWindow(WindowInput):
    """Records what it would post and which keys it would hold"""

    def __init__(self):
        super().__init__(PREP)
        self.posted = []
        self.held = []

    def post(self, message, wparam, lparam):
        self.posted.append((message, wparam))

    async def make_active(self):
        pass

    def hold(self, keys):
        self.held.append(list(keys))

    def release(self):
        self.held.append([])

    def client_point(self, x, y):
        return round(x), round(y)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_):
        pass
    monkeypatch.setattr(win_input.asyncio, 'sleep', instant)


@pytest.mark.asyncio
async def test_a_shortcut_holds_its_modifier_and_types_nothing():
    window = RecordingWindow()
    await window.press(None, 'Control+a')
    assert window.posted == [(WM_KEYDOWN, VK_CONTROL), (WM_KEYDOWN, ord('A')),
                             (WM_KEYUP, ord('A')), (WM_KEYUP, VK_CONTROL)]
    assert window.held == [[VK_CONTROL], []]


@pytest.mark.asyncio
async def test_alt_combinations_are_system_keys():
    window = RecordingWindow()
    await window.press(None, 'Alt+2')
    assert window.posted == [(WM_SYSKEYDOWN, VK_MENU), (WM_SYSKEYDOWN, ord('2')),
                             (WM_SYSKEYUP, ord('2')), (WM_KEYUP, VK_MENU)]


@pytest.mark.asyncio
async def test_enter_is_only_a_key_down_and_up():
    # Windows makes the character itself; posting one too typed Enter twice
    window = RecordingWindow()
    await window.press(None, 'Enter')
    assert window.posted == [(WM_KEYDOWN, 0x0D), (WM_KEYUP, 0x0D)]


@pytest.mark.asyncio
async def test_a_single_character_is_typed_whatever_the_layout():
    window = RecordingWindow()
    await window.press(None, 'ש')
    assert window.posted == [(WM_CHAR, ord('ש'))]


@pytest.mark.asyncio
async def test_text_goes_in_as_characters_and_line_breaks_as_shift_enter():
    window = RecordingWindow()
    await window.type_text(None, 'hi\nשלום')
    assert window.posted == [(WM_CHAR, ord('h')), (WM_CHAR, ord('i')),
                             (WM_KEYDOWN, VK_SHIFT), (WM_KEYDOWN, 0x0D), (WM_KEYUP, 0x0D), (WM_KEYUP, VK_SHIFT),
                             *[(WM_CHAR, ord(c)) for c in 'שלום']]


@pytest.mark.asyncio
async def test_emoji_hand_the_text_to_another_way_before_any_of_it_is_typed():
    window = RecordingWindow()
    with pytest.raises(WindowInputUnavailable):
        await window.type_text(None, 'ok 🙂')
    assert window.posted == []


@pytest.mark.asyncio
async def test_a_double_click_sends_the_second_press_as_a_double_click():
    window = RecordingWindow()
    await window.click(None, window.point, clicks=2)
    presses = [message for message, _ in window.posted if message != win_input.WM_MOUSEMOVE]
    assert presses == [WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_LBUTTONUP]


@pytest.mark.asyncio
async def test_a_middle_button_drag_holds_the_middle_button_the_whole_way():
    # Canvas apps such as Miro pan the view with it
    window = RecordingWindow()
    await window.drag(None, {'x': 10, 'y': 20}, {'x': 110, 'y': 70}, button=1)
    presses = [(m, w) for m, w in window.posted if m != win_input.WM_MOUSEMOVE]
    assert presses == [(WM_MBUTTONDOWN, MK_MBUTTON), (WM_MBUTTONUP, 0)]
    down = window.posted.index((WM_MBUTTONDOWN, MK_MBUTTON))
    up = window.posted.index((WM_MBUTTONUP, 0))
    assert all(w == MK_MBUTTON for m, w in window.posted[down + 1:up])
    # let go of before the release is posted, or Firefox never fires pointerup
    assert window.held == [[VK_MBUTTON], [], []]


@pytest.mark.asyncio
async def test_a_right_button_drag_pans_the_way_miro_does():
    window = RecordingWindow()
    await window.drag(None, {'x': 10, 'y': 20}, {'x': 110, 'y': 70}, button=2)
    presses = [(m, w) for m, w in window.posted if m != win_input.WM_MOUSEMOVE]
    assert presses == [(WM_RBUTTONDOWN, MK_RBUTTON), (WM_RBUTTONUP, 0)]
    assert window.held[0] == [VK_RBUTTON]


def test_page_points_become_physical_screen_pixels():
    window = RecordingWindow()
    assert window.screen_point(10, 20) == (220, 140)


def test_without_screen_details_the_window_cannot_be_used():
    with pytest.raises(WindowInputUnavailable):
        WindowInput({'target': 'the page'})


def test_a_tab_that_is_not_showing_gets_no_window_messages():
    with pytest.raises(WindowInputUnavailable):
        WindowInput({'screen': {**PREP['screen'], 'hidden': True}, 'point': PREP['point']})


@pytest.mark.asyncio
async def test_another_firefox_window_at_the_same_spot_is_never_used(monkeypatch):
    monkeypatch.setattr(win_input, 'firefox_windows', lambda: [11, 22])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: {11: 'Mail - Mozilla Firefox', 22: 'Notes'}[hwnd])
    with pytest.raises(WindowInputUnavailable):
        async with WindowInput(PREP):
            pass


def test_a_hidden_tab_can_still_come_up():
    """Right after a tab is activated it can still read as hidden: the tool retries this one"""
    with pytest.raises(WindowNotShowing):
        WindowInput({'screen': {'x': 0, 'y': 0, 'dpr': 1, 'title': 'Page', 'hidden': True}})


def fake_windows(monkeypatch, foreground, same_firefox=False):
    posted = []
    monkeypatch.setattr(win_input, 'firefox_windows', lambda: [7])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: 'Page - Firefox')
    monkeypatch.setattr(win_input, 'ACTIVATE_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'same_firefox', lambda one, other: same_firefox or one == other)
    monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: foreground, raising=False)
    monkeypatch.setattr(win_input.user32, 'GetWindowThreadProcessId', lambda hwnd, _: 1, raising=False)
    monkeypatch.setattr(win_input.user32, 'PostMessageW', lambda h, m, w, l: posted.append((h, m, w)) or 1, raising=False)
    return posted


ACTIVE, INACTIVE = (7, win_input.WM_ACTIVATE, win_input.WA_ACTIVE), (7, win_input.WM_ACTIVATE, win_input.WA_INACTIVE)


@pytest.mark.asyncio
async def test_keys_make_a_background_window_active_and_it_steps_down_later(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99)
    async with WindowInput(PREP) as window:
        await window.make_active()
        await window.make_active()
        assert posted == [ACTIVE]
    # The timer itself is asyncio: what matters is what it does when it fires
    win_input.activated[7].cancel()
    win_input.step_down(7)
    assert posted == [ACTIVE, INACTIVE]
    assert not win_input.activated


@pytest.mark.asyncio
async def test_an_action_without_keys_leaves_the_window_as_it_was(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99)
    async with WindowInput(PREP):
        pass
    assert posted == []
    assert not win_input.activated


@pytest.mark.asyncio
async def test_activation_is_posted_again_even_with_a_step_down_pending(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99)
    async with WindowInput(PREP) as window:
        await window.make_active()
    async with WindowInput(PREP) as window:
        await window.make_active()
    assert posted == [ACTIVE, ACTIVE]
    win_input.activated[7].cancel()
    win_input.activated.clear()


@pytest.mark.asyncio
async def test_the_window_the_person_is_in_is_left_alone(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=7)
    async with WindowInput(PREP) as window:
        await window.make_active()
    assert posted == []
    assert not win_input.activated


@pytest.mark.asyncio
async def test_another_window_of_the_same_firefox_is_not_lowered(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    async with WindowInput(PREP) as window:
        with pytest.raises(WindowInputUnavailable):
            await window.make_active()
    assert posted == []


def test_no_screen_position_is_not_worth_retrying():
    with pytest.raises(WindowInputUnavailable) as caught:
        WindowInput({})
    assert not isinstance(caught.value, WindowNotShowing)
