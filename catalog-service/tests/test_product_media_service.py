from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.db.models.product_image import ProductImage
from catalog_service.exceptions import (
    ProductImageLimitError,
    ProductImageNotFoundError,
    ProductImageOrderConflictError,
    ProductNotFoundError,
)
from catalog_service.media.glb_validation import InvalidProductModelError
from catalog_service.media.image_processing import ProcessedProductImage
from catalog_service.services.product_media import ProductMediaService


class FakeSession:
    def __init__(self, fail_commit: bool = False) -> None:
        self.active = False
        self.begin_calls = 0
        self.fail_commit = fail_commit

    def begin(self) -> "FakeSession":
        self.begin_calls += 1
        return self

    async def __aenter__(self) -> "FakeSession":
        self.active = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.active = False
        if self.fail_commit:
            raise OSError("DB commit failed")


class FakeProducts:
    def __init__(self, exists: bool = True, model_key: str | None = None) -> None:
        self.exists = exists
        self.product = SimpleNamespace(model_3d_key=model_key)

    async def get_by_id_for_update(self, product_id: UUID) -> object | None:
        return self.product if self.exists else None


class FakeImages:
    def __init__(self, images: list[ProductImage] | None = None) -> None:
        self.images = list(images or [])

    async def add(self, image: ProductImage) -> ProductImage:
        self.images.append(image)
        return image

    async def get_by_id(self, image_id: UUID, product_id: UUID) -> ProductImage | None:
        return next(
            (
                image
                for image in self.images
                if image.id == image_id and image.product_id == product_id
            ),
            None,
        )

    async def list_by_product_id(self, product_id: UUID) -> list[ProductImage]:
        return sorted(
            (image for image in self.images if image.product_id == product_id),
            key=lambda image: image.position,
        )

    async def delete(self, image: ProductImage) -> None:
        self.images.remove(image)


class FakeStorage:
    def __init__(self, session: FakeSession, fail_on_put: int | None = None) -> None:
        self.session = session
        self.fail_on_put = fail_on_put
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.fail_delete_key: str | None = None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        assert not self.session.active, "S3 upload must not hold a DB transaction"
        assert content_type in {"image/webp", "model/gltf-binary"}
        self.put_calls.append(key)
        if len(self.put_calls) == self.fail_on_put:
            raise OSError("S3 upload failed")
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        assert not self.session.active, "S3 cleanup must not hold a DB transaction"
        self.delete_calls.append(key)
        if key == self.fail_delete_key:
            raise OSError("S3 deletion failed")
        self.objects.pop(key, None)


def make_image(product_id: UUID, position: int) -> ProductImage:
    image_id = uuid4()
    return ProductImage(
        id=image_id,
        product_id=product_id,
        position=position,
        large_object_key=f"images/{image_id}/large.webp",
        thumbnail_object_key=f"images/{image_id}/thumbnail.webp",
    )


@pytest.fixture
def setup_media(monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple]:
    monkeypatch.setattr(
        "catalog_service.services.product_media.process_product_image",
        lambda _: ProcessedProductImage(large=b"large", thumbnail=b"thumbnail"),
    )
    monkeypatch.setattr(
        "catalog_service.services.product_media.validate_product_glb", lambda _: None
    )

    def build(
        *,
        exists: bool = True,
        images: list[ProductImage] | None = None,
        fail_on_put: int | None = None,
        model_key: str | None = None,
        fail_commit: bool = False,
    ) -> tuple[ProductMediaService, FakeSession, FakeImages, FakeStorage]:
        session = FakeSession(fail_commit=fail_commit)
        image_repo = FakeImages(images)
        storage = FakeStorage(session, fail_on_put=fail_on_put)
        service = ProductMediaService(
            session=session,  # type: ignore[arg-type]
            products=FakeProducts(exists, model_key),  # type: ignore[arg-type]
            cache=AsyncMock(spec=CacheProduct),
            images=image_repo,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
        )
        return service, session, image_repo, storage

    return build


async def test_add_image_saves_two_objects_and_image_row(setup_media: Callable[..., tuple]) -> None:
    product_id = uuid4()
    service, session, images, storage = setup_media()

    image = await service.add_image(product_id, b"input")

    assert image.position == 0
    assert image.product_id == product_id
    assert images.images == [image]
    assert storage.objects == {
        image.large_object_key: b"large",
        image.thumbnail_object_key: b"thumbnail",
    }
    assert storage.delete_calls == []
    assert session.begin_calls == 1
    service._cache.invalidate_product.assert_awaited_once_with(product_id)


async def test_second_upload_failure_cleans_attempted_keys(
    setup_media: Callable[..., tuple],
) -> None:
    service, session, images, storage = setup_media(fail_on_put=2)

    with pytest.raises(OSError, match="S3 upload failed"):
        await service.add_image(uuid4(), b"input")

    assert session.begin_calls == 0
    assert images.images == []
    assert storage.objects == {}
    assert storage.delete_calls == list(reversed(storage.put_calls))
    service._cache.invalidate_product.assert_not_awaited()


async def test_unknown_product_cleans_uploaded_objects(setup_media: Callable[..., tuple]) -> None:
    service, _, images, storage = setup_media(exists=False)

    with pytest.raises(ProductNotFoundError):
        await service.add_image(uuid4(), b"input")

    assert images.images == []
    assert storage.objects == {}
    assert len(storage.delete_calls) == 2
    service._cache.invalidate_product.assert_not_awaited()


async def test_ninth_image_is_rejected_and_cleaned(setup_media: Callable[..., tuple]) -> None:
    product_id = uuid4()
    existing = [make_image(product_id, position) for position in range(8)]
    service, _, images, storage = setup_media(images=existing)

    with pytest.raises(ProductImageLimitError):
        await service.add_image(product_id, b"input")

    assert images.images == existing
    assert storage.objects == {}
    assert len(storage.delete_calls) == 2
    service._cache.invalidate_product.assert_not_awaited()


async def test_delete_image_compacts_positions_then_removes_objects(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    first, middle, last = [make_image(product_id, position) for position in range(3)]
    service, session, images, storage = setup_media(images=[first, middle, last])
    storage.objects = {middle.large_object_key: b"large", middle.thumbnail_object_key: b"thumb"}

    await service.delete_image(product_id, middle.id)

    assert images.images == [first, last]
    assert [image.position for image in images.images] == [0, 1]
    assert storage.delete_calls == [middle.large_object_key, middle.thumbnail_object_key]
    assert storage.objects == {}
    assert session.begin_calls == 1
    service._cache.invalidate_product.assert_awaited_once_with(product_id)


async def test_delete_unknown_image_does_not_touch_storage(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    service, _, _, storage = setup_media()

    with pytest.raises(ProductImageNotFoundError):
        await service.delete_image(product_id, uuid4())

    assert storage.delete_calls == []


async def test_s3_delete_failure_does_not_undo_committed_image_deletion(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    image = make_image(product_id, 0)
    service, _, images, storage = setup_media(images=[image])
    storage.fail_delete_key = image.large_object_key

    await service.delete_image(product_id, image.id)

    assert images.images == []
    assert storage.delete_calls == [image.large_object_key, image.thumbnail_object_key]


async def test_reorder_images_updates_positions_without_touching_storage(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    first, second, third = [make_image(product_id, position) for position in range(3)]
    service, session, images, storage = setup_media(images=[first, second, third])

    reordered = await service.reorder_images(product_id, [third.id, first.id, second.id])

    assert [image.id for image in reordered] == [third.id, first.id, second.id]
    assert [(image.id, image.position) for image in images.images] == [
        (first.id, 1),
        (second.id, 2),
        (third.id, 0),
    ]
    assert session.begin_calls == 1
    assert storage.put_calls == storage.delete_calls == []
    service._cache.invalidate_product.assert_awaited_once_with(product_id)


@pytest.mark.parametrize("ids", ["missing", "duplicate", "foreign"])
async def test_reorder_rejects_nonmatching_gallery_without_changes(
    setup_media: Callable[..., tuple], ids: str,
) -> None:
    product_id = uuid4()
    first, second = [make_image(product_id, position) for position in range(2)]
    service, _, images, storage = setup_media(images=[first, second])
    requested_ids = {
        "missing": [first.id],
        "duplicate": [first.id, first.id],
        "foreign": [first.id, uuid4()],
    }[ids]

    with pytest.raises(ProductImageOrderConflictError):
        await service.reorder_images(product_id, requested_ids)

    assert [image.position for image in images.images] == [0, 1]
    assert storage.put_calls == storage.delete_calls == []


async def test_reorder_unknown_product_does_not_change_images(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    image = make_image(product_id, 0)
    service, _, images, _ = setup_media(exists=False, images=[image])

    with pytest.raises(ProductNotFoundError):
        await service.reorder_images(product_id, [image.id])

    assert images.images[0].position == 0


async def test_model_upload_and_replace_use_new_key_and_cleanup_old(
    setup_media: Callable[..., tuple],
) -> None:
    product_id = uuid4()
    service, session, _, storage = setup_media()
    product = service._products.product  # type: ignore[attr-defined]

    first_key = await service.replace_model(product_id, b"first")
    second_key = await service.replace_model(product_id, b"second")

    assert first_key != second_key
    assert first_key.startswith(f"products/{product_id}/models/")
    assert second_key.endswith(".glb")
    assert product.model_3d_key == second_key
    assert storage.objects == {second_key: b"second"}
    assert storage.delete_calls == [first_key]
    assert session.begin_calls == 2
    assert service._cache.invalidate_product.await_count == 2


async def test_model_upload_failure_does_not_start_db_transaction(
    setup_media: Callable[..., tuple],
) -> None:
    service, session, _, storage = setup_media(fail_on_put=1)

    with pytest.raises(OSError, match="S3 upload failed"):
        await service.replace_model(uuid4(), b"model")

    assert session.begin_calls == 0
    assert storage.objects == {}


async def test_invalid_model_is_rejected_before_s3_or_db(
    setup_media: Callable[..., tuple], monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, _, storage = setup_media()

    def reject(_: bytes) -> None:
        raise InvalidProductModelError("Invalid GLB")

    monkeypatch.setattr("catalog_service.services.product_media.validate_product_glb", reject)

    with pytest.raises(InvalidProductModelError):
        await service.replace_model(uuid4(), b"invalid")

    assert session.begin_calls == 0
    assert storage.put_calls == storage.delete_calls == []


async def test_model_db_failure_cleans_new_key_but_not_old(
    setup_media: Callable[..., tuple],
) -> None:
    old_key = "products/old.glb"
    service, session, _, storage = setup_media(model_key=old_key, fail_commit=True)

    with pytest.raises(OSError, match="DB commit failed"):
        await service.replace_model(uuid4(), b"model")

    assert session.begin_calls == 1
    assert storage.objects == {}
    assert storage.delete_calls == storage.put_calls
    assert old_key not in storage.delete_calls


async def test_model_unknown_product_cleans_uploaded_file(
    setup_media: Callable[..., tuple],
) -> None:
    service, _, _, storage = setup_media(exists=False)
    with pytest.raises(ProductNotFoundError):
        await service.replace_model(uuid4(), b"model")
    assert storage.objects == {}
    assert storage.delete_calls == storage.put_calls


async def test_delete_model_is_repeatable_and_does_not_hold_transaction_during_s3(
    setup_media: Callable[..., tuple],
) -> None:
    key = "products/existing.glb"
    service, session, _, storage = setup_media(model_key=key)
    product = service._products.product  # type: ignore[attr-defined]
    storage.objects[key] = b"model"

    await service.delete_model(uuid4())
    await service.delete_model(uuid4())

    assert product.model_3d_key is None
    assert storage.delete_calls == [key]
    assert storage.objects == {}
    assert session.begin_calls == 2
    assert service._cache.invalidate_product.await_count == 2


async def test_model_cleanup_failure_does_not_undo_replacement(
    setup_media: Callable[..., tuple],
) -> None:
    old_key = "products/old.glb"
    service, _, _, storage = setup_media(model_key=old_key)
    storage.fail_delete_key = old_key

    new_key = await service.replace_model(uuid4(), b"model")

    assert service._products.product.model_3d_key == new_key  # type: ignore[attr-defined]
    assert storage.objects[new_key] == b"model"
