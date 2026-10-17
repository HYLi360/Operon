#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Download-consumer edge paths split out of ``test_ncbi_coverage_gaps``.

The parent module keeps the adapter-level error paths; the aiohttp and
parallel-download corners live here so both modules stay inside the
file-length budget tracked by Codacy.
"""

from __future__ import annotations

import asyncio
import errno
import queue
import threading
from typing import Any

import pytest
import requests

from operon.adapters import ncbi_datasets as ncbi
from operon.errors import ValidationError
from tests.unit.test_ncbi_coverage_gaps import (
    _aiohttp_kwargs,
    _install_fake_aiohttp,
    _ScriptedSession,
    _SyncResponse,
)


def test_download_closes_response_and_reraises_non_retryable_http_error(tmp_path):
    """A non-retryable HTTP status closes the response before propagating."""
    primary = _SyncResponse(403)
    session = _ScriptedSession([primary])
    with pytest.raises(requests.HTTPError, match="HTTP 403"):
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"],
            tmp_path / "forbidden.zip",
            session=session,
            max_retries=0,
        )
    assert primary.closed is True
    assert len(session.calls) == 1
    assert not (tmp_path / "forbidden.zip").exists()


def test_aiohttp_download_classifies_enospc_from_failed_get_as_no_space(
    tmp_path, monkeypatch
):
    """ENOSPC raised before streaming is fatal, never retried as transient."""
    error = OSError(errno.ENOSPC, "No space left on device")
    _install_fake_aiohttp(monkeypatch, [error])
    destination = tmp_path / "full.zip"
    with pytest.raises(ValidationError, match="ran out of space") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(destination)))
    assert "download NCBI dataset package" in str(caught.value)
    assert not destination.exists()


def test_parallel_download_skips_empty_polls_before_the_sentinel(tmp_path, monkeypatch):
    """The consumer keeps polling while the producer has nothing to enqueue."""
    empty_polls: list[bool] = []
    seen_empty = threading.Event()

    async def idle_runner(**kwargs: Any) -> None:
        completed_queue = kwargs["completed_queue"]
        original_get = completed_queue.get

        def observing_get(*args: Any, **inner_kwargs: Any) -> Any:
            try:
                return original_get(*args, **inner_kwargs)
            except queue.Empty:
                empty_polls.append(True)
                seen_empty.set()
                raise

        completed_queue.get = observing_get
        # Only return once the consumer has survived at least one timed
        # get() timeout; the runner's sentinel then ends the consumer loop.
        assert seen_empty.wait(5.0)

    monkeypatch.setattr(
        "operon.adapters._ncbi_download._download_batches_async", idle_runner
    )
    consumed: list[Any] = []
    assert (
        ncbi.download_ncbi_datasets_parallel(
            [["GCF_000001405.40"]],
            tmp_path,
            max_workers=1,
            on_complete=lambda batch, path: consumed.append((batch, path)),
        )
        == []
    )
    assert empty_polls and consumed == []


def test_parallel_download_retries_sentinel_put_when_the_queue_is_full(
    tmp_path, monkeypatch
):
    """The producer's last-resort sentinel retries while a batch sits uncollected."""
    batch_a = ["GCF_000000001.1"]
    batch_b = ["GCF_000000002.2"]
    path_a = tmp_path / "a.zip"
    path_b = tmp_path / "b.zip"
    path_a.write_bytes(b"zip-a")
    path_b.write_bytes(b"zip-b")
    collect_started = threading.Event()
    full_observed = threading.Event()
    full_polls: list[bool] = []

    async def scripted_runner(**kwargs: Any) -> None:
        completed_queue = kwargs["completed_queue"]
        original_put = completed_queue.put

        def observing_put(item: Any, *args: Any, **inner_kwargs: Any) -> Any:
            try:
                return original_put(item, *args, **inner_kwargs)
            except queue.Full:
                full_polls.append(True)
                full_observed.set()
                raise

        completed_queue.put = observing_put
        original_put((batch_a, path_a, None))
        # Wait until the consumer is blocked inside on_complete(batch_a): only
        # then is the bounded queue empty again for the second finished batch.
        assert collect_started.wait(5.0)
        original_put((batch_b, path_b, None))

    def on_complete(batch: Any, zip_path: Any) -> None:
        if batch == batch_a:
            collect_started.set()
            # Return only after the runner observed the full queue, so the
            # retried sentinel put is the one that finally gets through.
            assert full_observed.wait(5.0)

    monkeypatch.setattr(
        "operon.adapters._ncbi_download._download_batches_async", scripted_runner
    )
    completed = ncbi.download_ncbi_datasets_parallel(
        [batch_a, batch_b],
        tmp_path,
        max_workers=1,
        on_complete=on_complete,
    )
    assert full_polls
    assert completed == [path_a, path_b]
