"""The MCP surface over a real in-memory client (fastmcp.Client(mcp)).

test_server_contract introspects ``server.mcp`` directly; these go through the
client the way a caller does, so a framework upgrade that stops the server
importing, drops a tool, loses annotations on the wire, or breaks dispatch
shows up here. Nothing touches NoteStore or AppleScript, so they run on Linux.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

# Same guard as test_server_contract: never let import hit the non-loopback exit.
os.environ.setdefault("APPLE_NOTES_MCP_HOST", "127.0.0.1")
os.environ.setdefault("APPLE_NOTES_MCP_API_KEY", "")

import pytest  # noqa: E402
from fastmcp import Client  # noqa: E402

from mcp_apple_notes import server  # noqa: E402

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "create_note",
    "list_folders",
    "list_notes",
    "get_note",
    "search_notes",
    "delete_note",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_server_registers_its_tools():
    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


async def test_read_only_annotations_survive_the_wire():
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    ann = tools["list_folders"].annotations
    assert ann is not None
    assert ann.read_only_hint is True
    assert ann.open_world_hint is False


async def test_a_tool_call_round_trips(monkeypatch):
    # list_folders reads through the module-level NoteStore reader; swap it so
    # the call never opens the real NoteStore.sqlite.
    folder = {"folder_id": 1, "name": "Inbox", "path": "Inbox", "note_count": 3}
    monkeypatch.setattr(server, "_reader", SimpleNamespace(list_folders=lambda: [folder]))

    async with Client(server.mcp) as client:
        result = await client.call_tool("list_folders", {})
    assert not result.is_error
    assert result.structured_content["folders"][0]["name"] == "Inbox"


async def test_a_tool_call_writes_one_usage_line(monkeypatch, capsys):
    import json

    monkeypatch.setattr(server, "_reader", SimpleNamespace(list_folders=lambda: []))
    capsys.readouterr()
    async with Client(server.mcp) as client:
        await client.call_tool("list_folders", {})
    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if '"mcp_usage"' in line
    ]
    assert len(lines) == 1
    assert lines[0]["server"] == "apple-notes"
    assert lines[0]["tool"] == "list_folders"
    assert lines[0]["outcome"] == "ok"


async def test_delete_proceeds_when_client_cannot_elicit(monkeypatch):
    """No elicitation handler on the client: ctx.elicit fails fast, delete proceeds."""
    import time

    deleted: list[int] = []
    monkeypatch.setattr(
        server, "delete_note", lambda note_id: deleted.append(note_id) or {"success": True, "note_id": note_id}
    )
    start = time.perf_counter()
    async with Client(server.mcp) as client:
        result = await client.call_tool("delete_note", {"note_id": 5})
    assert deleted == [5]
    assert result.structured_content["success"] is True
    assert time.perf_counter() - start < server._ELICIT_TIMEOUT_S
