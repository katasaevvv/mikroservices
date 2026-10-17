from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from catalog_service.api import app
from catalog_service.db.models.product_image import ProductImage
from catalog_service.db.models.products import Product
from catalog_service.exceptions import ProductImageOrderConflictError
from catalog_service.mappers.products import to_public_product
from catalog_service.media.glb_validation import InvalidProductModelError
from catalog_service.routers.dependencies import (
    get_current_principal,
    get_media_service,
    get_product_service,
    get_token_verifier,
)
from catalog_service.schemas.products import ProductListResponse


@pytest.fixture
def isolated_app():
    app.dependency_overrides.clear()
    yield app
    app.dependency_overrides.clear()


def product() -> Product:
    return Product(
        id=uuid4(),
        category="books",
        name="Test book",
        description=None,
        price=Decimal("100.00"),
        stock_quantity=5,
        is_active=True,
    )


async def test_public_batch_returns_product_snapshot(isolated_app: object) -> None:
    item = product()
    service = SimpleNamespace(get_products_by_ids=AsyncMock(return_value=[item]))
    app.dependency_overrides[get_product_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/products/batch", json={"product_ids": [str(item.id)]})

    assert response.status_code == 200
    assert response.json()["products"][0]["id"] == str(item.id)
    assert response.json()["products"][0]["stock_quantity"] == 5
    service.get_products_by_ids.assert_awaited_once_with(product_ids={item.id})


async def test_public_product_returns_ordered_image_urls(isolated_app: object) -> None:
    item = product()
    first_id, second_id = uuid4(), uuid4()
    item.images = [
        ProductImage(
            id=second_id,
            product_id=item.id,
            position=1,
            large_object_key=f"products/{item.id}/images/{second_id}/large.webp",
            thumbnail_object_key=f"products/{item.id}/images/{second_id}/thumbnail.webp",
        ),
        ProductImage(
            id=first_id,
            product_id=item.id,
            position=0,
            large_object_key=f"products/{item.id}/images/{first_id}/large.webp",
            thumbnail_object_key=f"products/{item.id}/images/{first_id}/thumbnail.webp",
        ),
    ]
    service = SimpleNamespace(get_product_by_id=AsyncMock(return_value=to_public_product(item)))
    app.dependency_overrides[get_product_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/products/{item.id}")

    assert response.status_code == 200
    body = response.json()
    assert [image["id"] for image in body["images"]] == [str(first_id), str(second_id)]
    assert body["images"][0]["position"] == 0
    assert body["images"][0]["url"] == f"/media/products/{item.id}/images/{first_id}/large.webp"
    assert body["images"][0]["thumbnail_url"] == (
        f"/media/products/{item.id}/images/{first_id}/thumbnail.webp"
    )
    assert body["model_3d_url"] is None


async def test_public_product_list_supports_products_without_images(isolated_app: object) -> None:
    item = product()
    item.images = []
    service = SimpleNamespace(
        get_products=AsyncMock(
            return_value=ProductListResponse(
                items=[to_public_product(item)],
                total=1,
                limit=20,
                offset=0,
            )
        )
    )
    app.dependency_overrides[get_product_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/products")

    assert response.status_code == 200
    assert response.json()["items"][0]["images"] == []
    assert response.json()["items"][0]["model_3d_url"] is None
    assert response.json()["total"] == 1
    assert response.json()["limit"] == 20
    assert response.json()["offset"] == 0
    service.get_products.assert_awaited_once_with(limit=20, offset=0)


async def test_public_product_exposes_model_url(isolated_app: object) -> None:
    item = product()
    item.images = []
    item.model_3d_key = f"products/{item.id}/models/{uuid4()}.glb"
    service = SimpleNamespace(get_product_by_id=AsyncMock(return_value=to_public_product(item)))
    app.dependency_overrides[get_product_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/products/{item.id}")

    assert response.status_code == 200
    assert response.json()["model_3d_url"] == f"/media/{item.model_3d_key}"


async def test_admin_product_create_requires_token(isolated_app: object) -> None:
    app.dependency_overrides[get_token_verifier] = lambda: object()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/products",
            json={"category": "books", "name": "Test", "price": "100.00", "stock_quantity": 5},
        )

    assert response.status_code == 401


async def test_non_admin_cannot_create_product(isolated_app: object) -> None:
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="customer")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/products",
            json={"category": "books", "name": "Test", "price": "100.00", "stock_quantity": 5},
        )

    assert response.status_code == 403


async def test_admin_can_create_product(isolated_app: object) -> None:
    item = product()
    service = SimpleNamespace(create_product=AsyncMock(return_value=item))
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_product_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/products",
            json={"category": "books", "name": "Test book", "price": "100.00", "stock_quantity": 5},
        )

    assert response.status_code == 201
    assert response.json()["id"] == str(item.id)
    service.create_product.assert_awaited_once()


async def test_admin_reorders_images(isolated_app: object) -> None:
    product_id = uuid4()
    first, second = [
        ProductImage(id=uuid4(), product_id=product_id, position=position,
                     large_object_key="large", thumbnail_object_key="thumbnail")
        for position in range(2)
    ]
    second.position, first.position = 0, 1
    service = SimpleNamespace(reorder_images=AsyncMock(return_value=[second, first]))
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/images/order",
            json={"image_ids": [str(second.id), str(first.id)]},
        )

    assert response.status_code == 200
    assert response.json() == [
        {"id": str(second.id), "position": 0},
        {"id": str(first.id), "position": 1},
    ]
    service.reorder_images.assert_awaited_once_with(product_id, [second.id, first.id])


async def test_reorder_duplicate_ids_is_422(isolated_app: object) -> None:
    product_id, image_id = uuid4(), uuid4()
    service = SimpleNamespace(reorder_images=AsyncMock())
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/images/order",
            json={"image_ids": [str(image_id), str(image_id)]},
        )

    assert response.status_code == 422
    service.reorder_images.assert_not_awaited()


async def test_reorder_stale_gallery_is_409(isolated_app: object) -> None:
    product_id = uuid4()
    service = SimpleNamespace(
        reorder_images=AsyncMock(side_effect=ProductImageOrderConflictError())
    )
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/images/order",
            json={"image_ids": [str(uuid4())]},
        )

    assert response.status_code == 409


async def test_non_admin_cannot_reorder_images(isolated_app: object) -> None:
    product_id = uuid4()
    service = SimpleNamespace(reorder_images=AsyncMock())
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="customer")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/images/order", json={"image_ids": []}
        )

    assert response.status_code == 403
    service.reorder_images.assert_not_awaited()


async def test_admin_uploads_model_and_receives_public_url(isolated_app: object) -> None:
    product_id = uuid4()
    key = f"products/{product_id}/models/{uuid4()}.glb"
    service = SimpleNamespace(replace_model=AsyncMock(return_value=key))
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/model-3d",
            files={"file": ("model.glb", b"model", "application/octet-stream")},
        )

    assert response.status_code == 200
    assert response.json() == {"model_3d_url": f"/media/{key}"}
    service.replace_model.assert_awaited_once_with(product_id, b"model")


async def test_invalid_model_is_422(isolated_app: object) -> None:
    product_id = uuid4()
    service = SimpleNamespace(
        replace_model=AsyncMock(side_effect=InvalidProductModelError("Invalid GLB"))
    )
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/model-3d",
            files={"file": ("fake.glb", b"broken", "model/gltf-binary")},
        )

    assert response.status_code == 422


async def test_non_admin_cannot_upload_model(isolated_app: object) -> None:
    product_id = uuid4()
    service = SimpleNamespace(replace_model=AsyncMock())
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="customer")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/admin/products/{product_id}/model-3d",
            files={"file": ("model.glb", b"model")},
        )

    assert response.status_code == 403
    service.replace_model.assert_not_awaited()


async def test_admin_deletes_model(isolated_app: object) -> None:
    product_id = uuid4()
    service = SimpleNamespace(delete_model=AsyncMock())
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(role="admin")
    app.dependency_overrides[get_media_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(f"/admin/products/{product_id}/model-3d")

    assert response.status_code == 204
    service.delete_model.assert_awaited_once_with(product_id)
