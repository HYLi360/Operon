"""Sphinx configuration for the English Operon documentation.

This tree is the parent Read the Docs project ``operonproject``, published at
``https://operonproject.readthedocs.io/en/latest/``; the Chinese translation
project builds ``docs/zh/`` from ``docs/zh/.readthedocs.yaml``. Shared settings
live in ``docs/conf_common.py``; see
``contributor/documentation-deployment.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conf_common import apply_shared_settings  # noqa: E402

apply_shared_settings(
    globals(),
    language="en",
    title_suffix="documentation",
    rtd_project="operonproject",
)
