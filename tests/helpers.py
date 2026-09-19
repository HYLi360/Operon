"""Small pytest-native helpers used while keeping test bodies readable."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest


def copy_project_tree(source: str | Path, target: str | Path) -> Path:
    """Copy a project directory into a *writable* one and return the copy.

    ``shutil.copytree`` preserves file modes, and a project whose database was
    read while it was itself read-only carries side files with that read-only
    mode: SQLite gives ``operon.sqlite-shm``/``-wal`` the mode of the database
    it opened.  A copy inherits them and can no longer be written — opening it
    raises ``attempt to write a readonly database`` (ODR-0021).  Dropping the
    shared-memory file is safe (SQLite rebuilds it from the log on the next
    open) and restoring write permission on the database, log and journal files
    makes the copy behave like the project it was copied from.
    """

    source_path = Path(source)
    target_path = Path(target)
    shutil.copytree(source_path, target_path)
    for shm_path in target_path.rglob("*.sqlite-shm"):
        shm_path.unlink()
    for sqlite_path in target_path.rglob("*.sqlite*"):
        if sqlite_path.is_file():
            sqlite_path.chmod(0o644)
    return target_path


class PytestAssertions:
    """Assertion adapter backed by plain assertions and :func:`pytest.raises`.

    The suite used class-based standard-library tests historically. Keeping this
    tiny adapter makes the migration reviewable without retaining that runner's
    lifecycle, discovery, or exception machinery.
    """

    def setup_method(self) -> None:
        self._cleanup_callbacks: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def teardown_method(self) -> None:
        while self._cleanup_callbacks:
            callback, args, kwargs = self._cleanup_callbacks.pop()
            callback(*args, **kwargs)

    def addCleanup(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._cleanup_callbacks.append((callback, args, kwargs))

    @staticmethod
    def assertEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left == right, message

    @staticmethod
    def assertNotEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left != right, message

    @staticmethod
    def assertTrue(value: Any, message: Any = None) -> None:
        assert value, message

    @staticmethod
    def assertFalse(value: Any, message: Any = None) -> None:
        assert not value, message

    @staticmethod
    def assertIn(member: Any, container: Any, message: Any = None) -> None:
        assert member in container, message

    @staticmethod
    def assertIsNone(value: Any, message: Any = None) -> None:
        assert value is None, message

    @staticmethod
    def assertIsNotNone(value: Any, message: Any = None) -> None:
        assert value is not None, message

    @staticmethod
    def assertGreater(left: Any, right: Any, message: Any = None) -> None:
        assert left > right, message

    @staticmethod
    def assertGreaterEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left >= right, message

    @staticmethod
    def assertLessEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left <= right, message

    @staticmethod
    def assertAlmostEqual(left: float, right: float, places: int = 7, message: Any = None) -> None:
        assert round(abs(left - right), places) == 0, message

    @staticmethod
    def assertRaises(exception: type[BaseException]) -> AbstractContextManager[Any]:
        return pytest.raises(exception)

    @staticmethod
    def assertRaisesRegex(exception: type[BaseException], pattern: str) -> AbstractContextManager[Any]:
        return pytest.raises(exception, match=pattern)
