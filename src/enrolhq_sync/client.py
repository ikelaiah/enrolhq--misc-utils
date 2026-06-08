"""Resilient wrapper around the EnrolHQ SDK client.

All network flakiness handling lives here so the rest of the package can treat
the API as reliable. The predecessor wrapped each call in a bare ``try/except``
with no retry; this wrapper adds bounded exponential backoff and caches the
reference dictionaries (countries/languages/nationalities) that the FK
transforms need.

Retry is deliberately conservative: a small fixed number of attempts with
exponential backoff, and it never retries a ``PUT`` more times than configured
(a half-applied write should surface, not silently re-fire forever).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

_log = logging.getLogger("enrolhq_sync.client")

T = TypeVar("T")

# Reference dictionaries the FK transforms resolve against, paired with the
# SDK accessor that returns the full list.
_DICTIONARY_SOURCES = ("countries", "languages", "nationalities")


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5  # seconds; doubled each retry
    max_delay: float = 8.0


def _with_retry(func: Callable[[], T], policy: RetryPolicy, what: str) -> T:
    delay = policy.base_delay
    last_exc: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 — SDK raises a broad hierarchy
            last_exc = exc
            if attempt == policy.attempts:
                break
            _log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                what, attempt, policy.attempts, exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, policy.max_delay)
    assert last_exc is not None
    raise last_exc


class ResilientClient:
    """Thin facade over ``EnrolHQClient`` with retry + dictionary caching."""

    def __init__(self, sdk_client: Any, policy: RetryPolicy | None = None) -> None:
        self._sdk = sdk_client
        self._policy = policy or RetryPolicy()
        self._dictionaries: dict[str, dict[str, Any]] | None = None

    # -- applications -------------------------------------------------------

    def list_applications(self, *, entry_year: int) -> Iterable[dict[str, Any]]:
        """List summaries for one entry year (school-owned records only)."""
        return _with_retry(
            lambda: list(
                self._sdk.applications.list(entry_year=entry_year, has_external_id=True)
            ),
            self._policy,
            f"list(entry_year={entry_year})",
        )

    def get_application(self, app_id: Any) -> dict[str, Any]:
        return _with_retry(
            lambda: self._sdk.applications.get(app_id),
            self._policy,
            f"get({app_id})",
        )

    def update_application(self, app_id: Any, body: dict[str, Any]) -> Any:
        return _with_retry(
            lambda: self._sdk.applications.update(app_id, body),
            self._policy,
            f"update({app_id})",
        )

    # -- reference dictionaries --------------------------------------------

    def dictionaries(self) -> dict[str, dict[str, Any]]:
        """Return name->id maps for the FK reference dictionaries, cached."""
        if self._dictionaries is not None:
            return self._dictionaries
        out: dict[str, dict[str, Any]] = {}
        for name in _DICTIONARY_SOURCES:
            fetcher = getattr(self._sdk.reference_data, name)
            try:
                items = _with_retry(lambda: list(fetcher()), self._policy, f"reference_data.{name}()")
            except Exception as exc:  # noqa: BLE001
                _log.warning("failed to load %s dictionary: %s", name, exc)
                items = []
            table: dict[str, Any] = {}
            for item in items:
                label = item.get("name") or item.get("title")
                fk_id = item.get("id")
                if label and fk_id is not None:
                    table[str(label).strip().lower()] = fk_id
            out[name] = table
            _log.info("  %s dictionary: %d entries", name, len(table))
        self._dictionaries = out
        return out


def build_default_client(policy: RetryPolicy | None = None) -> ResilientClient:
    """Construct a :class:`ResilientClient` around a fresh ``EnrolHQClient``.

    Imported lazily so the package (and its tests) load without the SDK or a
    configured token.
    """
    from enrolhq import EnrolHQClient

    return ResilientClient(EnrolHQClient(), policy=policy)
