from catalog_service.db.models.products import Product
from catalog_service.schemas.products import ProductImagePublicRead, ProductPublicRead, ProductRead


def to_public_product(product: Product) -> ProductPublicRead:
    return ProductPublicRead(
        **ProductRead.model_validate(product).model_dump(),
        images=[
            ProductImagePublicRead(
                id=image.id,
                position=image.position,
                url=f"/media/{image.large_object_key}",
                thumbnail_url=f"/media/{image.thumbnail_object_key}",
            )
            for image in sorted(product.images, key=lambda image: image.position)
        ],
        model_3d_url=f"/media/{product.model_3d_key}" if product.model_3d_key else None,
    )