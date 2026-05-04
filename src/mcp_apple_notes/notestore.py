#!/usr/bin/env python3
"""Read-only access to Apple Notes via NoteStore.sqlite.

Provides folder listing, note listing/reading, full-text search,
and aggregate statistics — all without AppleScript overhead.
"""

import gzip
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Core Data epoch offset: 2001-01-01 00:00:00 UTC in Unix time
_COREDATA_EPOCH_OFFSET = 978307200

# Entity type constants (Z_ENT values in ZICCLOUDSYNCINGOBJECT)
_ENT_NOTE = 12
_ENT_FOLDER = 15
_ENT_ACCOUNT = 14
_ENT_ATTACHMENT = 5

# Maximum blob size to process for FTS indexing (10 MB)
_MAX_BLOB_SIZE = 10 * 1024 * 1024

# UUID pattern for filtering protobuf noise
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class NoteStoreReader:
    """Read-only access to Apple Notes via NoteStore.sqlite."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        # Per-thread FTS connection cache — each thread needs its own sqlite3.Connection
        # because SQLite objects created on one thread cannot be used on another (the
        # default check_same_thread=True raises ProgrammingError across thread-pool hops).
        self._fts_local = threading.local()
        self._fts_mtime: float = 0.0
        self._fts_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection to NoteStore.sqlite."""
        if not self._db_path.exists():
            logger.error("NoteStore.sqlite not found at %s", self._db_path)
            raise FileNotFoundError(
                "NoteStore database not found. "
                "Ensure Apple Notes is set up and Full Disk Access is granted."
            )
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _coredata_to_iso(timestamp: float | None) -> str | None:
        """Convert a Core Data timestamp to an ISO 8601 string."""
        if timestamp is None:
            return None
        try:
            dt = datetime.fromtimestamp(
                timestamp + _COREDATA_EPOCH_OFFSET, tz=timezone.utc
            )
            return dt.isoformat()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _extract_text_from_protobuf(data: bytes) -> str:
        """Gzip-decompress a protobuf blob and extract readable text."""
        if not data or len(data) > _MAX_BLOB_SIZE:
            return ""
        try:
            decompressed = gzip.decompress(data)
        except Exception:
            return ""

        # Extract runs of printable UTF-8 characters (5+ bytes)
        fragments = re.findall(
            rb"[\x20-\x7e\xc0-\xff][\x80-\xbf\x20-\x7e]{4,}", decompressed
        )
        texts = []
        for frag in fragments:
            try:
                t = frag.decode("utf-8", errors="ignore").strip()
            except UnicodeDecodeError:
                continue
            # Filter out UUIDs and hex-only strings
            if not t or _UUID_RE.match(t):
                continue
            if len(t) > 3 and not all(c in "0123456789abcdefABCDEF" for c in t):
                texts.append(t)
        return "\n".join(texts)

    # ------------------------------------------------------------------
    # Folder queries
    # ------------------------------------------------------------------

    def list_folders(self) -> list[dict]:
        """List all folders with note counts and nesting info."""
        conn = self._connect()
        try:
            # Get all folders
            folders_raw = conn.execute(
                """
                SELECT Z_PK, ZTITLE2, ZPARENT, ZIDENTIFIER
                FROM ZICCLOUDSYNCINGOBJECT
                WHERE Z_ENT = ?
                  AND ZMARKEDFORDELETION = 0
                  AND ZISHIDDENNOTECONTAINER != 1
                  AND (ZFOLDERTYPE IS NULL OR ZFOLDERTYPE != 1)
                ORDER BY ZTITLE2
                """,
                (_ENT_FOLDER,),
            ).fetchall()

            # Count notes per folder
            counts = {}
            for row in conn.execute(
                """
                SELECT ZFOLDER, COUNT(*) as cnt
                FROM ZICCLOUDSYNCINGOBJECT
                WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0
                GROUP BY ZFOLDER
                """,
                (_ENT_NOTE,),
            ):
                counts[row["ZFOLDER"]] = row["cnt"]

            # Build parent lookup for nested paths
            pk_to_name = {r["Z_PK"]: r["ZTITLE2"] or "" for r in folders_raw}

            def _build_path(folder_row: sqlite3.Row) -> str:
                parts = [folder_row["ZTITLE2"] or ""]
                parent = folder_row["ZPARENT"]
                seen = set()
                while parent and parent in pk_to_name and parent not in seen:
                    seen.add(parent)
                    parts.insert(0, pk_to_name[parent])
                    # Walk up — need to re-query parent's parent
                    p_row = conn.execute(
                        "SELECT ZPARENT FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ?",
                        (parent,),
                    ).fetchone()
                    parent = p_row["ZPARENT"] if p_row else None
                return "/".join(parts)

            results = []
            for f in folders_raw:
                results.append(
                    {
                        "folder_id": f["Z_PK"],
                        "name": f["ZTITLE2"] or "",
                        "path": _build_path(f),
                        "note_count": counts.get(f["Z_PK"], 0),
                        "identifier": f["ZIDENTIFIER"],
                    }
                )
            return results
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Note listing
    # ------------------------------------------------------------------

    def list_notes(
        self,
        folder: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "modified",
    ) -> dict:
        """List notes with pagination."""
        conn = self._connect()
        try:
            sort_col = {
                "modified": "n.ZMODIFICATIONDATE1",
                "created": "n.ZCREATIONDATE3",
                "title": "n.ZTITLE1",
            }.get(sort_by, "n.ZMODIFICATIONDATE1")

            sort_dir = "ASC" if sort_by == "title" else "DESC"

            where_clauses = [
                "n.Z_ENT = ?",
                "n.ZMARKEDFORDELETION = 0",
                "(f.ZFOLDERTYPE IS NULL OR f.ZFOLDERTYPE != 1)",
            ]
            params: list = [_ENT_NOTE]

            if folder:
                where_clauses.append("f.ZTITLE2 = ?")
                params.append(folder)

            where_sql = " AND ".join(where_clauses)

            # Total count
            total = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                WHERE {where_sql}
                """,
                params,
            ).fetchone()[0]

            # Paginated results — pinned first, then by sort column
            rows = conn.execute(
                f"""
                SELECT
                    n.Z_PK,
                    n.ZTITLE1,
                    n.ZSNIPPET,
                    n.ZFOLDER,
                    f.ZTITLE2 as folder_name,
                    n.ZCREATIONDATE3,
                    n.ZMODIFICATIONDATE1,
                    n.ZISPINNED,
                    n.ZHASCHECKLIST,
                    n.ZIDENTIFIER
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                WHERE {where_sql}
                ORDER BY n.ZISPINNED DESC, {sort_col} {sort_dir}
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()

            # Fetch snippets from protobuf for notes missing ZSNIPPET
            notes = []
            for r in rows:
                snippet = r["ZSNIPPET"] or ""
                if not snippet:
                    blob_row = conn.execute(
                        "SELECT ZDATA FROM ZICNOTEDATA WHERE ZNOTE = ?",
                        (r["Z_PK"],),
                    ).fetchone()
                    if blob_row and blob_row["ZDATA"]:
                        text = self._extract_text_from_protobuf(blob_row["ZDATA"])
                        snippet = text[:200] if text else ""

                notes.append(
                    {
                        "note_id": r["Z_PK"],
                        "identifier": r["ZIDENTIFIER"],
                        "title": r["ZTITLE1"] or "",
                        "snippet": snippet,
                        "folder": r["folder_name"] or "",
                        "created": self._coredata_to_iso(r["ZCREATIONDATE3"]),
                        "modified": self._coredata_to_iso(r["ZMODIFICATIONDATE1"]),
                        "is_pinned": bool(r["ZISPINNED"]),
                        "has_checklist": bool(r["ZHASCHECKLIST"]),
                    }
                )

            return {
                "notes": notes,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    _ENT_HASHTAG = 8
    _ENT_INLINE_ATTACHMENT = 9
    _HASHTAG_UTI = "com.apple.notes.inlinetextattachment.hashtag"

    def list_tags(self) -> list[dict]:
        """List all hashtags with usage counts."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    h.ZDISPLAYTEXT as tag,
                    h.ZIDENTIFIER,
                    COUNT(ia.Z_PK) as note_count
                FROM ZICCLOUDSYNCINGOBJECT h
                LEFT JOIN ZICCLOUDSYNCINGOBJECT ia
                    ON ia.ZTOKENCONTENTIDENTIFIER = UPPER(h.ZDISPLAYTEXT)
                    AND ia.Z_ENT = {self._ENT_INLINE_ATTACHMENT}
                    AND ia.ZTYPEUTI1 = '{self._HASHTAG_UTI}'
                WHERE h.Z_ENT = ?
                  AND h.ZDISPLAYTEXT IS NOT NULL
                  AND h.ZDISPLAYTEXT != ''
                GROUP BY h.Z_PK
                ORDER BY h.ZDISPLAYTEXT
                """,
                (self._ENT_HASHTAG,),
            ).fetchall()
            return [
                {
                    "tag": r["tag"],
                    "identifier": r["ZIDENTIFIER"],
                    "note_count": r["note_count"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_note_tags(self, note_id: int) -> list[str]:
        """Get all hashtags attached to a specific note."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT ZALTTEXT
                FROM ZICCLOUDSYNCINGOBJECT
                WHERE Z_ENT = {self._ENT_INLINE_ATTACHMENT}
                  AND ZTYPEUTI1 = '{self._HASHTAG_UTI}'
                  AND ZNOTE1 = ?
                ORDER BY ZALTTEXT
                """,
                (note_id,),
            ).fetchall()
            return [r["ZALTTEXT"] for r in rows]
        finally:
            conn.close()

    def search_by_tag(self, tag: str, limit: int = 50) -> list[dict]:
        """Find all notes with a specific hashtag."""
        conn = self._connect()
        try:
            # Normalize: strip leading # if present, uppercase for token match
            tag_clean = tag.lstrip("#")
            token = tag_clean.upper()

            rows = conn.execute(
                f"""
                SELECT
                    n.Z_PK as note_id,
                    n.ZTITLE1 as title,
                    n.ZIDENTIFIER as identifier,
                    n.ZSNIPPET as snippet,
                    f.ZTITLE2 as folder,
                    n.ZCREATIONDATE3,
                    n.ZMODIFICATIONDATE1,
                    n.ZISPINNED
                FROM ZICCLOUDSYNCINGOBJECT ia
                JOIN ZICCLOUDSYNCINGOBJECT n
                    ON n.Z_PK = ia.ZNOTE1 AND n.Z_ENT = {_ENT_NOTE}
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                WHERE ia.Z_ENT = {self._ENT_INLINE_ATTACHMENT}
                  AND ia.ZTYPEUTI1 = '{self._HASHTAG_UTI}'
                  AND ia.ZTOKENCONTENTIDENTIFIER = ?
                  AND n.ZMARKEDFORDELETION = 0
                  AND (f.ZFOLDERTYPE IS NULL OR f.ZFOLDERTYPE != 1)
                ORDER BY n.ZMODIFICATIONDATE1 DESC
                LIMIT ?
                """,
                (token, limit),
            ).fetchall()

            return [
                {
                    "note_id": r["note_id"],
                    "identifier": r["identifier"],
                    "title": r["title"] or "",
                    "snippet": r["snippet"] or "",
                    "folder": r["folder"] or "",
                    "created": self._coredata_to_iso(r["ZCREATIONDATE3"]),
                    "modified": self._coredata_to_iso(r["ZMODIFICATIONDATE1"]),
                    "is_pinned": bool(r["ZISPINNED"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Single note read
    # ------------------------------------------------------------------

    def get_note(self, note_id: int) -> dict:
        """Get full content of a note by its Z_PK."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT
                    n.Z_PK,
                    n.ZTITLE1,
                    n.ZSNIPPET,
                    n.ZFOLDER,
                    f.ZTITLE2 as folder_name,
                    n.ZCREATIONDATE3,
                    n.ZMODIFICATIONDATE1,
                    n.ZISPINNED,
                    n.ZHASCHECKLIST,
                    n.ZIDENTIFIER,
                    nd.ZDATA
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
                WHERE n.Z_PK = ? AND n.Z_ENT = ?
                """,
                (note_id, _ENT_NOTE),
            ).fetchone()

            if not row:
                raise ValueError(f"Note with id {note_id} not found")

            # Extract body — try protobuf first, then AppleScript fallback
            body = ""
            body_source = "empty"
            if row["ZDATA"]:
                body = self._extract_text_from_protobuf(row["ZDATA"])
                if body:
                    body_source = "protobuf"

            if not body:
                body = self._get_body_via_applescript(row["ZIDENTIFIER"])
                if body:
                    body_source = "applescript"

            # Get tags for this note
            tags = self.get_note_tags(note_id)

            return {
                "note_id": row["Z_PK"],
                "identifier": row["ZIDENTIFIER"],
                "title": row["ZTITLE1"] or "",
                "body": body,
                "body_source": body_source,
                "folder": row["folder_name"] or "",
                "tags": tags,
                "created": self._coredata_to_iso(row["ZCREATIONDATE3"]),
                "modified": self._coredata_to_iso(row["ZMODIFICATIONDATE1"]),
                "is_pinned": bool(row["ZISPINNED"]),
                "has_checklist": bool(row["ZHASCHECKLIST"]),
            }
        finally:
            conn.close()

    def _get_body_via_applescript(self, identifier: str) -> str:
        """Fallback: get note body HTML via AppleScript, convert to markdown."""
        try:
            from .applescript_bridge import get_note_html

            html = get_note_html(identifier)
            if not html:
                return ""
            try:
                import markdownify

                return markdownify.markdownify(html, strip=["img"])
            except ImportError:
                logger.warning("markdownify not installed, returning raw HTML")
                return html
        except Exception as e:
            logger.warning("AppleScript body fallback failed: %s", e)
            return ""

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Aggregate counts and statistics."""
        conn = self._connect()
        try:
            # Exclude notes in trash folders (handle NULL ZFOLDER safely)
            _not_trash = f"""
                (n.ZFOLDER IS NULL OR n.ZFOLDER NOT IN (
                    SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT
                    WHERE Z_ENT = {_ENT_FOLDER} AND ZFOLDERTYPE = 1
                ))
            """

            total = conn.execute(
                f"SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT n WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0 AND {_not_trash}",
                (_ENT_NOTE,),
            ).fetchone()[0]

            pinned = conn.execute(
                f"SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT n WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0 AND n.ZISPINNED = 1 AND {_not_trash}",
                (_ENT_NOTE,),
            ).fetchone()[0]

            with_checklist = conn.execute(
                f"SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT n WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0 AND n.ZHASCHECKLIST = 1 AND {_not_trash}",
                (_ENT_NOTE,),
            ).fetchone()[0]

            folder_count = conn.execute(
                "SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT = ? AND ZMARKEDFORDELETION = 0 AND ZISHIDDENNOTECONTAINER != 1 AND (ZFOLDERTYPE IS NULL OR ZFOLDERTYPE != 1)",
                (_ENT_FOLDER,),
            ).fetchone()[0]

            # Notes per folder
            per_folder = []
            for row in conn.execute(
                f"""
                SELECT f.ZTITLE2 as folder_name, COUNT(n.Z_PK) as cnt
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0
                  AND (f.ZFOLDERTYPE IS NULL OR f.ZFOLDERTYPE != 1)
                GROUP BY n.ZFOLDER
                ORDER BY cnt DESC
                """,
                (_ENT_NOTE,),
            ):
                per_folder.append(
                    {"folder": row["folder_name"] or "(unknown)", "count": row["cnt"]}
                )

            # Date range (excluding trash)
            dates = conn.execute(
                f"""
                SELECT
                    MIN(n.ZCREATIONDATE3) as oldest,
                    MAX(n.ZMODIFICATIONDATE1) as newest
                FROM ZICCLOUDSYNCINGOBJECT n
                WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0 AND {_not_trash}
                """,
                (_ENT_NOTE,),
            ).fetchone()

            return {
                "total_notes": total,
                "total_folders": folder_count,
                "pinned_notes": pinned,
                "notes_with_checklists": with_checklist,
                "notes_per_folder": per_folder,
                "oldest_note": self._coredata_to_iso(dates["oldest"]),
                "newest_modification": self._coredata_to_iso(dates["newest"]),
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    def _ensure_fts_index(self) -> sqlite3.Connection:
        """Build or rebuild the in-memory FTS5 index if NoteStore changed.

        Each thread gets its own in-memory SQLite connection stored in
        ``self._fts_local``.  SQLite connections created on one thread cannot be
        used on another (``check_same_thread=True`` is the default), so sharing a
        single ``self._fts_conn`` across FastMCP's thread-pool workers causes
        ``ProgrammingError: SQLite objects created in a thread can only be used in
        that same thread``.  Using ``threading.local()`` gives every worker its own
        copy and eliminates that error without loosening SQLite's safety guarantees.

        The global ``_fts_mtime`` tracks when the canonical NoteStore file last
        changed.  When the mtime advances, all threads discard their stale
        connections and rebuild on their next call.
        """
        try:
            current_mtime = os.path.getmtime(self._db_path)
        except OSError:
            current_mtime = 0.0

        # Fast path: this thread already has a valid connection for the current mtime
        thread_conn: sqlite3.Connection | None = getattr(self._fts_local, "conn", None)
        thread_mtime: float = getattr(self._fts_local, "mtime", -1.0)
        if thread_conn is not None and thread_mtime == current_mtime:
            return thread_conn

        with self._fts_lock:
            # Re-read mtime inside the lock — another thread may have just rebuilt
            # and advanced self._fts_mtime while we were waiting.
            try:
                current_mtime = os.path.getmtime(self._db_path)
            except OSError:
                current_mtime = 0.0

            # Close stale thread-local connection if it exists
            if thread_conn is not None:
                try:
                    thread_conn.close()
                except Exception:
                    pass

            logger.info("Building FTS index for thread %d (NoteStore mtime changed)", threading.get_ident())
            fts_conn = sqlite3.connect(":memory:")
            fts_conn.execute(
                """
                CREATE VIRTUAL TABLE notes_fts USING fts5(
                    note_id UNINDEXED,
                    title,
                    body,
                    folder,
                    tokenize='porter unicode61'
                )
                """
            )

            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT
                        n.Z_PK,
                        n.ZTITLE1,
                        n.ZFOLDER,
                        f.ZTITLE2 as folder_name,
                        nd.ZDATA
                    FROM ZICCLOUDSYNCINGOBJECT n
                    LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                        ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                    LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
                    WHERE n.Z_ENT = ? AND n.ZMARKEDFORDELETION = 0
                      AND (f.ZFOLDERTYPE IS NULL OR f.ZFOLDERTYPE != 1)
                    """,
                    (_ENT_NOTE,),
                ).fetchall()

                for r in rows:
                    body_text = ""
                    if r["ZDATA"]:
                        body_text = self._extract_text_from_protobuf(r["ZDATA"])
                    fts_conn.execute(
                        "INSERT INTO notes_fts (note_id, title, body, folder) VALUES (?, ?, ?, ?)",
                        (r["Z_PK"], r["ZTITLE1"] or "", body_text, r["folder_name"] or ""),
                    )
                fts_conn.commit()
            finally:
                conn.close()

            # Store in thread-local so this thread can reuse it
            self._fts_local.conn = fts_conn
            self._fts_local.mtime = current_mtime
            self._fts_mtime = current_mtime
            logger.info("FTS index built: %d notes indexed", len(rows))
            return fts_conn

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize a user query for FTS5 MATCH."""
        # Wrap each token in double quotes to prevent FTS5 syntax errors
        tokens = query.split()
        sanitized = []
        for token in tokens:
            # Strip FTS5 metacharacters
            cleaned = re.sub(r'["\*]', "", token)
            if cleaned and cleaned.upper() not in ("AND", "OR", "NOT", "NEAR"):
                sanitized.append(f'"{cleaned}"')
        return " ".join(sanitized) if sanitized else '""'

    def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across note titles and bodies."""
        fts_conn = self._ensure_fts_index()
        safe_query = self._sanitize_fts_query(query)

        try:
            fts_rows = fts_conn.execute(
                """
                SELECT
                    note_id,
                    title,
                    snippet(notes_fts, 2, '>>>', '<<<', '...', 40) as snippet,
                    folder,
                    bm25(notes_fts) as rank
                FROM notes_fts
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("FTS query failed: %s", e)
            return []

        # Enrich with metadata from NoteStore
        if not fts_rows:
            return []

        note_ids = [r[0] for r in fts_rows]
        conn = self._connect()
        try:
            placeholders = ",".join("?" * len(note_ids))
            meta_rows = conn.execute(
                f"""
                SELECT Z_PK, ZIDENTIFIER, ZCREATIONDATE3, ZMODIFICATIONDATE1, ZISPINNED
                FROM ZICCLOUDSYNCINGOBJECT
                WHERE Z_PK IN ({placeholders})
                """,
                note_ids,
            ).fetchall()
            meta = {r["Z_PK"]: r for r in meta_rows}
        finally:
            conn.close()

        results = []
        for r in fts_rows:
            m = meta.get(r[0], {})
            results.append(
                {
                    "note_id": r[0],
                    "identifier": m["ZIDENTIFIER"] if m else None,
                    "title": r[1],
                    "snippet": r[2],
                    "folder": r[3],
                    "rank": round(r[4], 4),
                    "created": self._coredata_to_iso(m["ZCREATIONDATE3"]) if m else None,
                    "modified": self._coredata_to_iso(m["ZMODIFICATIONDATE1"]) if m else None,
                    "is_pinned": bool(m["ZISPINNED"]) if m else False,
                }
            )
        return results

    # ------------------------------------------------------------------
    # Post-create identifier lookup (for building applenotes:// deep links)
    # ------------------------------------------------------------------

    def find_note_identifier_by_title(self, title: str, folder: str | None = None) -> str | None:
        """Return the ZIDENTIFIER (UUID) of the most-recently-created note matching *title*.

        Used right after an AppleScript/Shortcut create to obtain the UUID needed
        for an ``applenotes://showNote?identifier=<UUID>`` deep-link.

        Args:
            title: Exact note title to match (case-sensitive, per Apple Notes behaviour).
            folder: Optional folder name to narrow the match.  Pass the folder used
                    when creating the note to avoid returning a same-titled note from
                    a different folder.

        Returns:
            The ZIDENTIFIER string, or ``None`` if no match is found within 5 seconds
            of calling (NoteStore may not have flushed the new row yet — callers should
            retry briefly or tolerate a ``None`` result).
        """
        conn = self._connect()
        try:
            where = ["n.Z_ENT = ?", "n.ZMARKEDFORDELETION = 0", "n.ZTITLE1 = ?"]
            params: list = [_ENT_NOTE, title]

            if folder:
                where.append("f.ZTITLE2 = ?")
                params.append(folder)

            where_sql = " AND ".join(where)

            row = conn.execute(
                f"""
                SELECT n.ZIDENTIFIER
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f
                    ON f.Z_PK = n.ZFOLDER AND f.Z_ENT = {_ENT_FOLDER}
                WHERE {where_sql}
                ORDER BY n.ZCREATIONDATE3 DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row["ZIDENTIFIER"] if row else None
        finally:
            conn.close()
