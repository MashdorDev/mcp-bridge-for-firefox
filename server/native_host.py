"""
The pairing token for the Firefox add-on, over native messaging

Firefox starts this file when the add-on asks for the token, so pairing needs
nothing typed into the add-on's options. It answers {"get": "pairing-token"}
with the contents of ~/.foxmcp/pairing-token and nothing else, and Firefox lets
only the add-on named in the host manifest start it.

The server registers it at startup (register_native_host). It copies this
file into ~/.foxmcp/native-host/ and runs it with the server's own Python, so
the registration does not depend on where the server was installed. Keep this
file to the standard library for that reason.

Licensed under the MIT License - see LICENSE file for details
"""

import codecs
import json
import logging
import os
import secrets
import struct
import sys

logger = logging.getLogger(__name__)

HOST_NAME = 'dev.dorzairi.foxmcp'
ADDON_ID = 'foxmcp@dorzairi.dev'
FOXMCP_DIR = os.path.join(os.path.expanduser('~'), '.foxmcp')
PAIRING_TOKEN_FILE = os.path.join(FOXMCP_DIR, 'pairing-token')
TOKEN_REQUEST = {'get': 'pairing-token'}
# A request is a few bytes; anything longer is not one
MAX_REQUEST_BYTES = 1024


def ensure_pairing_token(path: str = PAIRING_TOKEN_FILE) -> str:
    """The token in `path`, created on first use, readable only by this user"""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(secrets.token_hex(16))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows: the profile folder is already private to the user
    with open(path) as f:
        return f.read().strip()


def answer(request, token_path: str = PAIRING_TOKEN_FILE) -> dict:
    """The reply to one request: the token for the one request there is"""
    if request != TOKEN_REQUEST:
        return {'error': 'unknown request'}
    return {'pairing_token': ensure_pairing_token(token_path)}


def serve_one(stdin, stdout, token_path: str = PAIRING_TOKEN_FILE) -> bool:
    """Read one length-prefixed JSON message and write the reply; False at end of input"""
    header = stdin.read(4)
    if len(header) < 4:
        return False
    (length,) = struct.unpack('<I', header)
    if length > MAX_REQUEST_BYTES:
        reply = {'error': 'request too long'}
    else:
        try:
            reply = answer(json.loads(stdin.read(length)), token_path)
        except ValueError:
            reply = {'error': 'not JSON'}
    body = json.dumps(reply).encode()
    stdout.write(struct.pack('<I', len(body)) + body)
    stdout.flush()
    return length <= MAX_REQUEST_BYTES


def host_paths(platform: str = sys.platform, home: str = os.path.expanduser('~')):
    """Where the copied host, its launcher and its manifest go on this platform"""
    folder = os.path.join(home, '.foxmcp', 'native-host')
    if platform == 'win32':
        manifest = os.path.join(folder, f'{HOST_NAME}.json')
        launcher = os.path.join(folder, 'foxmcp-native-host.cmd')
    else:
        if platform == 'darwin':
            mozilla = os.path.join(home, 'Library', 'Application Support', 'Mozilla', 'NativeMessagingHosts')
        else:
            mozilla = os.path.join(home, '.mozilla', 'native-messaging-hosts')
        manifest = os.path.join(mozilla, f'{HOST_NAME}.json')
        launcher = os.path.join(folder, 'foxmcp-native-host.sh')
    return {'script': os.path.join(folder, 'native_host.py'), 'launcher': launcher, 'manifest': manifest}


def _launcher_text(platform: str, python: str, script: str) -> str:
    # -I: no PYTHON* variables, no user site and not the script's folder on
    # the path, so nothing else on the machine changes what this runs
    if platform == 'win32':
        quote = lambda path: '"' + path.replace('%', '%%') + '"'
        return f'@echo off\r\n{quote(python)} -I {quote(script)} %*\r\n'
    quote = lambda path: "'" + path.replace("'", "'\\''") + "'"
    return f'#!/bin/sh\nexec {quote(python)} -I {quote(script)} "$@"\n'


def _write_if_changed(path: str, text: str, encoding: str = 'utf-8') -> None:
    try:
        with open(path, newline='', encoding=encoding) as f:
            if f.read() == text:
                return
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding=encoding) as f:
        f.write(text)


def _launcher_encoding(platform: str) -> str:
    # cmd.exe reads a batch file in the OEM code page, so a path such as
    # C:\Users\<Hebrew name> has to be written in it; a path it cannot
    # encode fails the registration, which is logged
    if platform == 'win32':
        try:
            return codecs.lookup('oem').name
        except LookupError:
            pass  # not Windows itself, as in the tests
    return 'utf-8'


def _register_in_windows_registry(manifest: str, winreg=None) -> None:
    if winreg is None:
        import winreg
    key_path = rf'Software\Mozilla\NativeMessagingHosts\{HOST_NAME}'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        try:
            if winreg.QueryValueEx(key, '')[0] == manifest:
                return
        except OSError:
            pass
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, manifest)


def register_native_host(python: str = sys.executable, platform: str = sys.platform,
                         home: str = os.path.expanduser('~'), winreg=None) -> bool:
    """Make this user's Firefox able to start the host, without admin rights

    Safe to run at every start: files and the registry value are written only
    when they differ. A failure is logged and returns False, since the add-on
    can still pair with a typed token.
    """
    try:
        paths = host_paths(platform, home)
        with open(os.path.abspath(__file__), newline='') as f:
            _write_if_changed(paths['script'], f.read())
        _write_if_changed(paths['launcher'], _launcher_text(platform, python, paths['script']),
                          _launcher_encoding(platform))
        if platform != 'win32':
            os.chmod(paths['launcher'], 0o755)
        _write_if_changed(paths['manifest'], json.dumps({
            'name': HOST_NAME,
            'description': 'Gives the MCP Bridge add-on the pairing token in ~/.foxmcp/pairing-token',
            'path': paths['launcher'],
            'type': 'stdio',
            'allowed_extensions': [ADDON_ID],
        }, indent=2) + '\n')
        if platform == 'win32':
            _register_in_windows_registry(paths['manifest'], winreg)
        return True
    except Exception as e:
        logger.warning(f"Could not register the native messaging host, so the add-on "
                       f"pairs only with a token typed into its options: {e}")
        return False


def main() -> None:
    while serve_one(sys.stdin.buffer, sys.stdout.buffer):
        pass


if __name__ == '__main__':
    main()
