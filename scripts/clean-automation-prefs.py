#!/usr/bin/env python3
"""
Remove the test prefs --remote-debugging-port leaves in a Firefox profile

They turn off add-on and Firefox updates, the blocklist, Safe Browsing and the
popup blocker, and point addons.mozilla.org, telemetry and Firefox Accounts at dummy
addresses. Shows what would go unless given --apply, as a pref someone turned off
on purpose looks the same. Firefox must be closed: it rewrites prefs.js on exit.

Licensed under the MIT License - see LICENSE file for details
"""

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

PREF = re.compile(r'^user_pref\("([^"]+)",\s*(.*)\);\s*$')
# The addresses the flag writes: a real one can have "dummy" in it too
DUMMY_ADDRESS = re.compile(r'^"(https?://|data:,)[^"]*(%\(server\)s|://dummy\.test|remote-settings-dummy)[^"]*"$')
TEST_VALUES = {'app.update.disabledForTesting': 'true', 'extensions.update.enabled': 'false',
               'remote.prefs.recommended': 'true', 'browser.safebrowsing.blockedURIs.enabled': 'false',
               'browser.safebrowsing.downloads.enabled': 'false', 'browser.safebrowsing.malware.enabled': 'false',
               'browser.safebrowsing.phishing.enabled': 'false', 'browser.safebrowsing.passwords.enabled': 'false',
               'dom.disable_open_during_load': 'false', 'security.fileuri.strict_origin_policy': 'false'}


def left_by_automation(line: str) -> bool:
    match = PREF.match(line)
    if not match:
        return False
    name, value = match.groups()
    return bool(DUMMY_ADDRESS.match(value)) or TEST_VALUES.get(name) == value


def firefox_is_running(profile: Path) -> bool:
    lock = profile / 'parent.lock'
    if sys.platform == 'win32':
        # Held open with no sharing while Firefox runs, and left behind after
        try:
            with open(lock, 'a'):
                return False
        except FileNotFoundError:
            return False
        except OSError:
            return True
    # Linux: a symlink to "address:+pid", whose target never exists as a file
    link = profile / 'lock'
    if link.is_symlink():
        try:
            os.kill(int(os.readlink(link).rpartition('+')[2]), 0)
            return True
        except (ValueError, ProcessLookupError):
            pass
        except PermissionError:
            return True
    # macOS, and Linux too: a file Firefox holds a lock on
    import fcntl
    try:
        with open(profile / '.parentlock', 'a') as held:
            fcntl.lockf(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


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
