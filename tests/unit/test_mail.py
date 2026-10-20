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
