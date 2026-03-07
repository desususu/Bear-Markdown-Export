import os
import time
from pathlib import Path

import pytest

from b2ou.config import ExportConfig, SyncGateConfig
from b2ou.daemon import run_once
from b2ou.db import BearNote
from b2ou.export import export_notes, write_note_file
from b2ou.import_ import backup_disk_note, import_changed_notes, iter_changed_note_files
from b2ou.snapshot import VaultSnapshot


class _DummyConn:
    def close(self) -> None:
        return None


def _write_with_mtime(path: Path, content: str, ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (ts, ts))


def test_backup_disk_note_plain_file_is_versioned(tmp_path: Path) -> None:
    src = tmp_path / "note.md"
    backup = tmp_path / "backup"

    src.write_text("v1", encoding="utf-8")
    backup_disk_note(src, backup)

    src.write_text("v2", encoding="utf-8")
    backup_disk_note(src, backup)

    assert (backup / "note.md").read_text(encoding="utf-8") == "v1"
    assert (backup / "note - 02.md").read_text(encoding="utf-8") == "v2"


def test_backup_disk_note_same_names_from_different_dirs(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    a = tmp_path / "A" / "same.md"
    b = tmp_path / "B" / "same.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    b.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")

    backup_disk_note(a, backup)
    backup_disk_note(b, backup)

    assert (backup / "same.md").read_text(encoding="utf-8") == "A"
    assert (backup / "same - 02.md").read_text(encoding="utf-8") == "B"


def test_import_changed_notes_keeps_timestamp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = ExportConfig(
        export_path=tmp_path / "vault",
        backup_path=tmp_path / "backup",
        bear_db=tmp_path / "database.sqlite",
        bear_image_path=tmp_path / "images",
    )
    old_ts = time.time() - 120
    _write_with_mtime(cfg.sync_ts_file, "sync", old_ts)
    _write_with_mtime(cfg.export_ts_file, "export", old_ts)
    _write_with_mtime(cfg.export_path / "A.md", "# A\nbody", old_ts + 10)

    monkeypatch.setattr("b2ou.import_.open_readonly", lambda _db: _DummyConn())

    def _fail_update(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("b2ou.import_.update_bear_note", _fail_update)

    changed = import_changed_notes(cfg)
    assert changed is False
    assert cfg.sync_ts_file.stat().st_mtime == pytest.approx(old_ts, abs=0.01)


def test_import_changed_notes_updates_timestamp_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = ExportConfig(
        export_path=tmp_path / "vault",
        backup_path=tmp_path / "backup",
        bear_db=tmp_path / "database.sqlite",
        bear_image_path=tmp_path / "images",
    )
    old_ts = time.time() - 120
    _write_with_mtime(cfg.sync_ts_file, "sync", old_ts)
    _write_with_mtime(cfg.export_ts_file, "export", old_ts)
    _write_with_mtime(cfg.export_path / "B.md", "# B\nbody", old_ts + 10)

    monkeypatch.setattr("b2ou.import_.open_readonly", lambda _db: _DummyConn())
    monkeypatch.setattr("b2ou.import_.update_bear_note", lambda *_a, **_k: None)

    changed = import_changed_notes(cfg)
    assert changed is True
    assert cfg.sync_ts_file.stat().st_mtime > old_ts


def test_export_notes_disambiguates_duplicate_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ExportConfig(
        export_path=tmp_path / "out",
        backup_path=tmp_path / "backup",
        export_format="md",
    )

    notes = [
        BearNote(
            title="Same title",
            text="# Same title\nfirst",
            creation_date=1.0,
            modified_date=2.0,
            uuid="11111111-1111-1111-1111-111111111111",
            pk=1,
        ),
        BearNote(
            title="Same title",
            text="# Same title\nsecond",
            creation_date=1.0,
            modified_date=3.0,
            uuid="22222222-2222-2222-2222-222222222222",
            pk=2,
        ),
    ]

    monkeypatch.setattr(
        "b2ou.export.copy_and_open",
        lambda _db: (_DummyConn(), None),
    )
    monkeypatch.setattr("b2ou.export.iter_notes", lambda _conn: iter(notes))
    monkeypatch.setattr(
        "b2ou.export.process_export_images",
        lambda text, *_args, **_kwargs: text,
    )
    monkeypatch.setattr("b2ou.export.set_creation_date", lambda *_a, **_k: None)

    count, expected = export_notes(cfg)
    assert count == 2
    assert len(expected) == 2

    names = sorted(p.name for p in expected)
    assert "Same title.md" in names
    assert "Same title - 22222222.md" in names

    contents = [p.read_text(encoding="utf-8") for p in expected]
    assert any("first" in c for c in contents)
    assert any("second" in c for c in contents)


def test_run_once_returns_when_lock_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = SyncGateConfig()
    called = {"run_sync": False}

    monkeypatch.setattr("b2ou.daemon._acquire_lock", lambda _f: None)
    monkeypatch.setattr("b2ou.daemon.run_sync", lambda *_a, **_k: called.update(run_sync=True))

    rc = run_once(cfg, tmp_path)
    assert rc == 0
    assert called["run_sync"] is False


def test_run_once_reuses_prebuilt_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = SyncGateConfig(
        folder_md=tmp_path / "md",
        folder_tb=tmp_path / "tb",
        backup_md=tmp_path / "backup_md",
        backup_tb=tmp_path / "backup_tb",
    )
    cfg.folder_md.mkdir(parents=True, exist_ok=True)
    cfg.folder_tb.mkdir(parents=True, exist_ok=True)
    marker = {cfg.folder_md: object(), cfg.folder_tb: object()}
    seen: dict[str, object] = {}

    monkeypatch.setattr("b2ou.daemon._acquire_lock", lambda _f: 1)
    monkeypatch.setattr("b2ou.daemon._release_lock", lambda *_a, **_k: None)
    monkeypatch.setattr("b2ou.daemon._load_state", lambda *_a, **_k: {})
    monkeypatch.setattr("b2ou.daemon._save_state", lambda *_a, **_k: None)
    monkeypatch.setattr("b2ou.daemon.build_snapshots", lambda _folders: marker)

    def _guard(_folders, _quiet, _last, verbose=False, log_all=False, snapshots=None):
        seen["guard_snapshots"] = snapshots
        return ""

    monkeypatch.setattr("b2ou.daemon.check_editing_guard", _guard)

    class _FakeDetector:
        def __init__(self, _state, _md, _tb, _bear_db, snapshots=None):
            seen["detector_snapshots"] = snapshots

        def bear_changed(self):
            return True

        def files_changed(self):
            return (False, False)

        def snapshot(self, state, post_snapshots=None):
            seen["snapshot_post"] = post_snapshots
            state.setdefault("hashes", {})

    monkeypatch.setattr("b2ou.daemon.ChangeDetector", _FakeDetector)

    def _run_sync(_cfg, _script_dir, export_only=False, files_changed=False, pre_snapshots=None):
        seen["run_sync_snapshots"] = pre_snapshots
        return marker

    monkeypatch.setattr("b2ou.daemon.run_sync", _run_sync)

    rc = run_once(cfg, tmp_path)
    assert rc == 1
    assert seen["guard_snapshots"] is marker
    assert seen["detector_snapshots"] is marker
    assert seen["run_sync_snapshots"] is marker
    assert seen["snapshot_post"] is marker


def test_vault_snapshot_cleans_cloud_junk(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    junk_dir = vault / ".sync"
    note = vault / "keep.md"
    junk_file = vault / ".DS_Store"
    junk_dir.mkdir(parents=True, exist_ok=True)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("keep", encoding="utf-8")
    junk_file.write_text("junk", encoding="utf-8")
    (junk_dir / "tmp.txt").write_text("junk", encoding="utf-8")

    snap = VaultSnapshot(vault)
    removed = snap.clean_junk()

    assert removed >= 2
    assert note.exists()
    assert not junk_file.exists()
    assert not junk_dir.exists()


def test_write_note_file_removes_temp_file(tmp_path: Path) -> None:
    note = tmp_path / "x.md"
    write_note_file(note, "body", modified_unix=0.0, created_core_data=0.0)
    assert note.read_text(encoding="utf-8") == "body"
    assert not (tmp_path / ".x.md.tmp").exists()


def test_iter_changed_note_files_ignores_cloud_sync_artifacts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = ExportConfig(
        export_path=vault,
        backup_path=tmp_path / "backup",
        bear_db=tmp_path / "database.sqlite",
        bear_image_path=tmp_path / "images",
    )
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "keep.md").write_text("# keep", encoding="utf-8")
    (vault / ".syncloud_tmp").write_text("junk", encoding="utf-8")
    (vault / "Thumbs.db").write_text("junk", encoding="utf-8")
    (vault / ".sync").mkdir(parents=True, exist_ok=True)
    (vault / ".sync" / "ghost.md").write_text("# ghost", encoding="utf-8")
    (vault / ".dropbox.cache").mkdir(parents=True, exist_ok=True)
    (vault / ".dropbox.cache" / "ghost2.md").write_text("# ghost2", encoding="utf-8")

    changed = iter_changed_note_files(vault, 0.0, cfg)
    rels = {str(p.relative_to(vault)) for p, _ in changed}
    assert rels == {"keep.md"}
