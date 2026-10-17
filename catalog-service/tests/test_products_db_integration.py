from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.exceptions import InsufficientStockError, ProductNotFoundError
from catalog_service.repositories.products import ProductRepository
from catalog_service.schemas.products import ProductCreate, ProductUpdate
from catalog_service.services.products import ProductsService
from tests.test_reservations_db_integration import product

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.test_reservations_db_integration",)


def service(session: AsyncSession) -> ProductsService:
    cache = MagicMock(spec=CacheProduct)
    cache.build_product_cache_key.side_effect = (
        lambda product_id: f"cache:product:{product_id}"
    )
    cache.get_cache = AsyncMock(return_value=None)
    cache.set_cache = AsyncMock()
    cache.invalidate_lists = AsyncMock()
    cache.invalidate_product = AsyncMock()
    return ProductsService(session=session, cache=cache, repository=ProductRepository(session))


async def test_create_product_persists_and_can_be_read(db_session: AsyncSession) -> None:
    created = await service(db_session).create_product(
        ProductCreate(
            category="books", name="A book", price=Decimal("199.00"), stock_quantity=4
        )
    )

    loaded = await service(db_session).get_product_by_id(created.id)
    assert loaded.id == created.id
    assert loaded.price == Decimal("199.00")
    assert loaded.stock_quantity == 4


async def test_get_unknown_product_raises(db_session: AsyncSession) -> None:
    with pytest.raises(ProductNotFoundError):
        await service(db_session).get_product_by_id(uuid4())


async def test_decrease_stock_is_atomic_and_rejects_shortage(db_session: AsyncSession) -> None:
    item = product(3)
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    product_id = item.id
    assert await service(db_session).decrease_product_stock(product_id, 2) == 1
    with pytest.raises(InsufficientStockError):
        await service(db_session).decrease_product_stock(product_id, 2)
    assert (await service(db_session).get_product_by_id(product_id)).stock_quantity == 1


async def test_decrease_unknown_product_raises(db_session: AsyncSession) -> None:
    with pytest.raises(ProductNotFoundError):
        await service(db_session).decrease_product_stock(uuid4(), 1)


async def test_deactivate_then_activate_product(db_session: AsyncSession) -> None:
    item = product(5)
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    await service(db_session).deactivate_product(item.id)
    assert (await service(db_session).get_product_by_id(item.id)).is_active is False
    await service(db_session).deactivate_product(item.id)
    activated = await service(db_session).activate_product(item.id)
    assert activated.is_active is True
    assert (await service(db_session).activate_product(item.id)).is_active is True


async def test_activate_unknown_product_raises(db_session: AsyncSession) -> None:
    with pytest.raises(ProductNotFoundError):
        await service(db_session).activate_product(uuid4())

async def test_partial_update_preserves_omitted_fields(
    db_session: AsyncSession,
) -> None:
    item = product(5)
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    await service(db_session).update_product(
        item.id,
        ProductUpdate(name="Updated book"),
    )
    loaded = await service(db_session).get_product_by_id(item.id)

    assert loaded.name == "Updated book"
    assert loaded.category == "books"
    assert loaded.description is None
    assert loaded.price == Decimal("100.00")
    assert loaded.stock_quantity == 5
    assert loaded.is_active is True
