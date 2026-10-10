"""Remote mirror commands: remotes, push, pull, evict, and locations."""

from __future__ import annotations

import argparse
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.utils import format_table


def add_remotes_parser(sub):
    sub.add_parser(
        "remotes",
        help="list configured remotes (project.yaml remotes:) and test connectivity",
    )


def add_push_parser(sub):
    p = sub.add_parser(
        "push",
        help="upload manifest files to a configured remote mirror (SFTP, checksum-verified, idempotent)",
    )
    p.add_argument(
        "--remote",
        required=True,
        help="remote name from the project.yaml remotes: section",
    )
    p.add_argument(
        "--file-id",
        action="append",
        default=[],
        help="restrict to these manifest files (default: all)",
    )


def add_evict_parser(sub):
    p = sub.add_parser(
        "evict", help="remove local bytes after an exact remote mirror copy is verified"
    )
    p.add_argument(
        "--remote", required=True, help="remote name holding the verified copy"
    )
    p.add_argument(
        "--file-id",
        action="append",
        default=[],
        help="restrict to these manifest files (default: all)",
    )


def add_locations_parser(sub):
    p = sub.add_parser(
        "locations", help="show local/remote residency for manifest files"
    )
    p.add_argument(
        "--file-id",
        action="append",
        default=[],
        help="restrict to these manifest files",
    )


def add_pull_parser(sub):
    p = sub.add_parser(
        "pull",
        help="restore manifest files from a configured remote mirror (SFTP, checksum-verified)",
    )
    p.add_argument(
        "--remote",
        required=True,
        help="remote name from the project.yaml remotes: section",
    )
    p.add_argument(
        "--file-id",
        action="append",
        default=[],
        help="restrict to these manifest files (default: everything in the remote manifest)",
    )


def _cmd_remotes(args: argparse.Namespace, project: Project) -> int:
    from operon.remotes import check_remote, list_remotes

    names = sorted(list_remotes(project))
    if not names:
        print("no remotes configured; add a 'remotes:' section to project.yaml")
        return 0
    results = [check_remote(project, name) for name in names]
    print(
        format_table(
            ["name", "type", "address", "root", "files", "status", "error"],
            (
                [
                    r["name"],
                    r["type"],
                    r["address"],
                    r["root"],
                    r["files"],
                    r["status"],
                    r["error"],
                ]
                for r in results
            ),
        )
    )
    return 0 if all(r["status"] == "ok" for r in results) else 1


def _cmd_push(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import push

    results = push(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("push", args.remote, results)


def _cmd_pull(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import pull

    results = pull(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("pull", args.remote, results)


def _cmd_evict(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import evict_local

    results = evict_local(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("evict", args.remote, results)


def _cmd_locations(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import list_locations

    rows = list_locations(db, args.file_id or None)
    print(
        format_table(
            [
                "file_id",
                "relative_path",
                "local_status",
                "remote",
                "remote_status",
                "verified_at",
            ],
            ([row[column] for column in row] for row in rows),
        )
    )
    return 0


def _print_sync_results(action: str, remote: str, results: list[dict[str, Any]]) -> int:
    print(
        format_table(
            ["file_id", "relative_path", "status", "error"],
            (
                [
                    r.get("file_id", ""),
                    r["relative_path"],
                    r["status"],
                    r.get("error") or "",
                ]
                for r in results
            ),
        )
    )
    errors = sum(1 for r in results if r["status"] == "error")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = ", ".join(
        f"{status}: {count}" for status, count in sorted(counts.items())
    )
    print(f"{action} {remote}: {summary}")
    return 1 if errors else 0
