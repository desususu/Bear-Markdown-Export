"""
Command-line interface for b2ou.

Subcommands
-----------
export     Export Bear notes to Markdown / TextBundle files.
import     Import changed Markdown / TextBundle files back into Bear.
sync       Smart sync (run-once) — checks guards before syncing (reads JSON config).
sync-manual Run one complete export + import cycle with CLI flags.
daemon     Start the FSEvents-driven sync daemon.
guard-test Test all three editing-guard layers and report results.

Usage examples
--------------
  python -m b2ou export --out ~/Notes --backup ~/NotesBak
  python -m b2ou import --out ~/Notes --backup ~/NotesBak
  python -m b2ou sync
  python -m b2ou sync-manual --out ~/Notes --backup ~/NotesBak
  python -m b2ou daemon
  python -m b2ou guard-test
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    """Configure root logging (file handler + optional stderr)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if sys.stdout.isatty() or not log_file:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

_HOME = Path.home()

_DEFAULT_OUT    = _HOME / "Work" / "BearNotes"
_DEFAULT_BACKUP = _HOME / "Work" / "BearSyncBackup"


def _add_export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out",    default=str(_DEFAULT_OUT),
                   help="Folder where Bear notes will be exported")
    p.add_argument("--backup", default=str(_DEFAULT_BACKUP),
                   help="Folder where conflict backups are stored")
    p.add_argument("--images", default=None,
                   help="Override path for the BearImages asset folder")
    p.add_argument("--format", choices=["md", "tb"], default="md",
                   help="Export format: md (Markdown) or tb (TextBundle)")
    p.add_argument("--exclude-tag", action="append", dest="exclude_tags",
                   default=[], metavar="TAG",
                   help="Skip notes with this Bear tag (repeatable)")
    p.add_argument("--hide-tags", action="store_true",
                   help="Strip #tags from exported Markdown")
    p.add_argument("--tag-folders", action="store_true",
                   help="Organise notes into subdirectories by tag")
    p.add_argument("--clean-export", action="store_true",
                   help="Export clean Markdown without BearID footers (disables import matching)")
    p.add_argument("-v", "--verbose", action="store_true")


def _build_export_config(args: argparse.Namespace):
    from b2ou.config import ExportConfig
    assets = Path(args.images) if args.images else None
    return ExportConfig(
        export_path=Path(args.out),
        backup_path=Path(args.backup),
        assets_path=assets,
        export_format=args.format,
        exclude_tags=args.exclude_tags,
        hide_tags=args.hide_tags,
        make_tag_folders=args.tag_folders,
        clean_export=getattr(args, "clean_export", False),
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    """Export Bear notes to disk."""
    cfg = _build_export_config(args)
    setup_logging(cfg.log_file if cfg.logging_enabled else None, args.verbose)
    log = logging.getLogger(__name__)

    from b2ou.export import (
        check_db_modified,
        cleanup_orphan_root_images,
        cleanup_stale_notes,
        export_notes,
        write_timestamps,
    )

    if not check_db_modified(cfg):
        log.info("No notes needed export (Bear database unchanged).")
        return 0

    cfg.export_path.mkdir(parents=True, exist_ok=True)
    note_count, expected = export_notes(cfg)
    write_timestamps(cfg)

    removed = cleanup_stale_notes(cfg.export_path, expected)
    if removed:
        log.info("Cleaned %d stale files from export folder.", removed)

    orphans = cleanup_orphan_root_images(cfg)
    if orphans:
        log.info("Cleaned %d orphan root images.", orphans)

    log.info("%d notes exported to: %s", note_count, cfg.export_path)
    return 1


def cmd_import(args: argparse.Namespace) -> int:
    """Import changed notes from disk back into Bear."""
    cfg = _build_export_config(args)
    setup_logging(cfg.log_file if cfg.logging_enabled else None, args.verbose)
    log = logging.getLogger(__name__)

    from b2ou.import_ import import_changed_notes
    updated = import_changed_notes(cfg)
    if updated:
        log.info("Import phase complete.")
        return 1
    log.info("No changed notes to import.")
    return 0


def cmd_sync_manual(args: argparse.Namespace) -> int:
    """Run a full export + import cycle using CLI arguments."""
    import_rc = 0
    if not getattr(args, "clean_export", False):
        # Import first, then export (hub-and-spoke: Bear is truth)
        import_rc = cmd_import(args)
    export_rc = cmd_export(args)
    return max(import_rc, export_rc)


def cmd_sync(args: argparse.Namespace) -> int:
    """Smart sync gate (run-once). Uses JSON configuration."""
    script_dir = Path(args.config).parent if args.config else Path.cwd()
    config_file = Path(args.config) if args.config else script_dir / "b2ou_config.json"

    from b2ou.config import load_sync_gate_config
    try:
        cfg = load_sync_gate_config(config_file)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    log_file = script_dir / "b2ou_sync.log"
    setup_logging(log_file, args.verbose)

    from b2ou.daemon import run_once
    return run_once(
        cfg, script_dir,
        force=args.force,
        export_only=args.export_only or args.clean_export or cfg.clean_export,
        dry_run=args.dry_run,
    )


def cmd_daemon(args: argparse.Namespace) -> int:
    """Start the FSEvents-driven sync daemon."""
    script_dir = Path(args.config).parent if args.config else Path.cwd()
    config_file = Path(args.config) if args.config else script_dir / "b2ou_config.json"

    from b2ou.config import load_sync_gate_config
    try:
        cfg = load_sync_gate_config(config_file)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    log_file = script_dir / "b2ou_daemon.log"
    setup_logging(log_file, args.verbose)

    from b2ou.daemon import SyncDaemon
    daemon = SyncDaemon(
        cfg, script_dir,
        export_only=args.export_only or args.clean_export or cfg.clean_export,
    )
    daemon.run()
    return 0


def cmd_guard_test(args: argparse.Namespace) -> int:
    """Test all three editing-guard layers and print results."""
    script_dir = Path(args.config).parent if args.config else Path.cwd()
    config_file = Path(args.config) if args.config else script_dir / "b2ou_config.json"

    from b2ou.config import load_sync_gate_config
    try:
        cfg = load_sync_gate_config(config_file)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    setup_logging(None, verbose=True)
    log = logging.getLogger(__name__)

    from b2ou.daemon import _resolve
    folder_md = _resolve(cfg.folder_md, script_dir)
    folder_tb = _resolve(cfg.folder_tb, script_dir)
    folders = [folder_md, folder_tb]

    log.info("Testing editing guard layers…")
    from b2ou.guard import check_editing_guard
    reason = check_editing_guard(
        folders, float(cfg.write_quiet_seconds), 0.0, verbose=True
    )
    if reason:
        log.info("Guard BLOCKED: %s", reason)
        return 1
    log.info("Guard: all clear — sync would proceed.")
    return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="b2ou",
        description="Bear to Obsidian / Ulysses sync toolkit",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {_version()}"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ── export ───────────────────────────────────────────────────────────
    p_export = sub.add_parser("export", help="Export Bear notes to disk")
    _add_export_args(p_export)
    p_export.set_defaults(func=cmd_export)

    # ── import ───────────────────────────────────────────────────────────
    p_import = sub.add_parser("import", help="Import changed notes into Bear")
    _add_export_args(p_import)
    p_import.set_defaults(func=cmd_import)

    # ── sync-manual ──────────────────────────────────────────────────────
    p_sync_manual = sub.add_parser("sync-manual", help="Run a manual import + export cycle (ignores JSON config)")
    _add_export_args(p_sync_manual)
    p_sync_manual.set_defaults(func=cmd_sync_manual)

    # ── sync ─────────────────────────────────────────────────────────────
    p_sync = sub.add_parser("sync", help="Run-once smart sync (uses JSON config)")
    p_sync.add_argument("--config", default=None, metavar="FILE",
                        help="Path to b2ou_config.json")
    p_sync.add_argument("--force", action="store_true",
                        help="Bypass all guards and sync immediately")
    p_sync.add_argument("--export-only", action="store_true",
                        help="Skip import phase")
    p_sync.add_argument("--clean-export", action="store_true",
                        help="Export clean Markdown without BearID footers (forces export-only)")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without actually syncing")
    p_sync.add_argument("-v", "--verbose", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # ── daemon ───────────────────────────────────────────────────────────
    p_daemon = sub.add_parser("daemon", help="Start the FSEvents-driven sync daemon")
    p_daemon.add_argument("--config", default=None, metavar="FILE",
                          help="Path to b2ou_config.json")
    p_daemon.add_argument("--export-only", action="store_true",
                          help="Skip import phase")
    p_daemon.add_argument("--clean-export", action="store_true",
                          help="Export clean Markdown without BearID footers (forces export-only)")
    p_daemon.add_argument("-v", "--verbose", action="store_true")
    p_daemon.set_defaults(func=cmd_daemon)

    # ── guard-test ───────────────────────────────────────────────────────
    p_gt = sub.add_parser("guard-test", help="Test editing-guard layers")
    p_gt.add_argument("--config", default=None, metavar="FILE")
    p_gt.add_argument("-v", "--verbose", action="store_true")
    p_gt.set_defaults(func=cmd_guard_test)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    return args.func(args)


def _version() -> str:
    try:
        from b2ou import __version__
        return __version__
    except ImportError:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
