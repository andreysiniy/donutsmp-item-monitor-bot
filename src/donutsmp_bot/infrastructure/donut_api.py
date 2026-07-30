import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any

import httpx
from pydantic import ValidationError

from ..core.enums import PriceType
from ..domain.schemas import ApiHealth, AuctionListing, PriceSnapshot
from .rate_limiter import PerTokenRateLimiter

logger = logging.getLogger(__name__)


class DonutApiError(RuntimeError):
    pass


class DonutAuthenticationError(DonutApiError):
    pass


class DonutRateLimitError(DonutApiError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"API rate limit reached; retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class DonutTransientError(DonutApiError):
    pass


class DonutResponseError(DonutApiError):
    pass


@dataclass(slots=True)
class TokenApiMetrics:
    request_count: int = 0
    rate_limit_count: int = 0
    total_latency_seconds: float = 0
    last_success_at: datetime | None = None
    last_error: str | None = None

    @property
    def average_latency_seconds(self) -> float:
        if not self.request_count:
            return 0
        return self.total_latency_seconds / self.request_count


class DonutApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        limiter: PerTokenRateLimiter,
        timeout_seconds: float = 10,
        max_search_pages: int = 3,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limiter = limiter
        self.max_search_pages = max_search_pages
        self.max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._metrics: defaultdict[str, TokenApiMetrics] = defaultdict(TokenApiMetrics)
        self.health = ApiHealth()

    async def close(self) -> None:
        await self._client.aclose()

    def metrics(self, token_key: str) -> TokenApiMetrics:
        return self._metrics[token_key]

    async def validate_token(self, token: str, token_key: str) -> None:
        await self._request_page(
            token=token,
            token_key=token_key,
            page=1,
            search="diamond",
            interactive=True,
        )

    async def get_price(
        self,
        *,
        token: str,
        token_key: str,
        item_id: str,
        price_type: PriceType,
        interactive: bool = False,
    ) -> PriceSnapshot:
        search = item_id.removeprefix("minecraft:")
        exact_listings: list[AuctionListing] = []
        pages_scanned = 0

        for page in range(1, self.max_search_pages + 1):
            payload = await self._request_page(
                token=token,
                token_key=token_key,
                page=page,
                search=search,
                interactive=interactive,
            )
            pages_scanned = page
            raw_results = payload.get("result", [])
            if not isinstance(raw_results, list):
                raise DonutResponseError("DonutSMP response field 'result' is not a list")
            if not raw_results:
                break

            for raw_listing in raw_results:
                if not isinstance(raw_listing, dict):
                    continue
                raw_item = raw_listing.get("item")
                if not isinstance(raw_item, dict) or raw_item.get("id") != item_id:
                    continue
                try:
                    exact_listings.append(AuctionListing.model_validate(raw_listing))
                except ValidationError:
                    logger.warning(
                        "Ignoring malformed listing item_id=%s page=%s token_fingerprint=%s",
                        item_id,
                        page,
                        token_key,
                    )
            if exact_listings:
                break

        checked_at = datetime.now(UTC)
        if not exact_listings:
            return PriceSnapshot(
                item_id=item_id,
                price_type=price_type,
                selected_price=None,
                listing=None,
                checked_at=checked_at,
                pages_scanned=pages_scanned,
            )

        listing = min(exact_listings, key=lambda item: item.selected_price(price_type))
        return PriceSnapshot(
            item_id=item_id,
            price_type=price_type,
            selected_price=listing.selected_price(price_type),
            listing=listing,
            checked_at=checked_at,
            pages_scanned=pages_scanned,
        )

    async def _request_page(
        self,
        *,
        token: str,
        token_key: str,
        page: int,
        search: str,
        interactive: bool,
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire(token_key, interactive=interactive)
            started = perf_counter()
            metrics = self._metrics[token_key]
            metrics.request_count += 1
            try:
                response = await self._client.request(
                    "GET",
                    f"v1/auction/list/{page}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"search": search, "sort": "lowest_price"},
                )
                metrics.total_latency_seconds += perf_counter() - started
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                metrics.total_latency_seconds += perf_counter() - started
                metrics.last_error = type(exc).__name__
                self.health.available = False
                self.health.last_error = type(exc).__name__
                if attempt >= self.max_retries:
                    raise DonutTransientError(type(exc).__name__) from exc
                delay = self.limiter.record_transient_error(token_key)
                await self._sleep(delay)
                continue

            if response.status_code in (401, 403):
                metrics.last_error = "authentication"
                raise DonutAuthenticationError("DonutSMP rejected the token")
            if response.status_code == 429:
                metrics.rate_limit_count += 1
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                self.limiter.block(token_key, retry_after)
                metrics.last_error = "rate_limit"
                raise DonutRateLimitError(retry_after)
            if 500 <= response.status_code <= 599:
                metrics.last_error = f"http_{response.status_code}"
                self.health.available = False
                self.health.last_error = metrics.last_error
                if attempt >= self.max_retries:
                    raise DonutTransientError(metrics.last_error)
                delay = self.limiter.record_transient_error(token_key)
                await self._sleep(delay)
                continue
            if response.is_error:
                metrics.last_error = f"http_{response.status_code}"
                raise DonutResponseError(f"Unexpected HTTP status {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                metrics.last_error = "invalid_json"
                raise DonutResponseError("DonutSMP returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise DonutResponseError("DonutSMP response is not an object")
            if payload.get("status") != 0:
                metrics.last_error = "api_status"
                raise DonutResponseError("DonutSMP returned a non-zero API status")

            now = datetime.now(UTC)
            metrics.last_success_at = now
            metrics.last_error = None
            self.health.available = True
            self.health.last_error = None
            self.health.last_success_at = now
            self.limiter.record_success(token_key)
            logger.info(
                "DonutSMP request succeeded token_fingerprint=%s page=%s http_status=%s",
                token_key,
                page,
                response.status_code,
            )
            return payload

        raise DonutTransientError("Retry limit exhausted")


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return 1
    try:
        return max(float(value), 0)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            now = datetime.now(parsed.tzinfo or UTC)
            return max((parsed - now).total_seconds(), 0)
        except (TypeError, ValueError):
            return 1


def format_decimal_price(value: Decimal | None) -> str:
    if value is None:
        return "no listings"
    if value == value.to_integral_value():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
