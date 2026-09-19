"""Shared HTTP plumbing for source clients.

Every client here talks to a public biomedical API. None of them call a model:
this whole layer is deterministic by design, so that facts enter the corpus
before any model has a chance to invent one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

USER_AGENT = "condition-briefing/0.1 (health system strategy briefings)"
DEFAULT_TIMEOUT = 45.0

#: Status codes worth retrying. A 4xx other than 429 means we built a bad
#: request, and retrying it just burns the rate limit.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Serialises requests to at most ``rate_per_sec``.

    NCBI enforces 3 requests/second without an API key (10 with one) and will
    start returning 429s — and temporarily block the caller — above that. This
    is a hard external constraint, not a politeness setting.
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if not self._min_interval:
            return
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


class HttpSource:
    """Base for a source client.

    Subclasses set ``provider`` and ``rate_limit`` and implement their own
    fetch methods. Use as an async context manager, or pass in a shared
    ``httpx.AsyncClient`` when several sources run concurrently in one job.
    """

    provider: str = "unset"
    rate_limit: float = 0.0
    base_url: str = ""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        max_attempts: int = 4,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._limiter = limiter or RateLimiter(self.rate_limit)
        self._max_attempts = max_attempts

    async def __aenter__(self) -> HttpSource:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} used outside an async context manager; "
                "enter it with `async with`, or pass an httpx.AsyncClient"
            )
        return self._client

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential_jitter(initial=0.5, max=8.0),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                await self._limiter.acquire()
                resp = await self.client.get(url, params=params)
                resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request(url, params)).json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return (await self._request(url, params)).text


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def unique(items: list[str]) -> list[str]:
    """Order-preserving de-duplication, dropping empties."""
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item, None)
    return list(seen)


def first(value: Any) -> str | None:
    """openFDA returns most scalar label fields as single-element lists."""
    if isinstance(value, list) and value:
        return value[0]
    return value if isinstance(value, str) else None


def parse_partial_date(value: str | None) -> date | None:
    """Normalise the several date shapes these APIs emit.

    CT.gov gives 'YYYY-MM' or 'YYYY-MM-DD'; openFDA gives 'YYYYMMDD'; PubMed
    gives free text. Partial dates are anchored to the first of the period —
    good enough for recency decay, and never presented to a reader as precise.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        if len(raw) == 8 and raw.isdigit():
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        parts = raw.split("-")
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1)
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def year_from(pubdate: str | None) -> int | None:
    """PubMed pubdate is free text: '2026 Jun 9', '2026 Jul-Aug', '2026'."""
    if not pubdate:
        return None
    head = pubdate.strip().split(" ")[0]
    return int(head) if head.isdigit() and len(head) == 4 else None
