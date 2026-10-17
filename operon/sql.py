#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""SQL identifier boundary for project-defined metadata and generic helpers."""

from __future__ import annotations

import re

from operon.errors import ValidationError


def quote_identifier(name: str) -> str:
    """Validate and quote one identifier, never an expression or a value.

    Use the same identifier alphabet as metadata column creation. Values
    must still use SQLite parameters; quoting does not authorize a table.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValidationError(f"unsafe SQL identifier {name!r}")
    return f'"{name}"'
