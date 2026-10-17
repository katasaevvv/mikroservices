from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.mappers.products import to_public_product
from catalog_service.db.models.products import Product
from catalog_service.repositories.products import ProductRepository
from catalog_service.exceptions import InsufficientStockError, ProductNotFoundError
from catalog_service.schemas.products import ProductCreate, ProductListResponse, ProductPublicRead, ProductUpdate


class ProductsService:
    def __init__(
        self, 
        session: AsyncSession,
        cache: CacheProduct,
        repository: ProductRepository,
    ):
        self._session = session
        self._cache = cache
        self._repository = repository


    async def create_product(self, product_data: ProductCreate) -> Product:
        product = Product(
            id=uuid4(),
            **product_data.model_dump(),
        )
        async with self._session.begin():
            new_product = await self._repository.add(product)

            await self._cache.invalidate_lists()

        return new_product

    async def get_product_by_id(self, product_id: UUID) -> ProductPublicRead:
        cache_product_key = self._cache.build_product_cache_key(product_id)
        cache_product = await self._cache.get_cache(cache_product_key)

        if cache_product is not None:
            return ProductPublicRead.model_validate_json(cache_product)
        
        async with self._session.begin():
            product = await self._repository.get_by_id_with_images(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=product_id)
            dto = to_public_product(product)

        await self._cache.set_cache(cache_product_key, dto)
        return dto

    async def get_products_by_ids(self, product_ids: set[UUID]) -> list[Product]:
        async with self._session.begin():
            products = await self._repository.get_by_ids(product_ids)

        return products

    async def decrease_product_stock(self, product_id: UUID, quantity: int) -> int:
        async with self._session.begin():
            product = await self._repository.get_by_id(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=product_id)
            product_quantity = await self._repository.set_stock_quantity(product_id=product_id, quantity=quantity)
            if product_quantity is None:
                raise InsufficientStockError(product_id=product_id, quantity=quantity, stock_quantity=product.stock_quantity) 
        
        await self._cache.invalidate_product(product_id)

        return product_quantity

    async def update_product(self, product_id: UUID, product_data: ProductUpdate) -> Product:
        async with self._session.begin():
            product = await self._repository.get_by_id(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=product_id)

            changes = product_data.model_dump(exclude_unset=True)

            for field_name, value in changes.items():
                setattr(product, field_name, value)

            await self._session.flush()

        await self._cache.invalidate_product(product_id)
                

        return product

    async def deactivate_product(self, product_id: UUID) -> None:
        async with self._session.begin():
            product = await self._repository.get_by_id(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=product_id)

            if not product.is_active:
                return

            product.is_active = False

        await self._cache.invalidate_product(product_id)

    async def activate_product(self, product_id: UUID) -> Product:
        async with self._session.begin():
            product = await self._repository.get_by_id(product_id)

            if product is None:
                raise ProductNotFoundError(product_id=product_id)

            if product.is_active is True:
                return product

            product.is_active = True

        await self._cache.invalidate_product(product_id)

        return product

    async def get_products(
        self,
        limit: int,
        offset: int,
    ) -> ProductListResponse:

        cache_key = self._cache.build_products_list_cache_key(limit=limit, offset=offset)
        cache = await self._cache.get_cache(cache_key)
        if cache is not None:
            return ProductListResponse.model_validate_json(cache)
        
        async with self._session.begin():
            products = await self._repository.get_list_products(
                limit=limit,
                offset=offset,
            )
            total = await self._repository.count_active()

        response = ProductListResponse(
                items=[to_public_product(product) for product in products],
                total=total,
                limit=limit,
                offset=offset,
        )

        await self._cache.set_cache(cache_key, response)

        return response


