"""Focused tests for the defensive elicit confirmation on delete_note.

delete_note is the only irreversible tool (a deleted note_id cannot be
re-created). It now asks for confirmation via ctx.elicit() *when the client
supports it*, and must degrade gracefully everywhere else:

  - no Context (e.g. a client that never passes one) -> delete proceeds,
  - Context present but elicitation unsupported (ctx.elicit raises) -> delete
    proceeds (destructiveHint is the safety net),
  - user accepts -> delete proceeds,
  - user declines / cancels -> nothing is deleted, a clear cancelled
    WriteResult is returned (success=False, error set), NO opaque raise.

These exercise the tool's underlying coroutine directly with fake Context
objects and a patched applescript bridge, so they run without Apple Notes.
"""
from __future__ import annotations

import asyncio
import os

import pytest

# Keep server import side-effect-free on any CI host (see test_server_contract).
os.environ.setdefault("APPLE_NOTES_MCP_HOST", "127.0.0.1")
os.environ.setdefault("APPLE_NOTES_MCP_API_KEY", "")

from mcp_apple_notes import server  # noqa: E402
from mcp_apple_notes.server import WriteResult, tool_delete_note  # noqa: E402


class _Answer:
    """Stand-in for an ElicitResult with a controllable action."""

    def __init__(self, action: str):
        self.action = action


class _FakeCtx:
    """Minimal Context double.

    ``elicit_action`` controls the elicitation outcome:
      - "accept"/"decline"/"cancel" -> ctx.elicit returns an _Answer,
      - "unsupported" -> ctx.elicit raises (client has no elicitation handler).
    """

    def __init__(self, elicit_action: str):
        self._elicit_action = elicit_action
        self.elicit_calls = 0
        self.info_messages: list[str] = []

    async def elicit(self, message, response_type=str):
        self.elicit_calls += 1
        if self._elicit_action == "unsupported":
            raise RuntimeError("Client does not support elicitation")
        return _Answer(self._elicit_action)

    async def info(self, message):
        self.info_messages.append(message)


@pytest.fixture
def captured_delete(monkeypatch):
    """Patch the bridge delete_note bound in server; record invocations."""
    calls: list[int] = []

    def _fake_delete(note_id: int) -> dict:
        calls.append(note_id)
        return {"success": True, "note_id": note_id}

    monkeypatch.setattr(server, "delete_note", _fake_delete)
    return calls


def test_delete_proceeds_without_context(captured_delete):
    """No Context at all -> delete runs (cannot elicit, must not break)."""
    result = asyncio.run(tool_delete_note(note_id=7, ctx=None))
    assert isinstance(result, WriteResult)
    assert result.success is True
    assert result.note_id == 7
    assert captured_delete == [7]


def test_delete_proceeds_when_elicitation_unsupported(captured_delete):
    """ctx.elicit raises (unsupported) -> delete proceeds; never raises out."""
    ctx = _FakeCtx("unsupported")
    result = asyncio.run(tool_delete_note(note_id=11, ctx=ctx))
    assert ctx.elicit_calls == 1
    assert result.success is True
    assert result.note_id == 11
    assert captured_delete == [11]


def test_delete_proceeds_on_accept(captured_delete):
    """User confirms -> delete proceeds."""
    ctx = _FakeCtx("accept")
    result = asyncio.run(tool_delete_note(note_id=21, ctx=ctx))
    assert ctx.elicit_calls == 1
    assert result.success is True
    assert result.note_id == 21
    assert captured_delete == [21]


def test_delete_declined_does_not_delete(captured_delete):
    """User declines -> nothing deleted, clear cancelled result, no raise."""
    ctx = _FakeCtx("decline")
    result = asyncio.run(tool_delete_note(note_id=31, ctx=ctx))
    assert ctx.elicit_calls == 1
    assert result.success is False
    assert result.note_id == 31
    assert result.error and "cancel" in result.error.lower()
    assert captured_delete == []  # the irreversible op never ran


def test_delete_cancelled_does_not_delete(captured_delete):
    """User cancels the whole operation -> nothing deleted, no raise."""
    ctx = _FakeCtx("cancel")
    result = asyncio.run(tool_delete_note(note_id=41, ctx=ctx))
    assert ctx.elicit_calls == 1
    assert result.success is False
    assert result.note_id == 41
    assert result.error
    assert captured_delete == []


def test_write_result_validates_error_payload():
    """The output_schema model accepts the cancelled/error shape too."""
    wr = WriteResult(success=False, note_id=99, error="Delete cancelled by user.")
    dumped = wr.model_dump()
    assert dumped["success"] is False
    assert dumped["note_id"] == 99
    assert dumped["error"] == "Delete cancelled by user."

    # And a bare {"error": ...} payload must validate without raising.
    bare = WriteResult(success=False, error="boom")
    assert bare.note_id is None
    assert bare.error == "boom"
