"""Contract tests for the fastmcp uplift.

Verify the additive changes hold the backward-compatibility line:
  - every existing tool name + parameter set is preserved (no ctx leak),
  - every tool carries annotations + an output schema,
  - read tools are readOnlyHint, delete_note is destructiveHint,
  - the new resources and prompts are registered,
  - the structured-output models accept the reader's dict shapes.

These run without a live Apple Notes install — they only introspect the
already-built ``server.mcp`` object and exercise the Pydantic models.
"""
from __future__ import annotations

import asyncio
import os

# The server module refuses to start unauthenticated on a non-loopback host;
# force loopback so importing it never calls sys.exit(1) under any CI env.
os.environ.setdefault("APPLE_NOTES_MCP_HOST", "127.0.0.1")
os.environ.setdefault("APPLE_NOTES_MCP_API_KEY", "")

from mcp_apple_notes import server  # noqa: E402
from mcp_apple_notes.server import (  # noqa: E402
    FolderList,
    NoteDetail,
    NoteList,
    SearchResult,
    StatsResult,
    TagList,
    TagSearchResult,
    WriteResult,
)

# Frozen public contract — the names + params the Cloudflare portal and live
# clients depend on. If this dict needs to change, it is a breaking change.
_EXPECTED_PARAMS = {
    "create_note": {"title", "body", "folder"},
    "create_recipe_note": {"title", "body_html", "image_url", "video_url"},
    "list_folders": set(),
    "list_notes": {"folder", "limit", "offset", "sort_by"},
    "list_tags": set(),
    "search_by_tag": {"tag", "limit"},
    "get_note": {"note_id"},
    "search_notes": {"query", "limit"},
    "get_stats": set(),
    "move_note": {"note_id", "folder"},
    "delete_note": {"note_id"},
}

_READ_TOOLS = {
    "list_folders",
    "list_notes",
    "list_tags",
    "search_by_tag",
    "get_note",
    "search_notes",
    "get_stats",
}


def _tools_by_name() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_all_tool_names_preserved() -> None:
    tools = _tools_by_name()
    assert set(tools) == set(_EXPECTED_PARAMS), (
        "Tool set changed — additive uplift must not add/remove client-facing tools"
    )


def test_tool_params_unchanged_and_ctx_hidden() -> None:
    tools = _tools_by_name()
    for name, expected in _EXPECTED_PARAMS.items():
        props = set(tools[name].parameters.get("properties", {}).keys())
        assert props == expected, f"{name} params changed: {props} != {expected}"
        assert "ctx" not in props, f"{name} leaks the Context param to clients"


def test_every_tool_has_annotations_and_output_schema() -> None:
    tools = _tools_by_name()
    for name, tool in tools.items():
        assert tool.annotations is not None, f"{name} missing annotations"
        assert tool.annotations.title, f"{name} missing human title"
        assert tool.output_schema is not None, f"{name} missing output schema"


def test_read_tools_are_readonly() -> None:
    tools = _tools_by_name()
    for name in _READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True, f"{name} not readOnly"


def test_delete_is_destructive_and_idempotent() -> None:
    tools = _tools_by_name()
    delete = tools["delete_note"].annotations
    assert delete.destructiveHint is True
    assert delete.idempotentHint is True
    move = tools["move_note"].annotations
    assert move.destructiveHint is False
    assert move.idempotentHint is True


def test_resources_registered() -> None:
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    assert {"notes://stats", "notes://folders", "notes://tags"} <= uris


def test_prompts_registered() -> None:
    names = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert {"capture_recipe", "triage_notes"} <= names


# --- structured output models accept the reader's dict shapes --------------


def test_models_accept_reader_dicts() -> None:
    folder = {"folder_id": 1, "name": "Notes", "path": "Notes", "note_count": 3}
    FolderList(folders=[folder])

    note = {
        "note_id": 10,
        "identifier": "UUID-10",
        "title": "Hi",
        "snippet": "snip",
        "folder": "Notes",
        "created": None,
        "modified": None,
        "is_pinned": False,
        "has_checklist": False,
    }
    nl = NoteList(notes=[note], total=1, limit=50, offset=0)
    assert nl.total == 1

    detail = {**note, "body": "body text", "body_source": "protobuf", "tags": ["#x"]}
    nd = NoteDetail(**detail)
    assert nd.body == "body text"

    TagList(tags=[{"tag": "#x", "identifier": "T", "note_count": 2}], total=1)
    TagSearchResult(results=[note], tag="#x", total=1)

    hit = {**note, "rank": -1.23}
    sr = SearchResult(results=[hit], query="x")
    assert sr.results[0].rank == -1.23

    StatsResult(
        total_notes=1,
        total_folders=1,
        pinned_notes=0,
        notes_with_checklists=0,
        notes_per_folder=[{"folder": "Notes", "count": 1}],
        oldest_note=None,
        newest_modification=None,
    )


def test_write_result_preserves_envelope() -> None:
    # The historical create_note return shape must round-trip unchanged.
    wr = WriteResult(
        success=True,
        note_id=42,
        title="Test",
        folder="Recipes",
        url="applenotes://showNote?identifier=ABC",
    )
    dumped = wr.model_dump()
    assert dumped["success"] is True
    assert dumped["note_id"] == 42
    assert dumped["url"].startswith("applenotes://")
