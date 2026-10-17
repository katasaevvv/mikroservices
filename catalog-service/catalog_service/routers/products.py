from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from catalog_service.routers.dependencies import ServiceDependency
from catalog_service.schemas.products import (
    ProductBatchRequest,
    ProductBatchResponse,
    ProductListResponse,
    ProductPublicRead,
    ProductSnapshot,
)

LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]

router = APIRouter(
    prefix="/products", 
    tags=["Products"], 
)


@router.post(
    "/batch",
    response_model=ProductBatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Получить список Товаров по ID"
)
async def get_batch(
    data: ProductBatchRequest,
    service: ServiceDependency,
) -> ProductBatchResponse:
    products = await service.get_products_by_ids(product_ids=data.product_ids)
    return ProductBatchResponse(
        products=[ProductSnapshot.model_validate(product) for product in products],
    )


@router.get(
    "",
    response_model=ProductListResponse,
    status_code=status.HTTP_200_OK,
    summary="Получить все товары",
)
async def get_products(
    service: ServiceDependency,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> ProductListResponse:
    return await service.get_products(
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{product_id}",
    response_model=ProductPublicRead,
    status_code=status.HTTP_200_OK,
    summary="Получить товар по ID",
)
async def get_by_id(
    product_id: UUID,
    service: ServiceDependency,
) -> ProductPublicRead:
    product = await service.get_product_by_id(product_id=product_id)
    return product
