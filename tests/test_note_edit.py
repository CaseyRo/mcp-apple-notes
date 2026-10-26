"""update_note / append_to_note (CDI-1918), with AppleScript and NoteStore mocked.

Every edit is a whole-body AppleScript rewrite, so these pin the safety gate:
a missing, locked or content-losing note is refused with a clear ToolError and
nothing is written. Runs on Linux; the one osascript check is darwin-only and
never talks to Notes.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

# Same guard as test_server_contract: never let import hit the non-loopback exit.
os.environ.setdefault("APPLE_NOTES_MCP_HOST", "127.0.0.1")
os.environ.setdefault("APPLE_NOTES_MCP_API_KEY", "")

import pytest  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from mcp_apple_notes import applescript_bridge as bridge  # noqa: E402
from mcp_apple_notes import server  # noqa: E402
from mcp_apple_notes.notestore import NoteStoreReader  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def osa(monkeypatch):
    """Record the AppleScript instead of running it; a test sets ``osa.reply``."""
    calls = SimpleNamespace(scripts=[], reply="ok Shopping")

    def fake_run(script):
        calls.scripts.append(script)
        return calls.reply

    monkeypatch.setattr(bridge, "run_applescript", fake_run)
    monkeypatch.setattr(bridge, "_store_id_cache", "STORE")
    monkeypatch.setattr(NoteStoreReader, "rewrite_losses", lambda self, note_id: [])
    return calls


# --- bridge: the AppleScript that gets written -------------------------------


def test_update_keeps_the_title_and_escapes_the_body(osa):
    result = bridge.update_note(7, 'say "hi" \\ <b>x</b>')

    assert result == {"success": True, "note_id": 7, "title": "Shopping"}
    script = osa.scripts[0]
    assert 'note id "x-coredata://STORE/ICNote/p7"' in script
    assert (
        'set body of theNote to "<h1>" & (name of theNote) & "</h1><br>'
        'say \\"hi\\" \\\\ <b>x</b>"'
    ) in script


def test_append_adds_to_the_existing_body_after_the_gates(osa):
    bridge.append_to_note(7, "Milk")

    script = osa.scripts[0]
    write = script.index('set body of theNote to oldBody & "<div>Milk</div>"')
    for gate in ('return "locked"', "count of attachments", 'oldBody contains "<a "', 'return "markup"'):
        assert script.index(gate) < write, f"{gate} must run before the write"


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ("not_found", "Note 7 not found"),
        ("locked", "locked"),
        ("attachments 2", "2 attachment"),
        ("markup", "links or headings"),
    ],
)
@pytest.mark.parametrize("edit", [bridge.update_note, bridge.append_to_note])
def test_refusals_name_the_reason(osa, edit, reply, message):
    osa.reply = reply
    with pytest.raises(ValueError, match=message):
        edit(7, "x")


def test_osascript_failure_is_not_a_refusal(osa):
    osa.reply = False
    with pytest.raises(RuntimeError, match="osascript failed"):
        bridge.update_note(7, "x")


def test_notestore_losses_refuse_before_any_applescript(osa, monkeypatch):
    monkeypatch.setattr(NoteStoreReader, "rewrite_losses", lambda self, note_id: ["a checklist"])
    with pytest.raises(ValueError, match="checklist"):
        bridge.append_to_note(7, "x")
    assert osa.scripts == []


@pytest.mark.skipif(sys.platform != "darwin", reason="needs osascript")
def test_escaped_html_parses_as_an_applescript_literal():
    html = '<span style="color: red">a \\ b</span>'
    literal = bridge.escape_applescript_string(html)
    out = subprocess.run(
        ["osascript", "-e", f'return "{literal}"'], capture_output=True, text=True
    )
    assert out.stdout.strip() == html


# --- notestore: what AppleScript cannot see ----------------------------------


def test_rewrite_losses_reads_checklists_and_tags(tmp_path):
    db = tmp_path / "NoteStore.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, "
        "ZHASCHECKLIST INTEGER DEFAULT 0, ZTYPEUTI1 TEXT, ZALTTEXT TEXT, ZNOTE1 INTEGER)"
    )
    tag = NoteStoreReader._ENT_INLINE_ATTACHMENT
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 12, 0, None, None, None),
            (2, 12, 1, None, None, None),
            (3, 12, 0, None, None, None),
            (4, tag, 0, NoteStoreReader._HASHTAG_UTI, "#rezept", 3),
        ],
    )
    conn.commit()
    conn.close()
    reader = NoteStoreReader(db)

    assert reader.rewrite_losses(1) == []
    assert reader.rewrite_losses(2) == ["a checklist"]
    assert reader.rewrite_losses(3) == ["tags (#rezept)"]
    # Not flushed yet (or gone): AppleScript decides, so no false "not found".
    assert reader.rewrite_losses(99) == []


# --- server: through the in-memory client ------------------------------------


@pytest.mark.anyio
async def test_edit_annotations_survive_the_wire():
    async with Client(server.mcp) as client:
        tools = {t.name: t.annotations for t in await client.list_tools()}
    update, append = tools["update_note"], tools["append_to_note"]
    assert update.read_only_hint is False and append.read_only_hint is False
    assert update.destructive_hint is False and append.destructive_hint is False
    assert update.idempotent_hint is True
    assert append.idempotent_hint is False


@pytest.mark.anyio
async def test_update_round_trips(monkeypatch):
    seen = []

    def fake_update(note_id, body):
        seen.append((note_id, body))
        return {"success": True, "note_id": note_id, "title": "Shopping"}

    monkeypatch.setattr(server, "update_note", fake_update)
    async with Client(server.mcp) as client:
        result = await client.call_tool("update_note", {"note_id": 7, "body": "<b>new</b>"})
    assert seen == [(7, "<b>new</b>")]
    assert result.structured_content["success"] is True
    assert result.structured_content["title"] == "Shopping"


@pytest.mark.anyio
async def test_a_refusal_is_a_tool_error(monkeypatch):
    def refuse(note_id, text):
        raise ValueError(f"Note {note_id} is locked (password protected) and was not changed.")

    monkeypatch.setattr(server, "append_to_note", refuse)
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="locked"):
            await client.call_tool("append_to_note", {"note_id": 7, "text": "x"})


@pytest.mark.anyio
async def test_append_rejects_empty_text(monkeypatch):
    monkeypatch.setattr(server, "append_to_note", lambda *a: pytest.fail("bridge was called"))
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("append_to_note", {"note_id": 7, "text": ""})
