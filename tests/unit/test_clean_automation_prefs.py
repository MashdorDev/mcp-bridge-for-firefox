#!/usr/bin/env python3
"""
scripts/clean-automation-prefs.py on a stand-in profile

Licensed under the MIT License - see LICENSE file for details
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'clean-automation-prefs.py'
PREFS = '''// Mozilla User Preferences
user_pref("app.update.disabledForTesting", true);
user_pref("browser.startup.page", 3);
user_pref("extensions.update.background.url", "http://%(server)s/dummy/updatebackground.xml");
user_pref("extensions.update.enabled", false);
user_pref("extensions.update.autoUpdateDefault", true);
user_pref("services.settings.server", "data:,#remote-settings-dummy/v1");
user_pref("signon.rememberSignons", false);
user_pref("browser.startup.homepage", "https://dummyjson.com/");
user_pref("browser.safebrowsing.phishing.enabled", false);
user_pref("extensions.webextensions.uuids", "{\\"dummy@example.org\\":\\"1234\\"}");
user_pref("browser.download.lastDir", "C:\\\\work\\\\dummy-data");
'''
KEPT = ['// Mozilla', 'browser.startup.page', 'autoUpdateDefault', 'signon.rememberSignons', 'webextensions.uuids', 'lastDir', 'dummyjson']


def run(profile, *more):
    return subprocess.run([sys.executable, str(SCRIPT), str(profile), *more], capture_output=True, text=True)


def profile(tmp_path):
    (tmp_path / 'prefs.js').write_text(PREFS, encoding='utf-8')
    return tmp_path


def test_without_apply_it_only_shows_them(tmp_path):
    result = run(profile(tmp_path))
    assert result.returncode == 0 and '5 of 12 lines' in result.stdout
    assert (tmp_path / 'prefs.js').read_text(encoding='utf-8') == PREFS
    assert 'signon.rememberSignons' not in result.stdout


def test_apply_removes_them_and_keeps_a_backup(tmp_path):
    assert run(profile(tmp_path), '--apply').returncode == 0
    cleaned = (tmp_path / 'prefs.js').read_text(encoding='utf-8')
    assert all(kept in cleaned for kept in KEPT)
    assert 'remote-settings-dummy' not in cleaned and '%(server)s' not in cleaned
    assert 'disabledForTesting' not in cleaned and 'update.enabled' not in cleaned and 'safebrowsing' not in cleaned
    assert list(tmp_path.glob('prefs.js.*.bak'))[0].read_text(encoding='utf-8') == PREFS


def test_a_folder_that_is_not_a_profile_is_refused(tmp_path):
    assert run(tmp_path, '--apply').returncode == 2
