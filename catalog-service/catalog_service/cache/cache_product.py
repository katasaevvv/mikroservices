
import logging
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError

PRODUCT_CACHE_PREFIX = "cache:product"
PRODUCT_LIST_CACHE_PREFIX = "cache:products:list"

Value = BaseModel
T = TypeVar("T")

logger = logging.getLogger(__name__)

class CacheProduct:
    def __init__(
        self,
        redis: Redis
    ) -> None:
        self._redis = redis

    def build_product_cache_key(self, product_id: UUID) -> str:
        return f"{PRODUCT_CACHE_PREFIX}:{product_id}"

    def build_products_list_cache_key(self, *, limit: int, offset: int) -> str:
        return f"{PRODUCT_LIST_CACHE_PREFIX}:limit:{limit}:offset:{offset}"

    async def get_cache(self, key: str) -> Any:
        try:
            return await self._redis.get(key)
        except (ConnectionError, TimeoutError):
            logger.warning(
            "redis.cache_get_failed",
            extra={"cache_key": key},
            exc_info=True,
        )
            return None

    async def set_cache(self, key: str, value: Value, ex: int = 300) -> None:
        value = value.model_dump_json()
        try:
            await self._redis.set(name=key, value=value, ex=ex)
        except (ConnectionError, TimeoutError):
                    logger.warning(
                    "redis.cache_set_failed",
                    extra={"cache_key": key},
                    exc_info=True,
                )

    async def invalidate_lists(self):
        try:
            keys = [
                key
                async for key in self._redis.scan_iter(
                    match=f"{PRODUCT_LIST_CACHE_PREFIX}:*"
                )
            ]
            if keys:
                await self._redis.delete(*keys)
        except (ConnectionError, TimeoutError):
                    logger.warning(
                    "redis.invalidate_list_failed",
                    exc_info=True,
                )

    async def invalidate_product(self, product_id: UUID):
        key = self.build_product_cache_key(product_id)
        try:
            await self._redis.delete(key)
        except (ConnectionError, TimeoutError):
                    logger.warning(
                    "redis.cache_invalidate_product_failed",
                    extra={"cache_key": key},
                    exc_info=True,
                )
        await self.invalidate_lists()