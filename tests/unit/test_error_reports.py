"""
Tests for the opt-in error reports (extension/reporting.js)

A report must never carry a message, a page address or the install's uuid:
only an action name and error code, or an error type and the add-on's own
stack frames. Runs the module in Node.
"""

import json
import os
import shutil
import subprocess

import pytest

REPORTING_JS = os.path.join(os.path.dirname(__file__), "..", "..", "extension", "reporting.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


def run(expression):
    script = f"const r = require({json.dumps(os.path.abspath(REPORTING_JS))}); console.log(JSON.stringify({expression}))"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_reported_code_names_the_action_and_nothing_else():
    event = run("r.describeActionError('CAPTURE_ERROR', 'tabs.captureVisibleTab')")
    assert event["fingerprint"] == ["CAPTURE_ERROR tabs.captureVisibleTab"]
    assert event["tags"] == {"action": "tabs.captureVisibleTab", "code": "CAPTURE_ERROR"}


@pytest.mark.parametrize("code", ["INVALID_PARAMETER", "NOT_ALLOWED", "NOT_PAIRED", "SCRIPT_ERROR", "PAGE_ERROR"])
def test_agent_settings_and_page_errors_are_not_reported(code):
    assert run(f"r.describeActionError({json.dumps(code)}, 'content.click')") is None


def test_odd_action_name_is_not_sent():
    event = run("r.describeActionError('API_ERROR', 'https://example.org/secret')")
    assert event["tags"]["action"] == "unknown"


def test_crash_keeps_only_addon_frames_without_the_uuid():
    stack = "\n".join([
        "handleTabsAction@moz-extension://0f2a6c1e-5b7d-4e3a-9c11-7d0e5a1b2c3d/background.js:1162:5",
        "pageFn@https://example.org/app.js:10:2",
        "routeAction@moz-extension://0f2a6c1e-5b7d-4e3a-9c11-7d0e5a1b2c3d/background.js:690:7",
    ])
    event = run(f"r.describeCrash({{name: 'TypeError', message: 'secret https://example.org/x', stack: {json.dumps(stack)}}})")
    text = json.dumps(event)
    assert "example.org" not in text and "secret" not in text and "0f2a6c1e" not in text
    assert event["fingerprint"] == ["TypeError at background.js:1162"]
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert [f["function"] for f in frames] == ["routeAction", "handleTabsAction"]


def test_line_mixing_a_page_address_with_an_addon_frame_is_dropped():
    line = "x@https://example.org/a.js:1:1 y@moz-extension://0f2a6c1e/background.js:2:3"
    assert run(f"r.reportFrames({json.dumps(line)})") == []


def test_error_from_outside_the_addon_is_not_reported():
    assert run("r.describeCrash({name: 'Error', stack: 'fn@https://example.org/a.js:1:1'})") is None
    assert run("r.describeCrash('a string reason')") is None


@pytest.mark.parametrize("message", [
    "No tab with id 42",
    "Tabs API error: Invalid tab ID: 42",
    "Unknown upload u-1",
    "Content API error: can't access property \"query\", data is undefined",
])
def test_agent_mistakes_inside_reported_codes_are_not_reported(message):
    assert run(f"r.describeActionError('CAPTURE_ERROR', 'tabs.captureVisibleTab', {json.dumps(message)})") is None


SEND = """
const sent = [];
globalThis.browser = {
  storage: {local: {get: async () => ({errorReports: %s})}},
  permissions: {getAll: async () => (%s)},
  runtime: {getManifest: () => ({version: '0.0.0'})},
};
globalThis.fetch = async (url, init) => { sent.push(JSON.parse(init.body.split(String.fromCharCode(10))[2]).fingerprint[0]); };
const r = require(%s);
(async () => {
  await Promise.all(Array.from({length: 30}, (_, i) => r.reportActionError('API_ERROR', i < 25 ? 'tabs.list' : 'tabs.a' + i)));
  console.log(JSON.stringify(sent));
})();
"""


def send_burst(setting, permissions):
    script = SEND % (setting, permissions, json.dumps(os.path.abspath(REPORTING_JS)))
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_failures_arriving_together_send_one_report_each():
    sent = send_burst("true", "{data_collection: ['technicalAndInteraction']}")
    assert sorted(sent) == ["API_ERROR tabs.a25", "API_ERROR tabs.a26", "API_ERROR tabs.a27", "API_ERROR tabs.a28", "API_ERROR tabs.a29", "API_ERROR tabs.list"]


@pytest.mark.parametrize("setting,permissions", [
    ("false", "{data_collection: ['technicalAndInteraction']}"),
    ("true", "{data_collection: []}"),
    ("true", "{permissions: [], origins: []}"),  # a Firefox with no data-collection grants
])
def test_nothing_is_sent_without_the_setting_and_the_grant(setting, permissions):
    assert send_burst(setting, permissions) == []
