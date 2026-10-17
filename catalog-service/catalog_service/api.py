from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from catalog_service.routers.dependencies import get_settings
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from redis.asyncio import Redis

from catalog_service.config import MediaSettings
from catalog_service.db.session import async_engine
from catalog_service.exceptions import (
    InsufficientStockError,
    InvalidAccessTokenError,
    PermissionDeniedError,
    ProductImageLimitError,
    ProductImageNotFoundError,
    ProductImageOrderConflictError,
    ProductNotFoundError,
)
from catalog_service.media.glb_validation import InvalidProductModelError
from catalog_service.media.image_processing import InvalidProductImageError
from catalog_service.observability import install_http_logging
from catalog_service.routers.admin_products import router as admin_products_router
from catalog_service.routers.products import router as products_router
from catalog_service.storage.s3 import create_s3_storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    media_settings = MediaSettings()
    try:
        async with create_s3_storage(settings=media_settings) as storage:
            app.state.media_storage = storage
            app.state.redis = Redis.from_url(
                get_settings().redis_url, 
                decode_responses=True, 
                socket_connect_timeout=1.0, 
                socket_timeout=1.0
            )
            yield
    finally:
        await app.state.redis.aclose()
        await async_engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    title="Product Service",
    version="0.1.0"
)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

@app.exception_handler(ProductNotFoundError)
async def handle_product_not_found(
    request: Request,
    exc: ProductNotFoundError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )

@app.exception_handler(InsufficientStockError)
async def handle_product_infficient_stock(
    request: Request,
    exc: InsufficientStockError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )

@app.exception_handler(ProductImageLimitError)
async def handle_product_image_limit(
    request: Request,
    exc: ProductImageLimitError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )

@app.exception_handler(InvalidProductImageError)
async def handle_invalid_product_image(
    request: Request,
    exc: InvalidProductImageError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(exc)},
    )

@app.exception_handler(ProductImageNotFoundError)
async def handle_product_image_not_found(
    request: Request,
    exc: ProductImageNotFoundError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


@app.exception_handler(InvalidProductModelError)
async def handle_invalid_product_model(
    request: Request,
    exc: InvalidProductModelError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(exc)},
    )


@app.exception_handler(ProductImageOrderConflictError)
async def handle_product_image_order_conflict(
    request: Request,
    exc: ProductImageOrderConflictError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )

@app.exception_handler(InvalidAccessTokenError)
async def handle_invalid_acces_token(
    request: Request,
    exc: InvalidAccessTokenError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc)},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(PermissionDeniedError)
async def handle_permission_denied(
    request: Request,
    exc: PermissionDeniedError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": str(exc)},
    )


install_http_logging(app)
app.include_router(products_router)
app.include_router(admin_products_router)

