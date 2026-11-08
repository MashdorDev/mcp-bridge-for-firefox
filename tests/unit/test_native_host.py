"""
Tests for the native messaging host that gives the Firefox add-on the pairing
token, and for the server registering it at startup
"""

import io
import json
import os
import struct
import subprocess
import sys

import test_imports  # Automatic path setup
from server import native_host
from server.native_host import (ADDON_ID, HOST_NAME, answer, host_paths,
                                register_native_host, serve_one)


def framed(message) -> bytes:
    body = json.dumps(message).encode()
    return struct.pack('<I', len(body)) + body


def replies(raw: bytes) -> list:
    out, at = [], 0
    while at < len(raw):
        (length,) = struct.unpack('<I', raw[at:at + 4])
        out.append(json.loads(raw[at + 4:at + 4 + length]))
        at += 4 + length
    return out


def test_the_token_request_gets_the_token_file(tmp_path):
    token_path = tmp_path / 'pairing-token'
    token_path.write_text('abc123\n')

    assert answer({'get': 'pairing-token'}, str(token_path)) == {'pairing_token': 'abc123'}


def test_the_token_is_created_like_the_server_creates_it(tmp_path):
    token_path = tmp_path / '.foxmcp' / 'pairing-token'
    token = answer({'get': 'pairing-token'}, str(token_path))['pairing_token']

    assert len(token) == 32
    assert token_path.read_text() == token


def test_any_other_request_gets_no_token(tmp_path):
    token_path = tmp_path / 'pairing-token'
    token_path.write_text('secret')

    for request in ({'get': 'other'}, {'get': 'pairing-token', 'x': 1}, 'pairing-token', None, []):
        reply = answer(request, str(token_path))
        assert 'secret' not in json.dumps(reply)
        assert reply == {'error': 'unknown request'}


def test_messages_are_length_prefixed_both_ways(tmp_path):
    token_path = tmp_path / 'pairing-token'
    token_path.write_text('abc123')
    stdin = io.BytesIO(framed({'get': 'pairing-token'}) + framed({'get': 'nope'}))
    stdout = io.BytesIO()

    while serve_one(stdin, stdout, str(token_path)):
        pass

    assert replies(stdout.getvalue()) == [{'pairing_token': 'abc123'}, {'error': 'unknown request'}]


def test_a_request_too_long_is_refused_and_ends_the_host(tmp_path):
    stdin = io.BytesIO(struct.pack('<I', 10_000_000) + b'x' * 10)
    stdout = io.BytesIO()

    assert serve_one(stdin, stdout, str(tmp_path / 'pairing-token')) is False
    assert replies(stdout.getvalue()) == [{'error': 'request too long'}]


def test_the_host_runs_as_firefox_starts_it(tmp_path):
    """As a separate process, binary stdio, with the file copied out of the package"""
    copy = tmp_path / 'native_host.py'
    copy.write_text(open(native_host.__file__).read())
    home = tmp_path / 'home'
    (home / '.foxmcp').mkdir(parents=True)
    (home / '.foxmcp' / 'pairing-token').write_text('from-the-file')
    env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home)}

    done = subprocess.run([sys.executable, str(copy)], input=framed({'get': 'pairing-token'}),
                          capture_output=True, env=env, timeout=30)

    assert replies(done.stdout) == [{'pairing_token': 'from-the-file'}]


class FakeWinreg:
    HKEY_CURRENT_USER = 'HKCU'
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.writes = 0

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def CreateKey(self, root, path):
        return self._Key((root, path))

    def QueryValueEx(self, key, name):
        if key.path not in self.values:
            raise FileNotFoundError(name)
        return self.values[key.path], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[key.path] = value
        self.writes += 1


def test_windows_registers_under_hkcu_with_a_cmd_launcher(tmp_path):
    winreg = FakeWinreg()
    python = r'C:\venv dir\Scripts\python.exe'

    assert register_native_host(python=python, platform='win32', home=str(tmp_path), winreg=winreg)

    paths = host_paths('win32', str(tmp_path))
    key = ('HKCU', rf'Software\Mozilla\NativeMessagingHosts\{HOST_NAME}')
    assert winreg.values == {key: paths['manifest']}
    manifest = json.loads(open(paths['manifest']).read())
    assert manifest == {'name': HOST_NAME, 'description': manifest['description'], 'path': paths['launcher'],
                        'type': 'stdio', 'allowed_extensions': [ADDON_ID]}
    launcher = open(paths['launcher'], newline='').read()
    assert launcher == f'@echo off\r\n"{python}" -I "{paths["script"]}" %*\r\n'
    assert open(paths['script']).read() == open(native_host.__file__).read()


def test_registering_again_changes_nothing(tmp_path):
    winreg = FakeWinreg()
    register_native_host(python='py', platform='win32', home=str(tmp_path), winreg=winreg)
    paths = host_paths('win32', str(tmp_path))
    old = 1_000_000_000_000_000_000  # 2001, in ns: a time no write today leaves
    for path in paths.values():
        os.utime(path, ns=(old, old))

    assert register_native_host(python='py', platform='win32', home=str(tmp_path), winreg=winreg)

    assert winreg.writes == 1
    assert {os.stat(path).st_mtime_ns for path in paths.values()} == {old}


def test_a_new_python_rewrites_the_launcher(tmp_path):
    winreg = FakeWinreg()
    register_native_host(python='old', platform='win32', home=str(tmp_path), winreg=winreg)
    register_native_host(python='new', platform='win32', home=str(tmp_path), winreg=winreg)

    assert '"new"' in open(host_paths('win32', str(tmp_path))['launcher']).read()


def test_macos_and_linux_put_the_manifest_where_firefox_looks(tmp_path):
    mac = host_paths('darwin', str(tmp_path))['manifest']
    linux = host_paths('linux', str(tmp_path))['manifest']

    assert mac == os.path.join(str(tmp_path), 'Library', 'Application Support', 'Mozilla',
                               'NativeMessagingHosts', f'{HOST_NAME}.json')
    assert linux == os.path.join(str(tmp_path), '.mozilla', 'native-messaging-hosts', f'{HOST_NAME}.json')


def test_linux_launcher_is_a_quoted_shell_script(tmp_path):
    assert register_native_host(python="/opt/it's here/python", platform='linux', home=str(tmp_path))

    paths = host_paths('linux', str(tmp_path))
    launcher = open(paths['launcher']).read()
    assert launcher.startswith('#!/bin/sh\nexec \'/opt/it\'\\\'\'s here/python\' -I ')
    assert json.loads(open(paths['manifest']).read())['path'] == paths['launcher']
    if sys.platform != 'win32':
        assert os.stat(paths['launcher']).st_mode & 0o111


def test_a_failure_to_register_only_logs(tmp_path, caplog):
    class BrokenWinreg(FakeWinreg):
        def CreateKey(self, root, path):
            raise PermissionError('denied')

    assert register_native_host(python='py', platform='win32', home=str(tmp_path), winreg=BrokenWinreg()) is False
    assert 'Could not register the native messaging host' in caplog.text
