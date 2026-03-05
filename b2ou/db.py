"""
Bear SQLite database access layer.

All functions open the database in read-only mode (``?mode=ro``) to avoid
corrupting Bear's live database.  For export the database is first copied to
a temporary file so Bear can write freely while the export runs.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from b2ou.constants import CORE_DATA_EPOCH

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BearNote:
    title: str
    text: str
    creation_date: float   # Core Data timestamp (seconds since 2001-01-01)
    modified_date: float   # Core Data timestamp
    uuid: str
    pk: int


@dataclass(frozen=True)
class NoteFile:
    filename: str
    uuid: str


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def core_data_to_unix(ts: float) -> float:
    """Convert a Core Data timestamp to a Unix timestamp."""
    return ts + CORE_DATA_EPOCH


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open Bear's SQLite database in read-only mode."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def copy_and_open(db_path: Path) -> tuple[sqlite3.Connection, Optional[Path]]:
    """
    Copy Bear's database to a temp file and open it for reading.

    Returns ``(conn, tmp_path)``.  The caller must close *conn* and then
    delete *tmp_path* when finished.  If copying fails the live database is
    opened directly and *tmp_path* is ``None``.
    """
    try:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite", prefix="b2ou_export_")
        import os
        os.close(fd)
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        return conn, Path(tmp)
    except Exception as exc:
        log.warning("Could not copy database (%s) — reading live DB.", exc)
        conn = open_readonly(db_path)
        return conn, None


# ---------------------------------------------------------------------------
# Note queries
# ---------------------------------------------------------------------------

def iter_notes(conn: sqlite3.Connection) -> Iterator[BearNote]:
    """Yield every non-trashed, non-archived note."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ZTITLE, ZTEXT, ZCREATIONDATE, ZMODIFICATIONDATE, "
        "       ZUNIQUEIDENTIFIER, Z_PK "
        "FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 AND ZARCHIVED = 0"
    )
    for row in cursor:
        yield BearNote(
            title=row["ZTITLE"],
            text=row["ZTEXT"].rstrip(),
            creation_date=row["ZCREATIONDATE"],
            modified_date=row["ZMODIFICATIONDATE"],
            uuid=row["ZUNIQUEIDENTIFIER"],
            pk=row["Z_PK"],
        )


def get_note_by_uuid(
    conn: sqlite3.Connection, uuid: str
) -> Optional[BearNote]:
    row = conn.execute(
        "SELECT ZTITLE, ZTEXT, ZCREATIONDATE, ZMODIFICATIONDATE, "
        "       ZUNIQUEIDENTIFIER, Z_PK "
        "FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 AND ZUNIQUEIDENTIFIER = ?",
        (uuid,),
    ).fetchone()
    if row is None:
        return None
    return BearNote(
        title=row["ZTITLE"],
        text=row["ZTEXT"].rstrip(),
        creation_date=row["ZCREATIONDATE"],
        modified_date=row["ZMODIFICATIONDATE"],
        uuid=row["ZUNIQUEIDENTIFIER"],
        pk=row["Z_PK"],
    )


def get_note_by_title(
    conn: sqlite3.Connection, title: str
) -> Optional[BearNote]:
    """Return the most-recently-modified non-trashed note with *title*."""
    if not title:
        return None
    row = conn.execute(
        "SELECT ZTITLE, ZTEXT, ZCREATIONDATE, ZMODIFICATIONDATE, "
        "       ZUNIQUEIDENTIFIER, Z_PK "
        "FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 AND ZARCHIVED = 0 AND ZTITLE = ? "
        "ORDER BY ZMODIFICATIONDATE DESC LIMIT 1",
        (title,),
    ).fetchone()
    if row is None:
        return None
    return BearNote(
        title=row["ZTITLE"],
        text=row["ZTEXT"].rstrip(),
        creation_date=row["ZCREATIONDATE"],
        modified_date=row["ZMODIFICATIONDATE"],
        uuid=row["ZUNIQUEIDENTIFIER"],
        pk=row["Z_PK"],
    )


def get_note_modification(
    conn: sqlite3.Connection, uuid: str
) -> Optional[float]:
    """Return the Core Data modification timestamp for *uuid*, or None."""
    row = conn.execute(
        "SELECT ZMODIFICATIONDATE FROM ZSFNOTE "
        "WHERE ZTRASHED = 0 AND ZUNIQUEIDENTIFIER = ?",
        (uuid,),
    ).fetchone()
    return float(row["ZMODIFICATIONDATE"]) if row else None


def get_note_files(conn: sqlite3.Connection, note_pk: int) -> list[NoteFile]:
    """Return all files attached to the note identified by *note_pk*."""
    rows = conn.execute(
        "SELECT ZFILENAME, ZUNIQUEIDENTIFIER "
        "FROM ZSFNOTEFILE WHERE ZNOTE = ?",
        (note_pk,),
    ).fetchall()
    return [NoteFile(filename=r["ZFILENAME"], uuid=r["ZUNIQUEIDENTIFIER"])
            for r in rows]


def get_note_files_by_uuid(
    conn: sqlite3.Connection, note_uuid: str
) -> list[NoteFile]:
    """Return all files attached to the note identified by its *note_uuid*."""
    rows = conn.execute(
        "SELECT F.ZFILENAME, F.ZUNIQUEIDENTIFIER "
        "FROM ZSFNOTEFILE F "
        "JOIN ZSFNOTE N ON F.ZNOTE = N.Z_PK "
        "WHERE N.ZUNIQUEIDENTIFIER = ? AND N.ZTRASHED = 0",
        (note_uuid,),
    ).fetchall()
    return [NoteFile(filename=r["ZFILENAME"], uuid=r["ZUNIQUEIDENTIFIER"])
            for r in rows]


# ---------------------------------------------------------------------------
# Change-detection helpers
# ---------------------------------------------------------------------------

def bear_db_signature(db_path: Path) -> tuple[float, int]:
    """
    Return a lightweight content signature: ``(max_mod_unix, note_count)``.

    *max_mod_unix* tracks the newest visible-note modification timestamp.
    *note_count*   catches deletes/archives where max_mod might not increase.
    """
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT MAX(ZMODIFICATIONDATE), COUNT(*) "
                "FROM ZSFNOTE WHERE ZTRASHED = 0 AND ZARCHIVED = 0"
            ).fetchone()
            if row and row[0] is not None:
                return core_data_to_unix(row[0]), int(row[1])
    except Exception as exc:
        log.debug("Could not read Bear DB signature: %s", exc)
    return 0.0, -1


def db_is_quiet(db_path: Path, quiet_seconds: float) -> bool:
    """True when all Bear database files have been idle for *quiet_seconds*."""
    import os
    import time
    now = time.time()
    for suffix in ("", "-wal", "-shm"):
        try:
            if now - os.stat(str(db_path) + suffix).st_mtime < quiet_seconds:
                return False
        except OSError:
            pass
    return True
