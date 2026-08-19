"""Small pytest-native helpers used while keeping test bodies readable."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable, Type

import pytest


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
    def assertRaises(exception: Type[BaseException]) -> AbstractContextManager[Any]:
        return pytest.raises(exception)

    @staticmethod
    def assertRaisesRegex(exception: Type[BaseException], pattern: str) -> AbstractContextManager[Any]:
        return pytest.raises(exception, match=pattern)
