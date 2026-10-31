#!/usr/bin/env python3
"""
Remove the test prefs --remote-debugging-port leaves in a Firefox profile

They turn off add-on and Firefox updates and the blocklist, and point
addons.mozilla.org, telemetry and Firefox Accounts at dummy addresses. Shows what
would go unless given --apply. Firefox must be closed: it rewrites prefs.js on exit.

Licensed under the MIT License - see LICENSE file for details
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

PREF = re.compile(r'^user_pref\("([^"]+)",\s*(.*)\);\s*$')
DUMMY_VALUE = re.compile(r'%\(server\)s|dummy', re.IGNORECASE)
# Off only under test, so with no dummy address to give them away
TEST_ONLY = {'app.update.disabledForTesting': 'true', 'extensions.update.enabled': 'false',
             'remote.prefs.recommended': 'true'}


def left_by_automation(line: str) -> bool:
    match = PREF.match(line)
    if not match:
        return False
    name, value = match.groups()
    return bool(DUMMY_VALUE.search(value)) or TEST_ONLY.get(name) == value


def firefox_is_running(profile: Path) -> bool:
    lock = profile / 'parent.lock'
    if sys.platform == 'win32':
        # Held open with no sharing while Firefox runs, and left behind after
        try:
            lock.rename(lock)
            with open(lock, 'a'):
                return False
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return (profile / 'lock').is_symlink() and (profile / 'lock').exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('profile', type=Path, help='The profile folder, the one holding prefs.js')
    parser.add_argument('--apply', action='store_true', help='Remove them, keeping a backup of prefs.js')
    args = parser.parse_args()
    prefs = args.profile / 'prefs.js'
    if not prefs.is_file():
        print(f'{prefs} not found: this is not a profile folder', file=sys.stderr)
        return 2
    lines = prefs.read_text(encoding='utf-8').splitlines(keepends=True)
    found = [line for line in lines if left_by_automation(line)]
    for line in found:
        print(line.rstrip())
    print(f'{len(found)} of {len(lines)} lines left by automation')
    if not args.apply or not found:
        return 0
    if firefox_is_running(args.profile):
        print('Firefox is running on this profile: close it first', file=sys.stderr)
        return 1
    backup = prefs.with_name(f'prefs.js.{time.strftime("%Y%m%d-%H%M%S")}.bak')
    shutil.copy2(prefs, backup)
    prefs.write_text(''.join(line for line in lines if not left_by_automation(line)), encoding='utf-8')
    print(f'Removed. The old file is {backup}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
