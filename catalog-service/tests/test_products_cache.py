from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.db.models.product_image import ProductImage
from catalog_service.db.models.products import Product
from catalog_service.schemas.products import ProductListResponse, ProductPublicRead
from catalog_service.services.products import ProductsService


def redis_mock() -> MagicMock:
    redis = MagicMock(spec=Redis)
    redis.get = AsyncMock()
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    return redis


async def test_product_cache_hit_returns_same_public_dto_as_db_miss() -> None:
    product_id = uuid4()
    image_id = uuid4()
    model_key = f"products/{product_id}/models/{uuid4()}.glb"
    product = Product(
        id=product_id,
        category="books",
        name="Test book",
        description=None,
        price=Decimal("100.00"),
        stock_quantity=5,
        is_active=True,
        model_3d_key=model_key,
        images=[
            ProductImage(
                id=image_id,
                product_id=product_id,
                position=0,
                large_object_key=f"products/{product_id}/images/{image_id}/large.webp",
                thumbnail_object_key=(
                    f"products/{product_id}/images/{image_id}/thumbnail.webp"
                ),
            )
        ],
    )
    redis = redis_mock()
    redis.get.return_value = None
    repository = SimpleNamespace(get_by_id_with_images=AsyncMock(return_value=product))
    service = ProductsService(
        session=MagicMock(),
        cache=CacheProduct(redis),
        repository=repository,
    )

    from_db = await service.get_product_by_id(product_id)
    cached_json = redis.set.await_args.kwargs["value"]
    redis.get.return_value = cached_json
    from_cache = await service.get_product_by_id(product_id)

    assert isinstance(from_db, ProductPublicRead)
    assert isinstance(from_cache, ProductPublicRead)
    assert from_cache == from_db
    assert from_cache.price == Decimal("100.00")
    assert from_cache.images[0].url == (
        f"/media/products/{product_id}/images/{image_id}/large.webp"
    )
    assert from_cache.model_3d_url == f"/media/{model_key}"
    repository.get_by_id_with_images.assert_awaited_once_with(product_id)


async def test_product_list_cache_hit_returns_same_response_as_db_miss() -> None:
    product = Product(
        id=uuid4(),
        category="books",
        name="Test book",
        description=None,
        price=Decimal("100.00"),
        stock_quantity=5,
        is_active=True,
        images=[],
    )
    redis = redis_mock()
    redis.get.return_value = None
    repository = SimpleNamespace(
        get_list_products=AsyncMock(return_value=[product]),
        count_active=AsyncMock(return_value=7),
    )
    service = ProductsService(
        session=MagicMock(),
        cache=CacheProduct(redis),
        repository=repository,
    )

    from_db = await service.get_products(limit=20, offset=0)
    cached_json = redis.set.await_args.kwargs["value"]
    redis.get.return_value = cached_json
    from_cache = await service.get_products(limit=20, offset=0)

    assert isinstance(from_db, ProductListResponse)
    assert isinstance(from_cache, ProductListResponse)
    assert from_cache == from_db
    assert from_cache.total == 7
    assert from_cache.limit == 20
    assert from_cache.offset == 0
    repository.get_list_products.assert_awaited_once_with(limit=20, offset=0)
    repository.count_active.assert_awaited_once_with()
    assert [call.args[0] for call in redis.get.await_args_list] == [
        "cache:products:list:limit:20:offset:0",
        "cache:products:list:limit:20:offset:0",
    ]


async def test_product_list_uses_distinct_cache_keys_for_pages() -> None:
    redis = redis_mock()
    redis.get.return_value = None
    repository = SimpleNamespace(
        get_list_products=AsyncMock(return_value=[]),
        count_active=AsyncMock(return_value=0),
    )
    service = ProductsService(
        session=MagicMock(),
        cache=CacheProduct(redis),
        repository=repository,
    )

    await service.get_products(limit=20, offset=0)
    await service.get_products(limit=20, offset=20)

    assert [call.kwargs["name"] for call in redis.set.await_args_list] == [
        "cache:products:list:limit:20:offset:0",
        "cache:products:list:limit:20:offset:20",
    ]


async def test_product_list_invalidation_deletes_all_cached_pages() -> None:
    async def cached_page_keys():
        yield "cache:products:list:limit:20:offset:0"
        yield "cache:products:list:limit:20:offset:20"

    redis = redis_mock()
    redis.scan_iter = MagicMock(return_value=cached_page_keys())

    await CacheProduct(redis).invalidate_lists()

    redis.scan_iter.assert_called_once_with(match="cache:products:list:*")
    redis.delete.assert_awaited_once_with(
        "cache:products:list:limit:20:offset:0",
        "cache:products:list:limit:20:offset:20",
    )


async def test_cache_get_connection_error_is_treated_as_miss() -> None:
    redis = redis_mock()
    redis.get.side_effect = ConnectionError("Redis unavailable")

    result = await CacheProduct(redis).get_cache("cache:product:test")

    assert result is None


async def test_cache_set_timeout_does_not_fail_request() -> None:
    redis = redis_mock()
    redis.set.side_effect = TimeoutError("Redis timed out")
    response = ProductListResponse(items=[], total=0, limit=20, offset=0)

    await CacheProduct(redis).set_cache("cache:products:list:limit:20:offset:0", response)

    redis.set.assert_awaited_once()


async def test_cache_invalidation_connection_error_does_not_fail_request() -> None:
    async def no_cached_pages():
        if False:
            yield ""

    product_id = uuid4()
    redis = redis_mock()
    redis.delete.side_effect = ConnectionError("Redis unavailable")
    redis.scan_iter = MagicMock(return_value=no_cached_pages())

    await CacheProduct(redis).invalidate_product(product_id)

    redis.delete.assert_awaited_once_with(f"cache:product:{product_id}")
