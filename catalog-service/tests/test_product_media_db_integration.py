from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.exceptions import ProductImageLimitError
from catalog_service.media.image_processing import ProcessedProductImage
from catalog_service.repositories.product_image import ProductImageRepository
from catalog_service.repositories.products import ProductRepository
from catalog_service.services.product_media import ProductMediaService
from catalog_service.storage.s3 import S3MediaStorage
from tests.test_reservations_db_integration import product

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.test_reservations_db_integration",)


def media_service(session: AsyncSession, storage: S3MediaStorage) -> ProductMediaService:
    return ProductMediaService(
        session=session,
        products=ProductRepository(session),
        cache=AsyncMock(spec=CacheProduct),
        images=ProductImageRepository(session),
        storage=storage,
    )


@pytest.fixture(autouse=True)
def fake_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalog_service.services.product_media.process_product_image",
        lambda _: ProcessedProductImage(large=b"large", thumbnail=b"thumbnail"),
    )
    monkeypatch.setattr(
        "catalog_service.services.product_media.validate_product_glb", lambda _: None
    )


async def test_upload_and_delete_persist_gallery_positions(db_session: AsyncSession) -> None:
    item = product(3)
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    storage = AsyncMock(spec=S3MediaStorage)
    service = media_service(db_session, storage)
    first = await service.add_image(item.id, b"first")
    second = await service.add_image(item.id, b"second")

    async with db_session.begin():
        stored = await ProductImageRepository(db_session).list_by_product_id(item.id)
    assert [image.id for image in stored] == [first.id, second.id]
    assert [image.position for image in stored] == [0, 1]

    await service.delete_image(item.id, first.id)

    async with db_session.begin():
        remaining = await ProductImageRepository(db_session).list_by_product_id(item.id)
    assert [image.id for image in remaining] == [second.id]
    assert remaining[0].position == 0
    storage.delete.assert_any_await(first.large_object_key)
    storage.delete.assert_any_await(first.thumbnail_object_key)


async def test_ninth_upload_rolls_back_and_removes_new_objects(
    db_session: AsyncSession,
) -> None:
    item = product(3)
    product_id = item.id
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    storage = AsyncMock(spec=S3MediaStorage)
    service = media_service(db_session, storage)
    for _ in range(8):
        await service.add_image(product_id, b"photo")

    with pytest.raises(ProductImageLimitError):
        await service.add_image(product_id, b"ninth")

    async with db_session.begin():
        stored = await ProductImageRepository(db_session).list_by_product_id(product_id)
    assert len(stored) == 8
    assert [image.position for image in stored] == list(range(8))
    assert storage.delete.await_count == 2


async def test_reorder_swaps_positions_in_one_transaction(db_session: AsyncSession) -> None:
    item = product(3)
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    storage = AsyncMock(spec=S3MediaStorage)
    service = media_service(db_session, storage)
    first = await service.add_image(item.id, b"first")
    second = await service.add_image(item.id, b"second")

    reordered = await service.reorder_images(item.id, [second.id, first.id])

    async with db_session.begin():
        stored = await ProductImageRepository(db_session).list_by_product_id(item.id)
    assert [image.id for image in reordered] == [second.id, first.id]
    assert [(image.id, image.position) for image in stored] == [
        (second.id, 0),
        (first.id, 1),
    ]
    storage.delete.assert_not_awaited()


async def test_model_key_is_persisted_replaced_and_removed(db_session: AsyncSession) -> None:
    item = product(3)
    product_id = item.id
    async with db_session.begin():
        await ProductRepository(db_session).add(item)

    storage = AsyncMock(spec=S3MediaStorage)
    service = media_service(db_session, storage)
    first_key = await service.replace_model(product_id, b"first")
    second_key = await service.replace_model(product_id, b"second")

    async with db_session.begin():
        stored = await ProductRepository(db_session).get_by_id(product_id)
        assert stored is not None
        assert stored.model_3d_key == second_key
    assert first_key != second_key
    storage.delete.assert_awaited_once_with(first_key)

    await service.delete_model(product_id)
    async with db_session.begin():
        stored = await ProductRepository(db_session).get_by_id(product_id)
        assert stored is not None
        assert stored.model_3d_key is None
    storage.delete.assert_any_await(second_key)
