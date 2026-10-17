#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Operon the Database System: Archive, Quality-Control, Organize, Analyze and Release Your Bio-Data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("OperonDBS")
except PackageNotFoundError:  # Direct use from an uninstalled source checkout.
    __version__ = "0+unknown"
