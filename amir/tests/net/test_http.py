import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from amir.config.settings import HttpSettings
from amir.net.http import FetchError, HttpFetcher

FEED = "forum_pc_update_notes.rss.xml"


class Recorder:
    def __init__(self) -> None:
        self.hits = 0
        self.headers: list[dict[str, str]] = []
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make_settings(**overrides: object) -> HttpSettings:
    # model_construct: read timeouts below 1 s are test-only and bypass field validation.
    values: dict[str, object] = {
        "connect_timeout_seconds": 2,
        "read_timeout_seconds": 0.2,
        "max_retries": 3,
        "max_response_bytes": 5_000_000,
        "user_agent": "AmirBeckett/0.1 (+Discord bot)",
    }
    values.update(overrides)
    return HttpSettings.model_construct(**values)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def serve() -> Callable[..., object]:
    servers: list[TestServer] = []

    async def _serve(handler: Callable[..., object]) -> TestServer:
        app = web.Application()
        app.router.add_get("/feed", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server

    _serve.servers = servers  # type: ignore[attr-defined]
    return _serve


@pytest.fixture(autouse=True)
async def _close_servers(serve: Callable[..., object]) -> AsyncIterator[None]:
    yield
    for server in serve.servers:  # type: ignore[attr-defined]
        await server.close()


def fetcher(session: aiohttp.ClientSession, rec: Recorder, **overrides: object) -> HttpFetcher:
    return HttpFetcher(session, make_settings(**overrides), sleep=rec.sleep, require_https=False)


def feed_handler(rec: Recorder, body: bytes):
    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        rec.headers.append(dict(request.headers))
        if request.headers.get("If-None-Match") == '"abc"':
            return web.Response(status=304, headers={"ETag": '"abc"'})
        return web.Response(
            body=body,
            headers={"ETag": '"abc"', "Last-Modified": "Sat, 24 Oct 2026 12:00:00 GMT"},
        )

    return handler


async def test_conditional_request_returns_not_modified(serve, rec, captured) -> None:
    server = await serve(feed_handler(rec, captured(FEED)))
    async with aiohttp.ClientSession() as session:
        result = await fetcher(session, rec).fetch(str(server.make_url("/feed")), etag='"abc"')
    assert result.not_modified is True
    assert result.status == 304
    assert result.body is None


async def test_200_returns_real_body_and_validators(serve, rec, captured) -> None:
    server = await serve(feed_handler(rec, captured(FEED)))
    async with aiohttp.ClientSession() as session:
        result = await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert result.status == 200
    assert result.body == captured(FEED)
    assert result.etag == '"abc"'
    assert result.last_modified == "Sat, 24 Oct 2026 12:00:00 GMT"
    assert result.not_modified is False


async def test_last_modified_sent_as_if_modified_since(serve, rec, captured) -> None:
    server = await serve(feed_handler(rec, captured(FEED)))
    async with aiohttp.ClientSession() as session:
        await fetcher(session, rec).fetch(
            str(server.make_url("/feed")), last_modified="Sat, 24 Oct 2026 12:00:00 GMT"
        )
    assert rec.headers[0]["If-Modified-Since"] == "Sat, 24 Oct 2026 12:00:00 GMT"


async def test_retries_503_then_succeeds(serve, rec, captured) -> None:
    inner = feed_handler(rec, captured(FEED))

    async def handler(request: web.Request) -> web.Response:
        if rec.hits < 2:
            rec.hits += 1
            return web.Response(status=503)
        return await inner(request)

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        result = await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert result.status == 200
    assert rec.sleeps == [1, 2]


async def test_gives_up_after_max_retries(serve, rec, caplog) -> None:
    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        return web.Response(status=503)

    server = await serve(handler)
    with caplog.at_level(logging.WARNING):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(FetchError) as exc:
                await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert exc.value.kind == "http"
    assert exc.value.status == 503
    assert rec.hits == 4
    assert rec.sleeps == [1, 2, 4]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert "127.0.0.1" in warnings[0]


async def test_retry_after_overrides_and_is_capped(serve, rec) -> None:
    values = iter(["7", "999"])

    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        if rec.hits <= 2:
            return web.Response(status=429, headers={"Retry-After": next(values)})
        return web.Response(body=b"ok")

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        result = await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert result.body == b"ok"
    assert rec.sleeps == [7, 60]


async def test_404_not_retried(serve, rec) -> None:
    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        return web.Response(status=404)

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert (exc.value.kind, exc.value.status) == ("http", 404)
    assert rec.hits == 1
    assert rec.sleeps == []


async def test_body_over_limit_rejected(serve, rec, captured) -> None:
    body, limit = captured(FEED), 1_000_000
    assert len(body) > limit  # the real feed is over the limit
    server = await serve(feed_handler(rec, body))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec, max_response_bytes=limit).fetch(
                str(server.make_url("/feed"))
            )
    assert exc.value.kind == "too_large"
    assert rec.hits == 1


async def test_chunked_body_over_limit_rejected(serve, rec, captured) -> None:
    body = captured(FEED)

    async def handler(request: web.Request) -> web.StreamResponse:
        rec.hits += 1
        resp = web.StreamResponse()
        await resp.prepare(request)  # chunked: no Content-Length
        await resp.write(body)
        await resp.write_eof()
        return resp

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec, max_response_bytes=1_000_000).fetch(
                str(server.make_url("/feed"))
            )
    assert exc.value.kind == "too_large"


async def test_read_timeout(serve, rec) -> None:
    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        await asyncio.sleep(1)
        return web.Response(body=b"late")

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec, max_retries=1).fetch(str(server.make_url("/feed")))
    assert exc.value.kind == "timeout"
    assert rec.hits == 2


async def test_network_error_retried_then_raised(rec) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec, max_retries=2).fetch("http://127.0.0.1:1/feed")
    assert exc.value.kind == "network"
    assert exc.value.status is None
    assert rec.sleeps == [1, 2]


async def test_user_agent_sent(serve, rec, captured) -> None:
    server = await serve(feed_handler(rec, captured(FEED)))
    async with aiohttp.ClientSession() as session:
        await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert rec.headers[0]["User-Agent"] == "AmirBeckett/0.1 (+Discord bot)"


async def test_non_https_final_url_rejected(serve, rec, captured) -> None:
    server = await serve(feed_handler(rec, captured(FEED)))
    async with aiohttp.ClientSession() as session:
        strict = HttpFetcher(session, make_settings(), sleep=rec.sleep)
        with pytest.raises(FetchError) as exc:
            await strict.fetch(str(server.make_url("/feed")))
    assert exc.value.kind == "network"
    assert rec.sleeps == []


async def test_invalid_url_fails_at_once_without_retry(rec) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec).fetch("http:///feed")
    assert exc.value.kind == "network"
    assert exc.value.permanent is True
    assert rec.sleeps == []


async def test_redirect_loop_fails_at_once_without_retry(serve, rec) -> None:
    async def handler(request: web.Request) -> web.Response:
        rec.hits += 1
        raise web.HTTPFound("/feed")

    server = await serve(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec).fetch(str(server.make_url("/feed")))
    assert exc.value.kind == "network"
    assert exc.value.permanent is True
    assert rec.hits == 10  # aiohttp's default redirect limit, all inside a single attempt
    assert rec.sleeps == []


async def test_malformed_configured_url_is_a_permanent_fetch_error(rec) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError) as exc:
            await fetcher(session, rec).fetch("http://[::1/feed")
    assert exc.value.kind == "network"
    assert exc.value.permanent is True
    assert rec.sleeps == []
