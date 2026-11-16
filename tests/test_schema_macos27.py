"""macOS 27 dropped ZISHIDDENNOTECONTAINER from NoteStore; folder listing must still work."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_apple_notes.notestore import NoteStoreReader
from tests.test_note_url import _make_notestore_with_note


def test_list_folders_and_stats_without_hidden_container_column(tmp_path: Path) -> None:
    db = tmp_path / "NoteStore.sqlite"
    _make_notestore_with_note(db, title="Rijstnoedels", identifier="UUID-1")
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE ZICCLOUDSYNCINGOBJECT DROP COLUMN ZISHIDDENNOTECONTAINER")
    conn.commit()
    conn.close()
    reader = NoteStoreReader(db)
    assert [f["name"] for f in reader.list_folders()] == ["Recipes"]
    assert reader.get_stats()["total_folders"] == 1
