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


@pytest.mark.asyncio
async def test_another_firefox_window_at_the_same_spot_is_never_used(monkeypatch):
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [11, 22])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: {11: 'Mail - Mozilla Firefox', 22: 'Notes'}[hwnd])
    with pytest.raises(WindowInputUnavailable):
        async with WindowInput(PREP):
            pass


@pytest.mark.parametrize('window, page, shows', [
    ('Inbox \u2014 Mozilla Firefox', 'Inbox', True),
    ('Inbox - Mozilla Firefox', 'Inbox', True),
    ('Inbox — Original profile — Mozilla Firefox', 'Inbox', True),
    ('Inbox', 'Inbox', True),
    ('Inbox - Draft to boss \u2014 Mozilla Firefox', 'Inbox', False),
    ('Home Depot \u2014 Mozilla Firefox', 'Home', False),
    ('Anything', '', False),
])
def test_a_window_title_must_be_the_page_and_the_browser_name_only(window, page, shows):
    assert win_input.shows_page(window, page) is shows


def hidden_windows(monkeypatch, windows, iconic=()):
    """windows maps hwnd to (title, contains the page's position)"""
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [
        h for h in windows if minimised or h not in iconic])
    monkeypatch.setattr(win_input, 'window_title', lambda h: windows[h][0])
    monkeypatch.setattr(win_input, 'window_contains', lambda h, x, y: windows[h][1])
    monkeypatch.setattr(win_input.user32, 'IsIconic', lambda h: h in iconic, raising=False)


def test_a_covered_window_needs_position_and_title_to_agree(monkeypatch):
    hidden_windows(monkeypatch, {7: ('Page \u2014 Mozilla Firefox', True), 8: ('Page \u2014 Mozilla Firefox', False)})
    assert win_input.hidden_window('Page', 0, 0) == 7


def test_the_window_in_use_elsewhere_with_this_title_is_never_taken(monkeypatch):
    hidden_windows(monkeypatch, {8: ('Page \u2014 Mozilla Firefox', False)})
    with pytest.raises(WindowNotShowing):
        win_input.hidden_window('Page', 0, 0)


def test_one_minimised_window_with_the_title_is_taken(monkeypatch):
    hidden_windows(monkeypatch, {7: ('Page \u2014 Mozilla Firefox', False)}, iconic=(7,))
    assert win_input.hidden_window('Page', 0, 0) == 7


def test_two_minimised_windows_with_the_title_are_refused(monkeypatch):
    hidden_windows(monkeypatch, {7: ('Page \u2014 Mozilla Firefox', False), 8: ('Page \u2014 Mozilla Firefox', False)}, iconic=(7, 8))
    with pytest.raises(WindowNotShowing):
        win_input.hidden_window('Page', 0, 0)


def test_a_minimised_window_refuses_a_click(monkeypatch):
    monkeypatch.setattr(win_input.user32, 'IsIconic', lambda hwnd: 1, raising=False)
    window = WindowInput(PREP)
    with pytest.raises(WindowInputUnavailable):
        window.client_point(10, 20)


def test_the_agent_window_comes_back_behind_everything_and_asks_again(monkeypatch):
    calls = []
    monkeypatch.setattr(win_input.user32, 'IsIconic', lambda hwnd: 1, raising=False)
    monkeypatch.setattr(win_input.user32, 'ShowWindow', lambda hwnd, how: calls.append(how), raising=False)
    monkeypatch.setattr(win_input.user32, 'SetWindowPos', lambda *a: calls.append(a[1]), raising=False)
    window = WindowInput({**PREP, 'agentWindow': True})
    with pytest.raises(WindowNotShowing):
        window.client_point(10, 20)
    assert calls == [win_input.SW_SHOWNOACTIVATE, win_input.HWND_BOTTOM]


def fake_windows(monkeypatch, foreground, same_firefox=False):
    posted = []
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [7])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: 'Page - Firefox')
    monkeypatch.setattr(win_input, 'ACTIVATE_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'same_firefox', lambda one, other: same_firefox or one == other)
    monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: foreground, raising=False)
    monkeypatch.setattr(win_input.user32, 'GetWindowThreadProcessId', lambda hwnd, _: 1, raising=False)
    monkeypatch.setattr(win_input.user32, 'IsIconic', lambda hwnd: 0, raising=False)
    monkeypatch.setattr(win_input.user32, 'SetWindowPos', lambda *a: 1, raising=False)
    monkeypatch.setattr(win_input.user32, 'PostMessageW', lambda h, m, w, l: posted.append((h, m, w)) or 1, raising=False)
    return posted


ACTIVE, INACTIVE = (7, win_input.WM_ACTIVATE, win_input.WA_ACTIVE), (7, win_input.WM_ACTIVATE, win_input.WA_INACTIVE)


@pytest.mark.asyncio
async def test_keys_make_a_background_window_active_and_it_steps_down_later(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99)
    async with WindowInput(PREP) as window:
        await window.make_active()
        assert set(posted) == {ACTIVE}
    posted.clear()
    # The timer itself is asyncio: what matters is what it does when it fires
    win_input.activated[7].cancel()
    win_input.step_down(7)
    assert posted == [INACTIVE]
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
    first = len(posted)
    async with WindowInput(PREP) as window:
        await window.make_active()
    assert first and len(posted) > first and set(posted) == {ACTIVE}
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
async def test_with_the_person_idle_in_this_firefox_activation_is_borrowed_and_returned(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 60)
    async with WindowInput(PREP) as window:
        await window.make_active()
        assert posted == [(99, win_input.WM_ACTIVATE, win_input.WA_INACTIVE), ACTIVE]
    assert posted[2:] == [INACTIVE, (99, win_input.WM_ACTIVATE, win_input.WA_ACTIVE)]
    assert not win_input.activated


@pytest.mark.asyncio
async def test_with_the_person_busy_in_this_firefox_their_window_is_not_lowered(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 0)
    monkeypatch.setattr(win_input, 'IDLE_WAIT_MAX_S', 0)
    async with WindowInput(PREP) as window:
        with pytest.raises(WindowInputUnavailable):
            await window.make_active()
    assert posted == []


@pytest.mark.asyncio
async def test_the_person_coming_back_mid_typing_gets_activation_back(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    idle = [60]
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: idle.pop(0) if len(idle) > 1 else idle[0])
    monkeypatch.setattr(win_input, 'IDLE_POLL_S', 0)
    async with WindowInput(PREP) as window:
        await window.make_active()
        idle[:] = [0, 60]
        await window.make_active()
        assert posted[2:4] == [INACTIVE, (99, win_input.WM_ACTIVATE, win_input.WA_ACTIVE)]
        assert window.lent_by == 99


@pytest.mark.asyncio
async def test_the_person_arriving_in_this_firefox_mid_action_stops_the_keys_going_there(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=55, same_firefox=False)
    async with WindowInput(PREP) as window:
        await window.make_active()
        monkeypatch.setattr(win_input, 'same_firefox', lambda one, other: True)
        monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: 99, raising=False)
        monkeypatch.setattr(win_input, 'seconds_idle', lambda: 0)
        monkeypatch.setattr(win_input, 'IDLE_WAIT_MAX_S', 0)
        with pytest.raises(WindowInputUnavailable):
            await window.make_active()
    assert set(posted) == {ACTIVE}
    win_input.activated[7].cancel()
    win_input.activated.clear()


@pytest.mark.asyncio
async def test_a_moment_with_no_foreground_does_not_end_the_loan(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 60)
    async with WindowInput(PREP) as window:
        await window.make_active()
        monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: 0, raising=False)
        await window.make_active()
        assert window.lent_by == 99
    assert posted[2:] == [INACTIVE, (99, win_input.WM_ACTIVATE, win_input.WA_ACTIVE)]


@pytest.mark.asyncio
async def test_activation_is_taken_again_when_the_person_leaves_for_another_program(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 60)
    async with WindowInput(PREP) as window:
        await window.make_active()
        monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: 55, raising=False)
        monkeypatch.setattr(win_input, 'same_firefox', lambda one, other: one == other)
        posted.clear()
        await window.make_active()
        assert window.lent_by == 0 and set(posted) == {ACTIVE}
    win_input.activated[7].cancel()
    win_input.activated.clear()


@pytest.mark.asyncio
async def test_an_untitled_page_in_the_agent_window_is_found_by_the_prefix(monkeypatch):
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [11, 22])
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: {11: 'Mail', 22: '[Agent] Mozilla Firefox'}[hwnd])
    monkeypatch.setattr(win_input.user32, 'GetWindowThreadProcessId', lambda hwnd, _: 1, raising=False)
    prep = {'screen': {'x': 1, 'y': 1, 'dpr': 1, 'title': '', 'hidden': True}, 'agentWindow': True}
    async with WindowInput(prep) as window:
        assert window.hwnd == 22


@pytest.mark.asyncio
async def test_a_stop_after_input_went_in_is_reported_never_redone(monkeypatch):
    fake_windows(monkeypatch, foreground=7)
    with pytest.raises(RuntimeError, match='part of the input'):
        async with WindowInput(PREP) as window:
            window.post(WM_CHAR, ord('a'), 1)
            raise WindowInputUnavailable('of a test')


@pytest.mark.asyncio
async def test_a_stop_before_any_input_may_still_fall_back(monkeypatch):
    fake_windows(monkeypatch, foreground=7)
    with pytest.raises(WindowInputUnavailable):
        async with WindowInput(PREP) as window:
            window.post(win_input.WM_MOUSEMOVE, 0, 0)
            raise WindowInputUnavailable('of a test')


@pytest.mark.asyncio
async def test_keys_stop_when_the_tab_stops_showing(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=7)

    async def gone():
        return False

    async with WindowInput(PREP, still_showing=gone) as window:
        with pytest.raises(WindowInputUnavailable):
            await window.press(None, 'Enter')
    assert posted == []


@pytest.mark.asyncio
async def test_firefox_raising_the_borrowing_window_does_not_end_the_loan(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 60)
    async with WindowInput(PREP) as window:
        await window.make_active()
        monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: 7, raising=False)
        await window.make_active()
        assert window.lent_by == 99 and len(posted) == 2
    assert posted[2:] == [INACTIVE, (99, win_input.WM_ACTIVATE, win_input.WA_ACTIVE)]
    assert not win_input.on_loan


@pytest.mark.asyncio
async def test_the_loan_returns_nothing_to_a_window_the_person_has_left(monkeypatch):
    posted = fake_windows(monkeypatch, foreground=99, same_firefox=True)
    monkeypatch.setattr(win_input, 'SWAP_SETTLE_S', 0)
    monkeypatch.setattr(win_input, 'seconds_idle', lambda: 60)
    async with WindowInput(PREP) as window:
        await window.make_active()
        monkeypatch.setattr(win_input.user32, 'GetForegroundWindow', lambda: 55, raising=False)
    assert posted[2:] == [INACTIVE]


@pytest.mark.asyncio
async def test_a_page_with_no_title_needs_the_spot_to_itself(monkeypatch):
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [11, 22])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: 'Mozilla Firefox')
    with pytest.raises(WindowNotShowing):
        async with WindowInput({'screen': {'x': 1, 'y': 1, 'dpr': 1, 'title': ''}}):
            pass


@pytest.mark.asyncio
async def test_a_prefix_of_a_title_does_not_pick_the_window(monkeypatch):
    monkeypatch.setattr(win_input, 'firefox_windows', lambda minimised=False: [11, 22])
    monkeypatch.setattr(win_input, 'window_contains', lambda hwnd, x, y: True)
    monkeypatch.setattr(win_input, 'window_title', lambda hwnd: {11: 'Page - Issues \u2014 Mozilla Firefox', 22: 'Page \u2014 Mozilla Firefox'}[hwnd])
    monkeypatch.setattr(win_input.user32, 'GetWindowThreadProcessId', lambda hwnd, _: 1, raising=False)
    async with WindowInput(PREP) as window:
        assert window.hwnd == 22


@pytest.mark.asyncio
async def test_the_machine_turn_is_one_at_a_time(monkeypatch):
    monkeypatch.setattr(win_input, 'MACHINE_TURN_WAIT_S', 0)
    async with win_input.machine_turn():
        with pytest.raises(WindowInputUnavailable):
            async with win_input.machine_turn():
                pass
    async with win_input.machine_turn():
        pass


def test_the_agent_windows_title_is_never_a_bare_page_title():
    window = WindowInput({**PREP, 'agentWindow': True})
    assert window.title == '[Agent] Page'


def test_no_screen_position_is_not_worth_retrying():
    with pytest.raises(WindowInputUnavailable) as caught:
        WindowInput({})
    assert not isinstance(caught.value, WindowNotShowing)
