"""Operon the Database System: Archive, Quality-Control, Organize, Analyze and Release Your Bio-Data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("operon")
except PackageNotFoundError:  # Direct use from an uninstalled source checkout.
    __version__ = "0+unknown"
