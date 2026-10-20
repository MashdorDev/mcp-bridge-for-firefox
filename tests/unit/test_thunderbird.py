"""
Tests for Thunderbird as a connected browser: the app it reports in hello
"""

from unittest.mock import Mock

import test_imports  # Automatic path setup
from server.server import FoxMCPServer


def test_hello_records_thunderbird_and_names_it():
    server = FoxMCPServer(host="localhost", port=8765)
    socket = Mock()
    server._add_browser(socket)
    server._handle_hello(socket, {"instance_id": "abcd1234", "app": "thunderbird", "version": "0.1.0"})

    browser = server.browsers["abcd1234"]
    assert browser.app == "thunderbird"
    assert browser.name.startswith("Thunderbird ")


def test_hello_without_app_is_firefox():
    server = FoxMCPServer(host="localhost", port=8765)
    socket = Mock()
    server._add_browser(socket)
    server._handle_hello(socket, {"instance_id": "abcd1234", "name": "Work"})

    assert server.browsers["abcd1234"].app == "firefox"
    assert server.browsers["abcd1234"].name == "Work"
