"""Sphinx configuration for the Chinese Operon documentation.

This tree is the Read the Docs translation project ``operonproject-zh`` of the
parent project ``operonproject`` (English), served under the parent's domain as
``https://operonproject.readthedocs.io/zh-cn/latest/``. Shared settings live in
``docs/conf_common.py``; see ``contributor/documentation-deployment.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conf_common import apply_shared_settings  # noqa: E402

apply_shared_settings(
    globals(),
    language="zh_CN",
    title_suffix="文档",
    rtd_project="operonproject-zh",
)
