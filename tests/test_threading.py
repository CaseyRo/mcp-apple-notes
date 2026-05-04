"""Regression tests for Bug 1: SQLite threading error in NoteStoreReader.

Verifies that concurrent calls to search_notes from different threads do not
raise ``ProgrammingError: SQLite objects created in a thread can only be used
in that same thread``.
"""
from __future__ import annotations

import gzip
import sqlite3
import struct
import tempfile
import threading
from pathlib import Path

import pytest

from mcp_apple_notes.notestore import NoteStoreReader


# ---------------------------------------------------------------------------
# Helpers to build a minimal NoteStore.sqlite fixture
# ---------------------------------------------------------------------------

_ENT_NOTE = 12
_ENT_FOLDER = 15


def _make_minimal_notestore(path: Path) -> None:
    """Create a NoteStore.sqlite with the schema and a couple of dummy notes."""
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
    # Insert a dummy folder
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT "
        "(Z_PK, Z_ENT, ZTITLE2, ZIDENTIFIER, ZFOLDERTYPE, ZISHIDDENNOTECONTAINER, ZMARKEDFORDELETION) "
        "VALUES (1, ?, 'Notes', 'FOLDER-UUID', NULL, 0, 0)",
        (_ENT_FOLDER,),
    )
    # Insert two dummy notes
    for pk, title in [(10, "Rijstnoedels recept"), (11, "Pasta recept")]:
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT "
            "(Z_PK, Z_ENT, ZTITLE1, ZFOLDER, ZCREATIONDATE3, ZMODIFICATIONDATE1, "
            "ZIDENTIFIER, ZMARKEDFORDELETION) "
            "VALUES (?, ?, ?, 1, 1000.0, 1001.0, ?, 0)",
            (pk, _ENT_NOTE, title, f"NOTE-UUID-{pk}"),
        )
        # Build a minimal gzip-compressed payload with recognizable text so FTS
        # indexing has something to decompress.
        raw = title.encode("utf-8")
        compressed = gzip.compress(raw)
        conn.execute(
            "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)",
            (pk, compressed),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_notes_from_same_thread(tmp_path: Path) -> None:
    """Baseline: search_notes works at all with a valid DB."""
    db = tmp_path / "NoteStore.sqlite"
    _make_minimal_notestore(db)
    reader = NoteStoreReader(db)

    results = reader.search_notes("Rijstnoedels")
    assert isinstance(results, list)


def test_search_notes_concurrent_threads_no_threading_error(tmp_path: Path) -> None:
    """Bug 1 regression: calling search_notes from multiple threads must not raise
    ``ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread``.

    The fix uses ``threading.local()`` so each thread gets its own FTS
    connection.  Before the fix, the second-thread call would reliably fail.
    """
    db = tmp_path / "NoteStore.sqlite"
    _make_minimal_notestore(db)
    reader = NoteStoreReader(db)

    errors: list[Exception] = []
    results_per_thread: dict[int, list] = {}

    def _search(thread_idx: int) -> None:
        try:
            r = reader.search_notes("recept", limit=10)
            results_per_thread[thread_idx] = r
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_search, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"
    # Every thread should have produced a list (possibly empty)
    assert len(results_per_thread) == 5
    for idx, r in results_per_thread.items():
        assert isinstance(r, list), f"Thread {idx} got non-list result: {r!r}"


def test_fts_conn_is_thread_local(tmp_path: Path) -> None:
    """Each thread must get its own FTS connection object (not a shared one)."""
    db = tmp_path / "NoteStore.sqlite"
    _make_minimal_notestore(db)
    reader = NoteStoreReader(db)

    conn_ids: dict[int, int] = {}
    barrier = threading.Barrier(3)

    def _grab_conn(idx: int) -> None:
        barrier.wait()  # start all threads simultaneously
        conn = reader._ensure_fts_index()
        conn_ids[idx] = id(conn)

    threads = [threading.Thread(target=_grab_conn, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # All threads must have a connection
    assert len(conn_ids) == 3
    # Each thread must have a *different* connection object
    assert len(set(conn_ids.values())) == 3, (
        "Expected each thread to hold a distinct FTS connection, "
        f"but got ids: {conn_ids}"
    )
