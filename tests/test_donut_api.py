import json
from decimal import Decimal

import httpx
import pytest

from donutsmp_bot.core.enums import PriceType
from donutsmp_bot.infrastructure.donut_api import (
    DonutApiClient,
    DonutAuthenticationError,
    DonutRateLimitError,
    DonutResponseError,
)
from donutsmp_bot.infrastructure.rate_limiter import PerTokenRateLimiter


def _client(
    handler: httpx.MockTransport,
    *,
    pages: int = 3,
) -> DonutApiClient:
    return DonutApiClient(
        base_url="https://api.example/",
        limiter=PerTokenRateLimiter(),
        max_search_pages=pages,
        max_retries=0,
        transport=handler,
    )


@pytest.mark.asyncio
async def test_get_with_json_body_paginates_and_filters_exact_item() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.path.rsplit("/", 1)[1])
        result = (
            [{"item": {"id": "diamond_block", "count": 1}, "price": 1}]
            if page == 1
            else [
                {
                    "item": {
                        "id": "diamond",
                        "count": 5,
                        "display_name": "Diamond",
                    },
                    "price": 100001,
                    "seller": {"name": "Seller"},
                    "time_left": 120,
                },
                {
                    "item": {"id": "diamond", "count": 2},
                    "price": 50000,
                    "seller": {"name": "CheaperUnit"},
                },
            ]
        )
        return httpx.Response(200, json={"status": 0, "result": result})

    client = _client(httpx.MockTransport(handler))
    try:
        snapshot = await client.get_price(
            token="private-token",
            token_key="fingerprint",
            item_id="diamond",
            price_type=PriceType.PER_ITEM,
        )
    finally:
        await client.close()

    assert len(requests) == 2
    assert snapshot.selected_price == Decimal("20000.2")
    assert snapshot.listing is not None
    assert snapshot.listing.seller.name == "Seller"
    assert requests[0].method == "GET"
    assert requests[0].headers["Authorization"] == "Bearer private-token"
    assert json.loads(requests[0].content) == {
        "search": "diamond",
        "sort": "lowest_price",
    }


@pytest.mark.asyncio
async def test_no_exact_listings_returns_null_price() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": 0, "result": []})
    )
    client = _client(transport)
    try:
        snapshot = await client.get_price(
            token="token",
            token_key="key",
            item_id="diamond",
            price_type=PriceType.TOTAL,
        )
    finally:
        await client.close()
    assert snapshot.selected_price is None
    assert snapshot.listing is None


@pytest.mark.asyncio
async def test_legacy_namespaced_item_id_matches_plain_api_item_id() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": 200,
                "result": [{"item": {"id": "diamond", "count": 1}, "price": 25000}],
            },
        )
    )
    client = _client(transport)
    try:
        snapshot = await client.get_price(
            token="token",
            token_key="key",
            item_id="minecraft:diamond",
            price_type=PriceType.TOTAL,
        )
    finally:
        await client.close()

    assert snapshot.item_id == "diamond"
    assert snapshot.selected_price == Decimal("25000")


@pytest.mark.asyncio
async def test_validate_token_accepts_http_status_in_api_status_field() -> None:
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": 200, "result": []})
        )
    )
    try:
        await client.validate_token("token", "fingerprint")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_errors(status: int) -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(status, json={})),
        pages=1,
    )
    try:
        with pytest.raises(DonutAuthenticationError):
            await client.validate_token("bad", "fingerprint")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_429_honors_retry_after() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "12"}))
    )
    try:
        with pytest.raises(DonutRateLimitError) as caught:
            await client.validate_token("token", "fingerprint")
    finally:
        await client.close()
    assert caught.value.retry_after == 12


@pytest.mark.asyncio
async def test_non_zero_api_status_is_an_error() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"status": 5, "result": []}))
    )
    try:
        with pytest.raises(DonutResponseError):
            await client.validate_token("token", "fingerprint")
    finally:
        await client.close()
