"""Online acquisition for the NCBI Datasets adapter: requests/aiohttp downloads and Entrez fallback."""

from __future__ import annotations

import asyncio
import errno
import os
import queue
import random
import ssl
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from operon import __version__
from operon.errors import ValidationError

from ._ncbi_model import (
    DEFAULT_INCLUDES,
    INCLUDE_TYPES,
    _canonical_accession,
    _DownloadCancelled,
    _integer_or_none,
    _RetryableDownloadError,
)
from ._ncbi_sources import _no_space_error, _require_disk_space, _zip_package_diagnostic

NCBI_DATASETS_API = "https://api.ncbi.nlm.nih.gov/datasets/v2"
NCBI_DATASETS_API_FALLBACK = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def download_ncbi_dataset(
        accessions: Sequence[str],
        destination: str | Path,
        *,
        includes: Sequence[str] = DEFAULT_INCLUDES,
        email: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        session: Any | None = None,
        max_retries: int = 4,
        retry_backoff: float = 1.0,
) -> Path:
    """Download one NCBI Datasets package with explicit SSL/network retries.

    Retries happen outside urllib3 as well: [SSL] record layer failure and
    other transport-level errors are transient in practice and should not
    force the operator to re-run the whole accession list by hand.
    """

    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - dependency installation error
        raise ValidationError("online NCBI download requires the 'requests' dependency") from exc

    canonical = [_canonical_accession(value) for value in accessions]
    if not canonical:
        raise ValidationError("no NCBI assembly accessions supplied for download")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    own_session = session is None
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=retry_backoff,
            status_forcelist=tuple(sorted(RETRYABLE_HTTP_STATUS)),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))

    last_error: Exception | None = None
    try:
        for attempt in range(max_retries + 1):
            if attempt:
                time.sleep(retry_backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5))
            try:
                return _download_ncbi_dataset_once(
                    canonical=canonical,
                    destination=destination,
                    includes=includes,
                    email=email,
                    api_key=api_key,
                    timeout=timeout,
                    session=session,
                )
            except _RetryableDownloadError as exc:
                last_error = exc
            except ssl.SSLError as exc:
                last_error = exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
            except requests.exceptions.Timeout as exc:
                last_error = exc
            except requests.exceptions.ChunkedEncodingError as exc:
                last_error = exc
    finally:
        if own_session:
            session.close()

    raise ValidationError(
        f"NCBI Datasets download failed after {max_retries + 1} attempt(s): {last_error}"
    ) from last_error


def _download_headers(email: str | None, api_key: str | None) -> dict[str, str]:
    """Common request headers for the NCBI Datasets package endpoint."""
    headers = {
        "Accept": "application/zip",
        "User-Agent": f"Operon/{__version__} NCBI-Datasets-Adapter ({email or 'email-not-provided'})",
    }
    if api_key:
        headers["api-key"] = api_key
    return headers


def _download_params(includes: Sequence[str]) -> list[tuple[str, str]]:
    """Include-type query parameters for the package endpoint."""
    return [("include_annotation_type", INCLUDE_TYPES[name]) for name in includes]


def _response_content_length(headers: Any) -> int | None:
    """Best-effort Content-Length parsing; None when absent or unreadable."""
    try:
        return int((headers or {}).get("Content-Length", "") or 0) or None
    except (TypeError, ValueError):
        return None


def _zip_download_precheck(destination: Path, content_length: int | None) -> tuple[int, str]:
    """Reserve disk space and open the staging temp file for a ZIP download."""
    if content_length:
        _require_disk_space(destination.parent, content_length, "download NCBI dataset package")
    return tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))


def _finalize_zip_download(tmp_name: str, destination: Path, canonical: Sequence[str]) -> None:
    """Validate the staged payload as a ZIP and promote it to the destination."""
    if not zipfile.is_zipfile(tmp_name):
        retryable, detail = _zip_package_diagnostic(Path(tmp_name), canonical)
        if retryable:
            raise _RetryableDownloadError(detail) from None
        raise ValidationError(detail) from None
    os.replace(tmp_name, destination)


def _discard_zip_tempfile(tmp_name: str) -> None:
    """Best-effort removal of the staging temp file after a failed download."""
    try:
        os.unlink(tmp_name)
    except OSError:
        pass


def _stream_zip_to_destination(
        destination: Path,
        *,
        chunks: Iterable[bytes],
        content_length: int | None,
        canonical: Sequence[str],
        retryable_stream_errors: tuple[type[BaseException], ...] = (),
) -> Path:
    """Write response chunks to the destination as a validated ZIP package."""
    fd, tmp_name = _zip_download_precheck(destination, content_length)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                if chunk:
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _finalize_zip_download(tmp_name, destination, canonical)
    except BaseException as exc:
        _discard_zip_tempfile(tmp_name)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise _no_space_error(destination.parent, "download NCBI dataset package", exc) from exc
        if isinstance(exc, retryable_stream_errors):
            raise _RetryableDownloadError(str(exc)) from exc
        raise
    return destination


def _request_zip_response(
        session: Any,
        *,
        joined: str,
        params: Sequence[tuple[str, str]],
        headers: dict[str, str],
        timeout: float,
) -> Any:
    """GET the package from the primary API base, then the fallback on 404/410."""
    import requests

    response = None
    last_error: Exception | None = None
    for base in (NCBI_DATASETS_API, NCBI_DATASETS_API_FALLBACK):
        url = f"{base}/genome/accession/{quote(joined, safe=',._')}/download"
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                stream=True,
                timeout=(30.0, timeout),
            )
            if response is None:
                raise _RetryableDownloadError("HTTP client returned no response")
            if response.status_code in {404, 410} and base == NCBI_DATASETS_API:
                response.close()
                response = None
                continue
            if response.status_code in RETRYABLE_HTTP_STATUS:
                raise _RetryableDownloadError(f"HTTP {response.status_code} from NCBI Datasets")
            try:
                response.raise_for_status()
            except BaseException:
                response.close()
                raise
            break
        except _RetryableDownloadError as exc:
            last_error = exc
            if response is not None:
                response.close()
                response = None
            if base == NCBI_DATASETS_API_FALLBACK:
                raise
        except (ssl.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_error = exc
            if response is not None:  # pragma: no cover
                response.close()
                response = None
            if base == NCBI_DATASETS_API_FALLBACK:
                raise _RetryableDownloadError(str(exc)) from exc

    if response is None:  # pragma: no cover
        if last_error is None:
            last_error = ValidationError("NCBI Datasets returned no downloadable package")
        raise ValidationError(f"NCBI Datasets download failed: {last_error}") from last_error
    return response


def _download_ncbi_dataset_once(
        *,
        canonical: Sequence[str],
        destination: Path,
        includes: Sequence[str],
        email: str | None,
        api_key: str | None,
        timeout: float,
        session: Any,
) -> Path:
    """One download attempt over primary and fallback API bases."""
    import requests

    headers = _download_headers(email, api_key)
    params = _download_params(includes)
    joined = ",".join(canonical)
    response = _request_zip_response(
        session, joined=joined, params=params, headers=headers, timeout=timeout,
    )
    try:
        return _stream_zip_to_destination(
            destination,
            chunks=response.iter_content(chunk_size=1024 * 1024),
            content_length=_response_content_length(getattr(response, "headers", None)),
            canonical=canonical,
            retryable_stream_errors=(
                ssl.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ),
        )
    finally:
        response.close()


def download_ncbi_datasets_parallel(
        batches: Sequence[Sequence[str]],
        staging_dir: str | Path,
        *,
        includes: Sequence[str] = DEFAULT_INCLUDES,
        email: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        max_workers: int = 3,
        max_retries: int = 4,
        retry_backoff: float = 1.0,
        on_complete: Any,
        on_error: Any | None = None,
        cancel_event: threading.Event | None = None,
) -> list[Path]:
    """Download accession batches concurrently and consume each as it lands.

    Downloads run in a dedicated asyncio thread.  Completed batches are handed
    to `on_complete(batch, zip_path)` on the caller thread, so SQLite writes
    stay on their original connection/thread while network I/O continues in
    the background.  Failed batches are isolated: other batches keep going and
    are processed normally, then an aggregate ValidationError is raised unless
    `on_error` is provided to collect failures instead.

    ``cancel_event`` is the cooperative-cancellation hook: when the caller
    supplies an event and it is set, pending batches stop at the next
    chunk/batch boundary and this function raises ``ShutdownRequested``, the
    same exception a SIGINT/SIGTERM produces, so the caller's interrupt
    bookkeeping (run recorded as ``interrupted``) runs unchanged.  With no
    event supplied an internal one is used, exactly as before.
    """
    import signal

    from operon.shutdown import ShutdownRequested

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    sentinel = object()
    # Bound the number of finished-but-not-yet-imported ZIPs.  When the queue
    # is full the asyncio producer blocks, which also backs off the network.
    completed_queue: queue.Queue[Any] = queue.Queue(maxsize=max_workers)
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    runner_errors: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(_download_batches_async(
                batches=batches,
                staging_dir=staging_dir,
                includes=includes,
                email=email,
                api_key=api_key,
                timeout=timeout,
                max_workers=max_workers,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                completed_queue=completed_queue,
                cancel_event=cancel_event,
            ))
        except BaseException as exc:  # noqa: BLE001 - worker-thread errors are ferried to the consumer and re-raised  # pylint: disable=broad-exception-caught
            runner_errors.append(exc)
        finally:
            # Give up once the consumer has gone (error/shutdown): a blocking
            # sentinel put on the full bounded queue would deadlock this
            # thread and hang the consumer's join.
            while not cancel_event.is_set():
                try:
                    completed_queue.put(sentinel, timeout=0.2)
                    break
                except queue.Full:
                    continue

    # daemon=True is only a last-resort backstop: the finally below cancels
    # pending work and joins this thread promptly on any exit path, but the
    # interpreter must never hang on it during process teardown.
    thread = threading.Thread(target=runner, name="operon-ncbi-download", daemon=True)
    thread.start()
    completed: list[Path] = []
    failures: list[tuple[Sequence[str], Exception]] = []
    try:
        while True:
            # A caller-supplied event that is set means cooperative cancel:
            # raise the signal-style interrupt so the run is recorded as
            # interrupted, exactly as after SIGINT/SIGTERM.  The internal
            # event is only set by the finally below (after this loop has
            # exited), so this check never fires for the CLI path.  The poll
            # also keeps the consumer from blocking forever on a queue whose
            # producer side goes quiet once the event is set.
            if cancel_event.is_set():
                raise ShutdownRequested(signal.SIGINT)
            try:
                item = completed_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is sentinel:
                break
            batch, zip_path, error = item
            if error is not None:
                failures.append((batch, error))
                if on_error is not None:
                    on_error(batch, error)
                continue
            if zip_path is None:
                continue
            on_complete(batch, zip_path)
            completed.append(zip_path)
    finally:
        # Stop pending work before joining so a processing error cannot leave
        # the download thread running indefinitely.
        cancel_event.set()
        thread.join()

    if runner_errors:
        raise runner_errors[0]
    if failures and on_error is None:
        details = "\n".join(
            f"- {','.join(batch)}: {error}" for batch, error in failures[:20]
        )
        if len(failures) > 20:
            details += f"\n- ... and {len(failures) - 20} more failed batch(es)"
        raise ValidationError(
            f"{len(failures)}/{len(batches)} NCBI download batch(es) failed:\n{details}"
        )
    return completed


async def _download_batches_async(
        *,
        batches: Sequence[Sequence[str]],
        staging_dir: Path,
        includes: Sequence[str],
        email: str | None,
        api_key: str | None,
        timeout: float,
        max_workers: int,
        max_retries: int,
        retry_backoff: float,
        completed_queue: Any,
        cancel_event: Any,
) -> None:
    semaphore = asyncio.Semaphore(max_workers)

    async def one_batch(batch: Sequence[str], index: int) -> tuple[Sequence[str], Path | None, BaseException | None]:
        if cancel_event.is_set():
            return batch, None, _DownloadCancelled("cancelled")
        destination = staging_dir / f"ncbi_dataset_{index:05d}.zip"
        async with semaphore:
            if cancel_event.is_set():
                return batch, None, _DownloadCancelled("cancelled")
            try:
                await _download_batch_aiohttp(
                    batch,
                    destination,
                    includes=includes,
                    email=email,
                    api_key=api_key,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                return batch, None, exc
        return batch, destination, None

    tasks = [asyncio.create_task(one_batch(batch, index)) for index, batch in enumerate(batches)]
    try:
        for finished in asyncio.as_completed(tasks):
            batch, zip_path, error = await finished
            if error is None and zip_path is None:  # pragma: no cover
                continue
            # The consumer may have exited on error or shutdown without
            # draining the bounded queue; a plain blocking put would then
            # deadlock this thread forever (and hang process exit).
            while not cancel_event.is_set():
                try:
                    completed_queue.put((batch, zip_path, error), timeout=0.2)
                    break
                except queue.Full:
                    continue
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _interruptible_retry_sleep(seconds: float, cancel_event: Any) -> None:
    """Backoff sleep that stays responsive to shutdown cancellation."""
    remaining = seconds
    while remaining > 0:
        if cancel_event.is_set():
            raise _DownloadCancelled()
        step = min(0.2, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _fetch_zip_response(session: Any, urls: Sequence[str], params: Sequence[tuple[str, str]]) -> Any:
    """GET the package from the primary API base, then the fallback on 404/410."""
    import aiohttp

    response = None
    for index, url in enumerate(urls):
        try:
            response = await session.get(url, params=params)
            if response.status in {404, 410} and index == 0:
                response.release()
                response = None
                continue
            if response.status in RETRYABLE_HTTP_STATUS:
                raise _RetryableDownloadError(f"HTTP {response.status} from NCBI Datasets")
            response.raise_for_status()
            break
        except _RetryableDownloadError:
            if response is not None:  # pragma: no branch
                response.release()
                response = None
            if index == len(urls) - 1:
                raise
        except (aiohttp.ClientSSLError, aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError, asyncio.TimeoutError, ssl.SSLError) as exc:
            if response is not None:  # pragma: no cover
                response.release()
                response = None
            if index == len(urls) - 1:
                raise _RetryableDownloadError(str(exc)) from exc
    if response is None:  # pragma: no cover
        raise _RetryableDownloadError("NCBI Datasets returned no downloadable package")
    return response


async def _astream_zip_to_destination(
        destination: Path,
        *,
        response: Any,
        content_length: int | None,
        canonical: Sequence[str],
        cancel_event: Any,
) -> Path:
    """Write an aiohttp response body to the destination as a validated ZIP."""
    fd, tmp_name = _zip_download_precheck(destination, content_length)
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                if cancel_event.is_set():
                    raise _DownloadCancelled()
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _finalize_zip_download(tmp_name, destination, canonical)
    except BaseException as exc:
        _discard_zip_tempfile(tmp_name)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise _no_space_error(destination.parent, "download NCBI dataset package", exc) from exc
        raise
    return destination


def _classify_attempt_failure(exc: BaseException, destination: Path) -> BaseException:
    """Map one failed attempt to its retry error; fatal errors are raised."""
    import aiohttp

    if isinstance(exc, _DownloadCancelled):
        raise exc
    if isinstance(exc, _RetryableDownloadError):
        return exc
    if isinstance(exc, (aiohttp.ClientSSLError, aiohttp.ClientConnectionError,
                        aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError,
                        asyncio.TimeoutError, ssl.SSLError)):
        return exc
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            raise _no_space_error(destination.parent, "download NCBI dataset package", exc) from exc
        return exc
    if isinstance(exc, aiohttp.ClientResponseError):
        if exc.status in RETRYABLE_HTTP_STATUS:
            return exc
        raise ValidationError(f"NCBI Datasets download failed: {exc}") from exc
    raise exc


async def _download_batch_aiohttp(
        accessions: Sequence[str],
        destination: Path,
        *,
        includes: Sequence[str],
        email: str | None,
        api_key: str | None,
        timeout: float,
        max_retries: int,
        retry_backoff: float,
        cancel_event: Any,
) -> Path:
    """One concurrent download task with SSL/transient-error retries."""
    import aiohttp

    canonical = [_canonical_accession(value) for value in accessions]
    if not canonical:
        raise ValidationError("no NCBI assembly accessions supplied for download")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = _download_headers(email, api_key)
    params = _download_params(includes)
    joined = ",".join(canonical)
    urls = [
        f"{base}/genome/accession/{quote(joined, safe=',._')}/download"
        for base in (NCBI_DATASETS_API, NCBI_DATASETS_API_FALLBACK)
    ]
    client_timeout = aiohttp.ClientTimeout(total=None, connect=30.0, sock_read=timeout)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if cancel_event.is_set():
            raise _DownloadCancelled()
        if attempt:
            await _interruptible_retry_sleep(
                retry_backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5),
                cancel_event,
            )
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=client_timeout) as session:
                response = await _fetch_zip_response(session, urls, params)
                try:
                    return await _astream_zip_to_destination(
                        destination,
                        response=response,
                        content_length=_response_content_length(response.headers),
                        canonical=canonical,
                        cancel_event=cancel_event,
                    )
                finally:
                    response.release()
        except BaseException as exc:
            last_error = _classify_attempt_failure(exc, destination)

    raise ValidationError(
        f"NCBI Datasets download failed after {max_retries + 1} attempt(s): {last_error}"
    ) from last_error


def fetch_entrez_assembly_reports(
        accessions: Sequence[str], *, email: str | None, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Use Biopython Entrez as a metadata fallback for unusual packages."""

    if not email:
        raise ValidationError("Biopython Entrez fallback requires --email or NCBI_EMAIL")
    try:
        from Bio import Entrez
    except ImportError as exc:  # pragma: no cover - dependency installation error
        raise ValidationError("Entrez fallback requires the 'biopython' dependency") from exc
    Entrez.email = email
    Entrez.api_key = api_key
    Entrez.tool = "Operon"
    reports: list[dict[str, Any]] = []
    for accession in accessions:
        canonical = _canonical_accession(accession)
        with Entrez.esearch(db="assembly", term=f"{canonical}[Assembly Accession]", retmax=2) as handle:
            found = Entrez.read(handle)
        ids = list(found.get("IdList") or [])
        if not ids:
            continue
        with Entrez.esummary(db="assembly", id=ids[0], report="full") as handle:
            summary = Entrez.read(handle, validate=False)
        documents = summary.get("DocumentSummarySet", {}).get("DocumentSummary", [])
        if not documents:
            continue
        doc = documents[0]
        synonym = doc.get("Synonym") or {}
        reports.append({
            "accession": str(doc.get("AssemblyAccession") or canonical),
            "organism": {
                "organismName": str(doc.get("SpeciesName") or ""),
                "taxId": _integer_or_none(doc.get("Taxid")),
            },
            "assemblyInfo": {
                "assemblyLevel": str(doc.get("AssemblyStatus") or ""),
                "assemblyName": str(doc.get("AssemblyName") or ""),
                "biosample": {"accession": str(doc.get("BioSampleAccn") or "")},
                "bioprojectAccession": str(doc.get("BioProjectAccn") or ""),
                "pairedAssembly": {
                    "accession": str(synonym.get("Genbank") or synonym.get("RefSeq") or "")
                },
                "refseqCategory": str(doc.get("RefSeq_category") or ""),
                "releaseDate": str(doc.get("SubmissionDate") or ""),
                "submitter": str(doc.get("SubmitterOrganization") or ""),
            },
            "sourceDatabase": "SOURCE_DATABASE_REFSEQ" if canonical.startswith("GCF_") else "SOURCE_DATABASE_GENBANK",
        })
    return reports
