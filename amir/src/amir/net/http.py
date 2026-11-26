from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import aiohttp

from amir.config.settings import HttpSettings

log = logging.getLogger(__name__)

_CHUNK_BYTES = 64 * 1024
_MAX_DELAY_SECONDS = 60

FetchErrorKind = Literal["timeout", "http", "too_large", "network"]


class FetchError(Exception):
    def __init__(self, kind: FetchErrorKind, status: int | None = None) -> None:
        super().__init__(f"fetch failed: {kind}" + (f" ({status})" if status is not None else ""))
        self.kind: FetchErrorKind = kind
        self.status = status
        self.retry_after: float | None = None
        self.permanent = False


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes | None
    etag: str | None
    last_modified: str | None
    not_modified: bool


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None  # HTTP-date form is ignored; fall back to exponential backoff
    return seconds if seconds >= 0 else None


class HttpFetcher:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: HttpSettings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        require_https: bool = True,
    ) -> None:
        self._session = session
        self._settings = settings
        self._sleep = sleep
        self._require_https = require_https

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        try:
            host = urlsplit(url).hostname or "?"
        except ValueError as exc:
            err = FetchError("network")
            err.permanent = True  # a malformed configured URL will not fix itself
            raise err from exc
        attempt = 0
        while True:
            try:
                return await self._attempt(url, etag, last_modified)
            except FetchError as err:
                if not self._retryable(err) or attempt >= self._settings.max_retries:
                    raise
                delay = min(
                    err.retry_after if err.retry_after is not None else 2**attempt,
                    _MAX_DELAY_SECONDS,
                )
                log.warning(
                    "fetch retry host=%s attempt=%d kind=%s status=%s delay=%s",
                    host,
                    attempt + 1,
                    err.kind,
                    err.status,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1

    @staticmethod
    def _retryable(err: FetchError) -> bool:
        if err.permanent:
            return False
        if err.kind in ("timeout", "network"):
            return True
        return (
            err.kind == "http"
            and err.status is not None
            and (err.status == 429 or err.status >= 500)
        )

    async def _attempt(self, url: str, etag: str | None, last_modified: str | None) -> FetchResult:
        headers = {"User-Agent": self._settings.user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._settings.connect_timeout_seconds,
            sock_read=self._settings.read_timeout_seconds,
        )
        try:
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                if self._require_https and resp.url.scheme != "https":
                    err = FetchError("network")
                    err.permanent = True  # a redirect to plain http will not fix itself
                    raise err
                if resp.status == 304:
                    return FetchResult(
                        304,
                        None,
                        resp.headers.get("ETag", etag),
                        resp.headers.get("Last-Modified", last_modified),
                        True,
                    )
                if resp.status != 200:
                    err = FetchError("http", resp.status)
                    err.retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                    raise err
                body = await self._read_body(resp)
                return FetchResult(
                    200, body, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), False
                )
        except FetchError:
            raise
        except TimeoutError as exc:
            raise FetchError("timeout") from exc
        except (aiohttp.InvalidURL, aiohttp.TooManyRedirects) as exc:
            err = FetchError("network")
            err.permanent = True  # a bad URL or a redirect loop will not fix itself
            raise err from exc
        except aiohttp.ClientError as exc:
            raise FetchError("network") from exc

    async def _read_body(self, resp: aiohttp.ClientResponse) -> bytes:
        limit = self._settings.max_response_bytes
        if resp.content_length is not None and resp.content_length > limit:
            raise FetchError("too_large")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise FetchError("too_large")
            chunks.append(chunk)
        return b"".join(chunks)
