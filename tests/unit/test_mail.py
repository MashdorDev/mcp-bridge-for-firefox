"""
Tests for the read-only mail tools: the request each sends and how the answer reads

The add-on side (thunderbird/mail.js) is checked by hand against a real
Thunderbird; these stub the server.
"""

import pytest

import test_imports  # Automatic path setup
from fastmcp.exceptions import ToolError
from server.mcp_tools import FoxMCPTools
from server.server import BrowserConnection


class StubServer:
    def __init__(self, data=None):
        self.browsers = {"tb": BrowserConnection(key="tb", websocket=None, name="Mail", app="thunderbird", paired=True)}
        self.active_browser = "tb"
        self.sent_request = None
        self.response = {"type": "response", "data": data or {}}

    async def send_request_and_wait(self, request):
        self.sent_request = request
        return self.response


async def call(server, name, **kwargs):
    tool = await FoxMCPTools(server).mcp.get_tool(name)
    try:
        return await tool.fn(**kwargs)
    except ToolError as error:
        return str(error)


@pytest.mark.asyncio
async def test_accounts_list_names_ids_and_addresses():
    server = StubServer({"accounts": [{"id": "account1", "name": "Work", "type": "imap",
                                       "identities": ["me@example.org"]}]})
    result = await call(server, "mail_list_accounts")

    assert server.sent_request["action"] == "mail.listAccounts"
    assert "- Work (imap): id account1, me@example.org" in result


@pytest.mark.asyncio
async def test_folders_can_be_limited_to_one_account():
    server = StubServer({"folders": [{"id": "f1", "accountId": "account1", "path": "/INBOX", "name": "Inbox",
                                      "specialUse": ["inbox"], "unread": 3, "total": 40}]})
    result = await call(server, "mail_list_folders", account_id="account1")

    assert server.sent_request["data"] == {"accountId": "account1"}
    assert "- account1 /INBOX [inbox]: id f1, 3 unread of 40" in result


@pytest.mark.asyncio
async def test_messages_list_newest_first_with_ids_and_marks():
    server = StubServer({"total": 2, "messages": [
        {"id": 7, "date": "2026-10-19T15:00:00.000Z", "author": "Chris <chris@example.org>",
         "subject": "Sync", "read": False, "flagged": True},
        {"id": 6, "date": "2026-10-18T09:30:00.000Z", "author": "Bot <noreply@example.org>",
         "subject": "", "read": True, "flagged": False},
    ]})
    result = await call(server, "mail_list_messages", folder_id="f1", limit=500)

    assert server.sent_request["data"] == {"folderId": "f1", "sinceDays": 30, "unreadOnly": False, "limit": 200}
    assert "- [7] 2026-10-19T15:00 unread flagged Chris <chris@example.org>: Sync" in result
    assert "- [6] 2026-10-18T09:30 Bot <noreply@example.org>: (no subject)" in result


@pytest.mark.asyncio
async def test_an_add_on_error_is_a_tool_error():
    server = StubServer()
    server.response = {"type": "error", "data": {"message": "Folder not found"}}

    assert "Failed to read mail: Folder not found" in await call(server, "mail_list_messages", folder_id="nope")


@pytest.mark.asyncio
async def test_mail_tools_need_a_paired_thunderbird():
    server = StubServer()
    server.browsers["tb"].paired = False

    assert "not paired" in await call(server, "mail_list_accounts")
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_search_needs_something_to_search_for():
    server = StubServer()

    assert "Give text, author or subject" in await call(server, "mail_search_messages")
    assert server.sent_request is None


@pytest.mark.asyncio
async def test_search_sends_every_criterion():
    server = StubServer({"total": 0, "messages": []})
    await call(server, "mail_search_messages", author="chris", subject="Sync", folder_id="f1", unread_only=True)

    assert server.sent_request["action"] == "mail.search"
    assert server.sent_request["data"] == {"text": None, "author": "chris", "subject": "Sync", "folderId": "f1",
                                           "sinceDays": 90, "unreadOnly": True, "limit": 25}


@pytest.mark.asyncio
async def test_a_message_reads_with_headers_attachments_and_body():
    server = StubServer({
        "id": 7, "accountId": "account1", "folder": "/INBOX", "subject": "Sync", "author": "Chris",
        "date": "2026-10-19T15:00:00.000Z", "read": True, "flagged": False, "tags": [],
        "headers": {"list-unsubscribe": ["<mailto:u@example.org>"], "received": ["a", "b"]},
        "attachments": [{"name": "notes.pdf", "contentType": "application/pdf", "size": 1200}],
        "body": "Hello", "bodyTruncated": True,
    })
    result = await call(server, "mail_get_message", message_id=7)

    assert server.sent_request["data"] == {"messageId": 7, "headersOnly": False, "maxBodyChars": 20000}
    assert "  list-unsubscribe: <mailto:u@example.org>" in result
    assert "  received: a\n  received: b" in result
    assert "  notes.pdf (application/pdf, 1200 bytes)" in result
    assert "Body (cut short; raise max_body_chars for more):\nHello" in result


@pytest.mark.asyncio
async def test_headers_only_leaves_the_body_out():
    server = StubServer({"id": 7, "headers": {}, "attachments": []})
    result = await call(server, "mail_get_message", message_id=7, headers_only=True)

    assert "Body" not in result
