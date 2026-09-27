"""Source acquisition helpers for the NCBI Datasets adapter: dataset discovery and ZIP safety."""

from __future__ import annotations

import io
import os
import shutil
import stat
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from operon.config import Project
from operon.errors import ConflictError, ValidationError
from operon.utils import atomic_copy, sha256_file

from ._ncbi_model import (
    ACCESSION_RE,
    DatasetAsset,
    SourceBundle,
    _canonical_accession,
    _deduplicate_reports,
    _extract_metadata,
    _read_report_file,
    _read_report_handle,
    _split_accession,
)


def _require_disk_space(path: Path, required_bytes: int, action: str) -> None:
    """Fail before a large write when the target filesystem is clearly full."""

    path = Path(path)
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    # Keep a small reserve for SQLite, metadata exports and filesystem
    # bookkeeping.  This is intentionally fixed rather than proportional so
    # multi-gigabyte genomes do not receive an excessive safety multiplier.
    reserve = 64 * 1024 * 1024
    needed = max(0, int(required_bytes)) + reserve
    if free < needed:
        raise ValidationError(
            f"insufficient space to {action} on filesystem containing {existing}: "
            f"need about {_format_bytes(needed)}, only {_format_bytes(free)} available. "
            "Free space, reduce --batch-size/--include content, or use "
            "--no-preserve-source when the original package is already archived elsewhere."
        )


def _no_space_error(path: Path, action: str, exc: OSError) -> ValidationError:
    existing = Path(path)
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free = shutil.disk_usage(existing).free
        available = f" ({_format_bytes(free)} currently available)"
    except OSError:
        available = ""
    return ValidationError(
        f"filesystem ran out of space while attempting to {action} at {existing}{available}. "
        "The NCBI adapter processes one batch at a time; free space, reduce --batch-size or "
        "download fewer --include file types, then rerun (completed batches are idempotent)."
    )


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover


def _local_zip_entry_names(path: Path, limit: int = 200) -> list[str]:
    """List local-file-header entries even when the central directory is absent."""
    import struct

    try:
        data = path.read_bytes()[: 16 * 1024 * 1024]
    except OSError:
        return []
    names: list[str] = []
    offset = 0
    while offset + 30 <= len(data) and len(names) < limit:
        if data[offset:offset + 4] != b"PK\x03\x04":
            break
        try:
            (_sig, _version, _flags, _method, _mtime, _mdate,
             _crc, comp_size, _uncomp_size, name_len, extra_len) = struct.unpack_from(
                "<IHHHHHIIIHH", data, offset
            )
        except struct.error:  # pragma: no cover
            break
        start = offset + 30
        end = start + name_len
        if end > len(data):
            break
        names.append(data[start:end].decode("utf-8", errors="replace"))
        if comp_size == 0xFFFFFFFF:
            break
        offset = end + extra_len + comp_size
    return names


def _zip_package_diagnostic(path: Path, accessions: Sequence[str]) -> tuple[bool, str]:
    """Return (is_retryable, human-readable diagnostic) for a bad ZIP payload."""
    entries = _local_zip_entry_names(path)
    names = [name.lower() for name in entries]
    has_report = any(
        name.endswith(("assembly_data_report.jsonl", "assembly_data_report.json",
                       "dataset_report.jsonl", "dataset_report.json"))
        for name in names
    )
    has_data = any(name.startswith("ncbi_dataset/data/") for name in names)
    joined = ",".join(accessions)
    if entries and not has_report and not has_data:
        return False, (
            f"NCBI returned an empty/README-only package for accession(s) {joined}; "
            f"the accession may be invalid, withdrawn, or unavailable "
            f"(local ZIP entries: {', '.join(entries[:5])})"
        )
    if entries:
        return True, (
            f"ZIP payload is truncated or has no central directory for accession(s) {joined}; "
            f"local entries start with: {', '.join(entries[:5])}"
        )
    return True, (
        f"ZIP payload for accession(s) {joined} has no recognizable ZIP content; "
        f"the server may have returned a transient error page"
    )


def load_dataset_reports(root: str | Path, direct_file: Path | None = None) -> list[dict[str, Any]]:
    root = Path(root)
    if root.is_file() and zipfile.is_zipfile(root):
        reports: list[dict[str, Any]] = []
        with zipfile.ZipFile(root) as archive:
            infos = _validated_zip_infos(archive)
            exact_names = {
                "assembly_data_report.jsonl",
                "assembly_data_report.json",
                "dataset_report.jsonl",
                "dataset_report.json",
            }
            candidates = [info for info in infos if PurePosixPath(info.filename).name in exact_names]
            if not candidates:
                candidates = [
                    info for info in infos
                    if info.filename.lower().endswith(".jsonl")
                       and "sequence_report" not in PurePosixPath(info.filename).name
                ]
            for info in candidates:
                with archive.open(info) as raw_handle:
                    text_handle = io.TextIOWrapper(raw_handle, encoding="utf-8-sig")
                    reports.extend(_read_report_handle(text_handle, f"{root}!/{info.filename}"))
        return _deduplicate_reports(reports)

    candidates: list[Path] = []
    if direct_file and direct_file.exists() and direct_file.suffix.lower() not in {".zip"}:
        candidates.append(direct_file)
    if root.is_file():
        candidates.append(root)
    elif root.is_dir():
        exact_names = {
            "assembly_data_report.jsonl",
            "assembly_data_report.json",
            "dataset_report.jsonl",
            "dataset_report.json",
        }
        candidates.extend(path for path in root.rglob("*") if path.is_file() and path.name in exact_names)
        if not candidates:
            candidates.extend(path for path in root.rglob("*.jsonl") if "sequence_report" not in path.name)
    unique_candidates = list(dict.fromkeys(path.resolve() for path in candidates))
    reports: list[dict[str, Any]] = []
    for path in unique_candidates:
        reports.extend(_read_report_file(path))
    return _deduplicate_reports(reports)


def discover_dataset_assets(
        root: str | Path,
        reports: Sequence[dict[str, Any]],
        source_label: str,
) -> list[DatasetAsset]:
    root = Path(root)
    report_accessions = [
        _extract_metadata(report)["accession"] for report in reports
        if _extract_metadata(report)["accession"]
    ]
    if root.is_file() and zipfile.is_zipfile(root):
        assets: list[DatasetAsset] = []
        with zipfile.ZipFile(root) as archive:
            for info in _validated_zip_infos(archive):
                if info.is_dir():
                    continue
                member_path = Path(*PurePosixPath(info.filename).parts)
                role = _asset_role(member_path)
                if role is None:
                    continue
                accession = _accession_from_path(member_path)
                if not accession and len(report_accessions) == 1:
                    accession = report_accessions[0]
                if not accession:
                    continue
                assets.append(DatasetAsset(
                    path=None,
                    accession=accession,
                    role=role,
                    source_url=f"ncbi-datasets:{source_label}:{info.filename}",
                    archive_path=root,
                    archive_member=info.filename,
                    size_bytes=info.file_size,
                ))
        return assets
    if not root.is_dir():
        return []
    assets: list[DatasetAsset] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        role = _asset_role(path)
        if role is None:
            continue
        accession = _accession_from_path(path)
        if not accession and len(report_accessions) == 1:
            accession = report_accessions[0]
        if not accession:
            continue
        assets.append(DatasetAsset(
            path=path,
            accession=accession,
            role=role,
            source_url=f"ncbi-datasets:{source_label}:{path.relative_to(root).as_posix()}",
            size_bytes=path.stat().st_size,
        ))
    return assets


def _asset_role(path: Path) -> str | None:
    name = path.name.lower()
    if name in {"assembly_data_report.jsonl", "assembly_data_report.json", "dataset_catalog.json"}:
        return None
    if "sequence_report" in name or "assembly_report" in name:
        return "assembly_report"
    if name.endswith((".gff", ".gff3", ".gff.gz", ".gff3.gz")):
        return "annotation_gff3"
    if name.endswith((".faa", ".faa.gz")) and ("protein" in name or name == "protein.faa"):
        return "protein_fasta"
    if "cds" in name and name.endswith((".fna", ".fa", ".fasta", ".fna.gz", ".fa.gz", ".fasta.gz")):
        return "cds_fasta"
    if name.endswith((".fna", ".fa", ".fasta", ".fna.gz", ".fa.gz", ".fasta.gz")):
        if any(token in name for token in ("rna", "cds", "protein")):
            return None
        return "genome_fasta"
    return None


def _accession_from_path(path: Path) -> str:
    # NCBI packages embed the accession in member filenames followed by an
    # underscore suffix (e.g. "GCF_000001405.40_GRCh38.p14_genomic.fna"), where
    # ACCESSION_RE can only match the unversioned base.  A versioned match in
    # any path part (typically the per-accession directory) is more specific
    # and wins over such truncated matches.
    fallback = ""
    for part in reversed(path.parts):
        match = ACCESSION_RE.search(part)
        if not match:
            continue
        value = _canonical_accession(match.group(0))
        if _split_accession(value)[1] is not None:
            return value
        if not fallback:
            fallback = value
    return fallback


def _open_source(path: Path, project: Project, preserve: bool, label: str | None = None) -> SourceBundle:
    path = path.resolve()
    if not path.exists():
        raise ValidationError(f"NCBI Datasets input does not exist: {path}")
    preserved: Path | None = None
    if preserve and path.is_file():
        preserved = _preserve_source(path, project)
    if path.is_dir():
        return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)
    if zipfile.is_zipfile(path):
        # Validate archive paths eagerly, but deliberately do not extract the
        # package.  Reports and assets are streamed from the ZIP later.
        with zipfile.ZipFile(path) as archive:
            _validated_zip_infos(archive)
        return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)
    return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)


def _preserve_source(path: Path, project: Project, *, move: bool = False) -> Path:
    digest = sha256_file(path)
    suffix = "".join(path.suffixes[-2:]) if len(path.suffixes) > 1 else path.suffix
    suffix = suffix or ".dat"
    target = project.raw_root / "metadata" / "ncbi_datasets" / f"{digest}{suffix}"
    if target.exists():
        if sha256_file(target) != digest:
            raise ConflictError(f"preserved NCBI source {target} has unexpected content")
        if move and path != target:
            path.unlink(missing_ok=True)
        return target
    if move:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
    else:
        _require_disk_space(target.parent, path.stat().st_size, "preserve NCBI source package")
        atomic_copy(path, target)
    return target


def _validated_zip_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Validate member paths/symlinks without materializing archive content."""

    infos = archive.infolist()
    for info in infos:
        _validate_zip_info(info)
    return infos


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    member = PurePosixPath(info.filename)
    if member.is_absolute() or ".." in member.parts:
        raise ValidationError(f"unsafe path in NCBI dataset ZIP: {info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValidationError(f"symbolic link is not allowed in NCBI dataset ZIP: {info.filename}")


def _safe_extract_zip(path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        for info in _validated_zip_infos(archive):
            member = PurePosixPath(info.filename)
            target = (destination / Path(*member.parts)).resolve()
            if destination != target and destination not in target.parents:  # pragma: no cover
                raise ValidationError(f"unsafe path in NCBI dataset ZIP: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
