import asyncio
import logging
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.db.models.product_image import ProductImage
from catalog_service.exceptions import (
    ProductImageLimitError,
    ProductImageNotFoundError,
    ProductImageOrderConflictError,
    ProductNotFoundError,
)
from catalog_service.cache.cache_product import CacheProduct
from catalog_service.media.glb_validation import validate_product_glb
from catalog_service.media.image_processing import process_product_image
from catalog_service.repositories.product_image import ProductImageRepository
from catalog_service.repositories.products import ProductRepository
from catalog_service.storage.s3 import S3MediaStorage

logger = logging.getLogger(__name__)



class ProductMediaService:
    def __init__(
        self,
        session: AsyncSession,
        products: ProductRepository,
        cache: CacheProduct,
        images: ProductImageRepository,
        storage: S3MediaStorage,
    ) -> None:
        self._session = session
        self._products = products
        self._cache = cache
        self._images = images
        self._storage = storage

    async def add_image(self, product_id: UUID, file_bytes: bytes) -> ProductImage:
        processed = await asyncio.to_thread(process_product_image, file_bytes)

        image_id = uuid4()
        prefix = f"products/{product_id}/images/{image_id}"
        large_key = f"{prefix}/large.webp"
        thumbnail_key = f"{prefix}/thumbnail.webp"

        attempted_keys: list[str] = []
        committed = False

        try:
            attempted_keys.append(large_key)
            await self._storage.put(large_key, processed.large, "image/webp")

            attempted_keys.append(thumbnail_key)
            await self._storage.put(thumbnail_key, processed.thumbnail, "image/webp")

            async with self._session.begin():
                product = await self._products.get_by_id_for_update(product_id)
                if product is None:
                    raise ProductNotFoundError(product_id)

                existing = await self._images.list_by_product_id(product_id)
                occupied = {image.position for image in existing}
                free_position = next(
                    (position for position in range(8) if position not in occupied),
                    None,
                )
                if free_position is None:
                    raise ProductImageLimitError()

                image = ProductImage(
                    id=image_id,
                    product_id=product_id,
                    large_object_key=large_key,
                    thumbnail_object_key=thumbnail_key,
                    position=free_position,
                )

                await self._images.add(image)

            committed = True
            await self._cache.invalidate_product(product_id)
            return image

        finally:
            if not committed:
                for key in reversed(attempted_keys):
                    try:
                        await self._storage.delete(key)
                    except Exception:
                        logger.exception("media.cleanup.failed")

    async def delete_image(self, product_id: UUID, image_id: UUID) -> None:
        async with self._session.begin():
            product = await self._products.get_by_id_for_update(product_id)
            if product is None:
                raise ProductNotFoundError(product_id)

            image = await self._images.get_by_id(image_id, product_id)
            if not image:
                raise ProductImageNotFoundError(image_id)

            keys = (image.large_object_key, image.thumbnail_object_key)
            await self._images.delete(image)

            remaining = await self._images.list_by_product_id(product_id)
            for position, remaining_image in enumerate(remaining):
                remaining_image.position = position

        await self._cache.invalidate_product(product_id)
        
        for key in keys:
            try:
                await self._storage.delete(key)
            except Exception:
                logger.exception("media.cleanup.failed")
        

    async def reorder_images(
        self, product_id: UUID, image_ids: list[UUID]
    ) -> list[ProductImage]:
        async with self._session.begin():
            product = await self._products.get_by_id_for_update(product_id)
            if product is None:
                raise ProductNotFoundError(product_id)

            current_images = await self._images.list_by_product_id(product_id)
            images_by_id = {image.id: image for image in current_images}
            if len(image_ids) != len(current_images) or set(image_ids) != set(images_by_id):
                raise ProductImageOrderConflictError()

            ordered_images = [images_by_id[image_id] for image_id in image_ids]
            for position, image in enumerate(ordered_images):
                image.position = position

        await self._cache.invalidate_product(product_id)

        return ordered_images

    async def replace_model(self, product_id: UUID, file_bytes: bytes) -> str:
        await asyncio.to_thread(validate_product_glb, file_bytes)
        new_key = f"products/{product_id}/models/{uuid4()}.glb"
        old_key: str | None = None
        committed = False

        try:
            await self._storage.put(new_key, file_bytes, "model/gltf-binary")
            async with self._session.begin():
                product = await self._products.get_by_id_for_update(product_id)
                if product is None:
                    raise ProductNotFoundError(product_id)
                old_key = product.model_3d_key
                product.model_3d_key = new_key
            committed = True
        finally:
            if not committed:
                try:
                    await self._storage.delete(new_key)
                except Exception:
                    logger.exception("media.cleanup.failed", extra={"product_id": str(product_id)})

        if old_key is not None:
            try:
                await self._storage.delete(old_key)
            except Exception:
                logger.exception("media.cleanup.failed", extra={"product_id": str(product_id)})
        await self._cache.invalidate_product(product_id)
        return new_key

    async def delete_model(self, product_id: UUID) -> None:
        async with self._session.begin():
            product = await self._products.get_by_id_for_update(product_id)
            if product is None:
                raise ProductNotFoundError(product_id)
            old_key = product.model_3d_key
            product.model_3d_key = None

        if old_key is not None:
            try:
                await self._storage.delete(old_key)
            except Exception:
                logger.exception("media.cleanup.failed", extra={"product_id": str(product_id)})
                
        await self._cache.invalidate_product(product_id)
