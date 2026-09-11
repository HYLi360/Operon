"""Query-cache adapter for the TimeTree REST API (divergence-time priors).

TimeTree terms of use forbid bulk mirroring or redistribution of the data, so
this adapter only caches the raw responses of the exact queries the user made,
inside the project directory.  Cache records keep the request URL, the fetch
time and the verbatim response body, so replayed queries stay auditable and
parsers can be corrected against the real payload later.

Any use of TimeTree data must cite: Kumar S, et al. 2022, Mol Biol Evol,
https://doi.org/10.1093/molbev/msac174
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from operon.errors import ValidationError
from operon.utils import atomic_write_text, now_iso

TIMETREE_API = "https://timetree.temple.edu/api"
TIMETREE_CITATION = (
    "TimeTree v5: Kumar S, et al. 2022, Mol Biol Evol, "
    "https://doi.org/10.1093/molbev/msac174"
)
TIMETREE_SOURCE_LABEL = "TimeTree v5 (Kumar et al. 2022, MBE)"

RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

CALIBRATION_COLUMNS = [
    "node_label", "taxa", "taxon_ids", "age_median", "ci_low", "ci_high",
    "study_count", "source", "queried_at", "cache_file",
]

_ID_KEYS = ("taxon_id", "taxid", "taxonid", "ncbi_id", "id")
_NAME_KEYS = ("scientific_name", "taxon_name", "name", "taxon")
_RANK_KEYS = ("rank", "taxon_rank", "taxonomic_rank")
_AGE_KEYS = ("precomputed_age", "median_time", "age", "time", "median_age", "median")
_CI_LOW_KEYS = ("precomputed_ci_low", "ci_low", "ci_lower", "confidence_low",
                "confidence_interval_low", "ci95_low", "lower_ci")
_CI_HIGH_KEYS = ("precomputed_ci_high", "ci_high", "ci_upper", "confidence_high",
                 "confidence_interval_high", "ci95_high", "upper_ci")
_STUDY_KEYS = ("all_total", "study_count", "studies", "n_studies", "total_studies")


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace(" ", "_").replace("-", "_")


def _flatten_dicts(value: Any) -> list[dict[str, Any]]:
    """Collect every nested JSON object with normalized keys, root first."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            found.append({_normalize_key(k): v for k, v in node.items()})
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _first_number(record: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = record.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_text(record: dict[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _truncated(body: str, limit: int = 500) -> str:
    body = body.strip()
    return body if len(body) <= limit else body[:limit] + "...(truncated)"


class TimeTreeClient:
    """Serial, query-cached client for the TimeTree REST API.

    ``session`` is injectable for tests; ``cache_dir=None`` disables caching.
    ``delay`` is the pause after each real network request (cache hits are
    free) so small batches stay polite to the public endpoint.
    """

    def __init__(
            self,
            cache_dir: str | Path | None = None,
            *,
            base_url: str = TIMETREE_API,
            timeout: float = 30.0,
            retries: int = 3,
            delay: float = 0.5,
            refresh: bool = False,
            session: Any = None,
    ) -> None:
        if timeout <= 0:
            raise ValidationError("TimeTree timeout must be positive")
        if not 1 <= retries <= 10:
            raise ValidationError("TimeTree retries must be between 1 and 10")
        if delay < 0:
            raise ValidationError("TimeTree delay must be nonnegative")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.delay = float(delay)
        self.refresh = refresh
        self._session = session
        self._own_session = session is None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self._own_session and self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "TimeTreeClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def session(self) -> Any:
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers["User-Agent"] = (
                "Operon-TimeTree-adapter (research; per-query cache only)"
            )
        return self._session

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def fetch(self, url: str) -> dict[str, Any]:
        """Return the raw response body for ``url``, from cache or network."""
        cache_path = self._cache_path(url)
        if cache_path is not None and cache_path.exists() and not self.refresh:
            try:
                record = json.loads(cache_path.read_text(encoding="utf-8"))
                body = record["body"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValidationError(
                    f"TimeTree cache record is unreadable: {cache_path}: {exc}"
                ) from exc
            return {
                "body": body,
                "cache_file": cache_path,
                "queried_at": str(record.get("retrieved_at") or ""),
                "from_cache": True,
            }

        import requests
        last_error: Exception | None = None
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.delay * (2 ** attempt) if self.delay else 0)
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                last_error = exc
                continue
            if response.status_code in RETRYABLE_HTTP_STATUS:
                last_error = ValidationError(f"HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise ValidationError(
                    f"TimeTree request failed: {url}: HTTP {response.status_code}: "
                    f"{_truncated(response.text)}"
                )
            record = {
                "url": url,
                "retrieved_at": now_iso(),
                "http_status": response.status_code,
                "body": response.text,
            }
            if cache_path is not None:
                atomic_write_text(
                    cache_path,
                    json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
            if self.delay:
                time.sleep(self.delay)
            return {
                "body": response.text,
                "cache_file": cache_path,
                "queried_at": record["retrieved_at"],
                "from_cache": False,
            }
        raise ValidationError(
            f"TimeTree request failed after {self.retries} attempt(s): {url}: {last_error}"
        ) from last_error

    def _fetch_json(self, url: str) -> tuple[Any, dict[str, Any]]:
        result = self.fetch(url)
        try:
            data = json.loads(result["body"])
        except ValueError as exc:
            raise ValidationError(
                f"TimeTree returned non-JSON content for {url}: "
                f"{_truncated(result['body'])}"
            ) from exc
        return data, result

    def resolve_taxon(self, name: str) -> list[dict[str, Any]]:
        """Resolve a scientific name to candidate NCBI taxonomy records."""
        name = str(name).strip()
        if not name:
            raise ValidationError("TimeTree taxon name must not be empty")
        url = f"{self.base_url}/taxon/{quote(name, safe='')}"
        data, _ = self._fetch_json(url)
        records = _flatten_dicts(data)
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for record in records:
            taxon_id = _first_number(record, _ID_KEYS)
            label = _first_text(record, _NAME_KEYS)
            if taxon_id is None or label is None:
                continue
            taxon_id_int = int(taxon_id)
            if taxon_id_int <= 0 or taxon_id_int in seen:
                continue
            seen.add(taxon_id_int)
            candidates.append({
                "taxon_id": taxon_id_int,
                "scientific_name": label,
                "rank": _first_text(record, _RANK_KEYS) or "",
            })
        if not candidates:
            raise ValidationError(
                f"TimeTree found no taxon for {name!r}; raw response: {_truncated(json.dumps(data, default=str))}"
            )
        return candidates

    def _divergence(self, url: str, label: str) -> dict[str, Any]:
        data, result = self._fetch_json(url)
        summary: dict[str, Any] | None = None
        for record in _flatten_dicts(data):
            if _first_number(record, _AGE_KEYS) is not None:
                summary = record
                break
        if summary is None:
            raise ValidationError(
                f"TimeTree returned no divergence time for {label}; "
                f"raw response: {_truncated(json.dumps(data, default=str))}"
            )
        names = [
            value for key in ("scientific_name_a", "scientific_name_b")
            if (value := summary.get(key))
        ]
        if not names:
            names = [
                str(value) for key, value in summary.items()
                if key.startswith("scientific_name") and value
            ]
        return {
            "label": label,
            "scientific_names": names,
            "age_median": _first_number(summary, _AGE_KEYS),
            "ci_low": _first_number(summary, _CI_LOW_KEYS),
            "ci_high": _first_number(summary, _CI_HIGH_KEYS),
            "study_count": _first_number(summary, _STUDY_KEYS),
            "adjusted_age": _first_number(summary, ("adjusted_age",)),
            "url": url,
            "cache_file": result["cache_file"],
            "queried_at": result["queried_at"],
            "from_cache": result["from_cache"],
        }

    def pairwise(self, id_a: int, id_b: int) -> dict[str, Any]:
        """Divergence-time summary for two NCBI taxonomy IDs."""
        id_a, id_b = sorted((int(id_a), int(id_b)))
        if id_a <= 0 or id_a == id_b:
            raise ValidationError("TimeTree pairwise requires two different positive NCBI IDs")
        url = f"{self.base_url}/pairwise/{id_a}/{id_b}/summaryjson"
        result = self._divergence(url, f"pair {id_a}/{id_b}")
        result["taxon_ids"] = [id_a, id_b]
        return result

    def mrca(self, ids: Sequence[int]) -> dict[str, Any]:
        """MRCA divergence-time summary for N NCBI taxonomy IDs."""
        taxon_ids = sorted({int(value) for value in ids})
        if len(taxon_ids) < 2 or taxon_ids[0] <= 0:
            raise ValidationError("TimeTree mrca requires at least two positive NCBI IDs")
        joined = "+".join(str(value) for value in taxon_ids)
        url = f"{self.base_url}/mrca/id/{joined}/summaryjson"
        result = self._divergence(url, f"MRCA of {joined}")
        result["taxon_ids"] = taxon_ids
        return result

    def timeline(self, taxon_id: int) -> list[dict[str, Any]]:
        """Node timetable from ``taxon_id`` back to the last universal ancestor."""
        taxon_id = int(taxon_id)
        if taxon_id <= 0:
            raise ValidationError("TimeTree timeline requires a positive NCBI ID")
        url = f"{self.base_url}/timeline/{taxon_id}"
        result = self.fetch(url)
        reader = csv.DictReader(io.StringIO(result["body"]))
        rows = [dict(row) for row in reader]
        if not rows:
            raise ValidationError(
                f"TimeTree returned no timeline nodes for taxon {taxon_id}; "
                f"raw response: {_truncated(result['body'])}"
            )
        for row in rows:
            row["_queried_at"] = result["queried_at"]
            row["_cache_file"] = str(result["cache_file"]) if result["cache_file"] else ""
        return rows

    def build_calibrations(
            self,
            taxa: Sequence[tuple[str, int]],
            *,
            pairs: bool = False,
    ) -> list[dict[str, Any]]:
        """Calibration TSV rows for a resolved taxon set.

        ``taxa`` holds ``(name, taxon_id)`` pairs.  The default mode queries
        one MRCA for the whole set; ``pairs=True`` queries every pair and
        emits one row per pair.
        """
        resolved = [(str(name), int(taxon_id)) for name, taxon_id in taxa]
        if len(resolved) < 2:
            raise ValidationError("TimeTree calibrations require at least two taxa")

        def row(label: str, members: list[tuple[str, int]], summary: dict[str, Any]) -> dict[str, Any]:
            cache_file = summary["cache_file"]
            return {
                "node_label": label,
                "taxa": ",".join(name for name, _ in members),
                "taxon_ids": ",".join(str(taxon_id) for _, taxon_id in members),
                "age_median": summary["age_median"],
                "ci_low": summary["ci_low"],
                "ci_high": summary["ci_high"],
                "study_count": summary["study_count"],
                "source": TIMETREE_SOURCE_LABEL,
                "queried_at": summary["queried_at"],
                "cache_file": str(cache_file) if cache_file else "",
            }

        if not pairs:
            summary = self.mrca([taxon_id for _, taxon_id in resolved])
            return [row(f"mrca({len(resolved)} taxa)", resolved, summary)]
        rows = []
        for index in range(len(resolved)):
            for other in range(index + 1, len(resolved)):
                members = [resolved[index], resolved[other]]
                id_a, id_b = sorted(taxon_id for _, taxon_id in members)
                summary = self.pairwise(id_a, id_b)
                rows.append(row(f"pair({id_a},{id_b})", members, summary))
        return rows
