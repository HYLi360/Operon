#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Manifest file commands: ingest, verify, standardize, and backup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from operon.backup import create_backup, verify_backup
from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.files import (
    ingest_file,
    recompress_files,
    standardize_all,
    standardize_file,
    verify_files,
)
from operon.utils import format_table


def add_ingest_parser(sub):
    p = sub.add_parser(
        "ingest",
        help="archive a file or directory (local path, sftp:// or remote:// URL) into raw/ with a content hash and manifest record",
    )
    p.add_argument(
        "--source",
        required=True,
        help="local path, sftp://[user@]host[:port]/path, or remote://<name>/<path>",
    )
    p.add_argument(
        "--entity-type",
        required=True,
        choices=["organism", "sample", "run", "assembly", "annotation"],
    )
    p.add_argument("--entity-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--format", dest="fmt")
    p.add_argument("--compression")
    p.add_argument("--source-url")
    p.add_argument(
        "--compress-archive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="archive plain FASTA/FASTQ sources as deterministic BGZF "
        "(default: the project's storage.compress_archive setting)",
    )
    p.add_argument("--move", action="store_true", help="move source instead of copying")


def add_verify_parser(sub):
    p = sub.add_parser(
        "verify",
        help="verify local artifacts and live-check recorded remotes for remote-only files",
    )
    p.add_argument("--file-id", action="append", default=[])


def add_standardize_parser(sub):
    p = sub.add_parser(
        "standardize",
        help="create standardized/ links or copies from verified raw files",
    )
    p.add_argument("--file-id", action="append", default=[])
    p.add_argument("--link", choices=["hardlink", "symlink", "copy"], default="copy")


def add_recompress_parser(sub):
    p = sub.add_parser(
        "recompress",
        help="re-archive plain FASTA/FASTQ raw files as deterministic BGZF "
        "(one-shot storage migration; push afterwards supersedes stale remote copies)",
    )
    p.add_argument("--file-id", action="append", default=[])
    p.add_argument("--all", action="store_true", help="recompress every eligible file")
    p.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )


def add_backup_parser(sub):
    p = sub.add_parser(
        "backup", help="create or verify a checksum-manifested project backup"
    )
    backup_sub = p.add_subparsers(dest="backup_command", required=True)
    bp = backup_sub.add_parser(
        "create", help="create a consistent SQLite-centered backup"
    )
    bp.add_argument("--output", required=True)
    bp.add_argument(
        "--scope", choices=["control", "results", "full"], default="control"
    )
    bp = backup_sub.add_parser("verify", help="verify every file in an existing backup")
    bp.add_argument("--input", required=True)


def _cmd_ingest(args: argparse.Namespace, project: Project, db: Database) -> int:
    source = args.source
    source_url = args.source_url
    temp_path: Path | None = None
    if source.startswith(("sftp://", "remote://")):
        from operon.remotes import fetch_url_to_temp

        temp_path = fetch_url_to_temp(project, source)
        source = str(temp_path)
        source_url = source_url or args.source
    try:
        row = ingest_file(
            db,
            project,
            source,
            args.entity_type,
            args.entity_id,
            args.role,
            fmt=args.fmt,
            compression=args.compression,
            source_url=source_url,
            move=args.move,
            compress_archive=getattr(args, "compress_archive", None),
        )
    finally:
        if temp_path is not None:
            if temp_path.is_dir() and not temp_path.is_symlink():
                import shutil

                shutil.rmtree(temp_path, ignore_errors=True)
            else:
                temp_path.unlink(missing_ok=True)
    print(
        f"registered {row['file_id']} -> {row['relative_path']} (sha256 {row['sha256'][:16]}...)"
    )
    return 0


def _cmd_verify(args: argparse.Namespace, project: Project, db: Database) -> int:
    results = verify_files(db, project, args.file_id or None)
    failed = [
        r for r in results if r["status"] not in {"CHECKSUM_VERIFIED", "REMOTE_ONLY"}
    ]
    print(
        format_table(
            ["file_id", "relative_path", "status", "remote", "current_sha256", "error"],
            (
                [
                    r["file_id"],
                    r["relative_path"],
                    r["status"],
                    r.get("remote") or "",
                    r["current_sha256"] or "",
                    r["error"] or "",
                ]
                for r in results
            ),
        )
    )
    if failed:
        return 1
    print(f"verified {len(results)} file(s)")
    return 0


def _cmd_standardize(args: argparse.Namespace, project: Project, db: Database) -> int:
    errors = 0
    if args.file_id:
        for file_id in args.file_id:
            result = standardize_file(db, project, file_id, link_kind=args.link)
            print(f"{file_id}: {result['action']} -> {result['target']}")
    else:
        for result in standardize_all(db, project, link_kind=args.link):
            if "error" in result:
                errors += 1
                print(
                    f"{result.get('file_id', '?')}: ERROR {result['error']}",
                    file=sys.stderr,
                )
            else:
                print(f"{result['file_id']}: {result['action']} -> {result['target']}")
    return 1 if errors else 0


def _cmd_recompress(args: argparse.Namespace, project: Project, db: Database) -> int:
    if not args.file_id and not args.all:
        raise ValidationError("recompress requires --file-id or --all")
    results = recompress_files(db, project, args.file_id or None, dry_run=args.dry_run)
    print(
        format_table(
            ["file_id", "relative_path", "status", "error"],
            (
                [
                    r["file_id"],
                    r.get("relative_path") or "",
                    r["status"],
                    r.get("error") or "",
                ]
                for r in results
            ),
        )
    )
    failed = [r for r in results if r["status"] == "error"]
    if failed:
        return 1
    print(f"recompress: {len(results)} file(s) processed")
    return 0


def _cmd_backup(args: argparse.Namespace, project: Project, db: Database) -> int:
    result = create_backup(db, project, args.output, scope=args.scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    result = verify_backup(args.input)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1
