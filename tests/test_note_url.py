"""Regression tests for Bug 2: create_note / create_recipe_note must return
a proper applenotes://showNote?identifier=<UUID> URL.

These tests mock AppleScript execution and the NoteStore lookup so they
run without a live Apple Notes installation.
"""
from __future__ import annotations

import sqlite3
import gzip
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mcp_apple_notes.notestore import NoteStoreReader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENT_NOTE = 12
_ENT_FOLDER = 15
_FAKE_UUID = "A1B2C3D4-E5F6-7890-ABCD-EF1234567890"


def _make_notestore_with_note(path: Path, title: str, identifier: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK        INTEGER PRIMARY KEY,
            Z_ENT       INTEGER,
            ZTITLE1     TEXT,
            ZTITLE2     TEXT,
            ZSNIPPET    TEXT,
            ZFOLDER     INTEGER,
            ZPARENT     INTEGER,
            ZCREATIONDATE3   REAL,
            ZMODIFICATIONDATE1 REAL,
            ZISPINNED   INTEGER DEFAULT 0,
            ZHASCHECKLIST INTEGER DEFAULT 0,
            ZIDENTIFIER TEXT,
            ZFOLDERTYPE INTEGER,
            ZISHIDDENNOTECONTAINER INTEGER DEFAULT 0,
            ZMARKEDFORDELETION INTEGER DEFAULT 0,
            ZTOKENCONTENTIDENTIFIER TEXT,
            ZTYPEUTI1   TEXT,
            ZALTTEXT    TEXT,
            ZDISPLAYTEXT TEXT,
            ZNOTE1      INTEGER
        );
        CREATE TABLE ZICNOTEDATA (
            Z_PK  INTEGER PRIMARY KEY,
            ZNOTE INTEGER,
            ZDATA BLOB
        );
        """
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT "
        "(Z_PK, Z_ENT, ZTITLE2, ZIDENTIFIER, ZFOLDERTYPE, ZISHIDDENNOTECONTAINER, ZMARKEDFORDELETION) "
        "VALUES (1, ?, 'Recipes', 'FOLDER-UUID', NULL, 0, 0)",
        (_ENT_FOLDER,),
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT "
        "(Z_PK, Z_ENT, ZTITLE1, ZFOLDER, ZCREATIONDATE3, ZMODIFICATIONDATE1, "
        "ZIDENTIFIER, ZMARKEDFORDELETION) "
        "VALUES (42, ?, ?, 1, 9999.0, 9999.0, ?, 0)",
        (_ENT_NOTE, title, identifier),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# NoteStoreReader.find_note_identifier_by_title
# ---------------------------------------------------------------------------


def test_find_note_identifier_returns_uuid(tmp_path: Path) -> None:
    """find_note_identifier_by_title must return the ZIDENTIFIER string."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Rijstnoedels", identifier=_FAKE_UUID)
    reader = NoteStoreReader(db)

    result = reader.find_note_identifier_by_title("Rijstnoedels")
    assert result == _FAKE_UUID


def test_find_note_identifier_returns_none_when_missing(tmp_path: Path) -> None:
    """Returns None when the title doesn't exist."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Some other note", identifier=_FAKE_UUID)
    reader = NoteStoreReader(db)

    result = reader.find_note_identifier_by_title("Rijstnoedels")
    assert result is None


def test_find_note_identifier_with_folder_filter(tmp_path: Path) -> None:
    """Folder filter narrows the lookup correctly."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Rijstnoedels", identifier=_FAKE_UUID)
    reader = NoteStoreReader(db)

    # Correct folder → found
    assert reader.find_note_identifier_by_title("Rijstnoedels", folder="Recipes") == _FAKE_UUID
    # Wrong folder → not found
    assert reader.find_note_identifier_by_title("Rijstnoedels", folder="Notes") is None


# ---------------------------------------------------------------------------
# _build_note_url (unit test via mock)
# ---------------------------------------------------------------------------


def test_build_note_url_returns_deep_link(tmp_path: Path) -> None:
    """_build_note_url must return applenotes://showNote?identifier=<UUID>."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="My Recipe", identifier=_FAKE_UUID)

    from mcp_apple_notes import applescript_bridge

    fake_settings = MagicMock()
    fake_settings.db_path_resolved = db

    # get_settings is imported inside the function body, so we patch at the config module
    with patch("mcp_apple_notes.config.get_settings", return_value=fake_settings):
        url = applescript_bridge._build_note_url(title="My Recipe", folder="Recipes")

    assert url == f"applenotes://showNote?identifier={_FAKE_UUID}", (
        f"Expected applenotes://showNote?identifier=..., got: {url!r}"
    )


def test_build_note_url_falls_back_gracefully(tmp_path: Path) -> None:
    """When the note can't be found, _build_note_url returns bare applenotes://."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Other Note", identifier=_FAKE_UUID)

    from mcp_apple_notes import applescript_bridge

    fake_settings = MagicMock()
    fake_settings.db_path_resolved = db

    # Use a very short retry by mocking time so the test doesn't wait 3 seconds
    with patch("mcp_apple_notes.config.get_settings", return_value=fake_settings):
        with patch("mcp_apple_notes.applescript_bridge.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 4.0]  # deadline exceeded immediately
            mock_time.sleep = MagicMock()
            url = applescript_bridge._build_note_url(title="Nonexistent Recipe", folder="Recipes")

    assert url == "applenotes://", f"Expected bare fallback URL, got: {url!r}"


# ---------------------------------------------------------------------------
# create_note integration (mock AppleScript, real NoteStore lookup)
# ---------------------------------------------------------------------------


def test_create_note_url_shape(tmp_path: Path) -> None:
    """Bug 2 regression: create_note must include url with ?identifier= in the result."""
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Test Recipe", identifier=_FAKE_UUID)

    # Mock AppleScript to return a fake CoreData URI
    fake_applescript_result = "x-coredata://STORE-UUID/ICNote/p42"

    from mcp_apple_notes import applescript_bridge

    fake_settings = MagicMock()
    fake_settings.db_path_resolved = db

    with (
        patch.object(applescript_bridge, "run_applescript", return_value=fake_applescript_result),
        patch("mcp_apple_notes.config.get_settings", return_value=fake_settings),
    ):
        result = applescript_bridge.create_note(
            title="Test Recipe", body="<p>ingredients</p>", folder="Recipes"
        )

    assert result["success"] is True
    assert "url" in result, "create_note result must contain a 'url' key"
    url = result["url"]
    assert "?identifier=" in url, (
        f"URL must contain '?identifier=' — got: {url!r}"
    )
    assert url == f"applenotes://showNote?identifier={_FAKE_UUID}"
