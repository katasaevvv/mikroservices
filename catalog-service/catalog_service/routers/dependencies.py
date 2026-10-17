from typing import Annotated, cast
from functools import lru_cache


from catalog_service.repositories.product_image import ProductImageRepository
from catalog_service.services.product_media import ProductMediaService
from catalog_service.storage.s3 import S3MediaStorage
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.cache.cache_product import CacheProduct
from catalog_service.db.session import get_session
from catalog_service.config import Settings
from catalog_service.security.tokens import TokenVerifier
from catalog_service.schemas.tokens import AccessTokenPayload
from catalog_service.repositories.products import ProductRepository
from catalog_service.services.products import ProductsService
from catalog_service.exceptions import InvalidAccessTokenError, PermissionDeniedError


bearer_scheme = HTTPBearer(auto_error=False)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

@lru_cache(maxsize=1)
def get_token_verifier() -> TokenVerifier:
    settings = get_settings()

    public_key = settings.jwt_public_key_path.read_text(encoding="utf-8")

    return TokenVerifier(
        public_key=public_key,
        algorithm=settings.jwt_algorithm,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience
    )

CredentialsDependency = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
TokenVerifierDependency = Annotated[TokenVerifier, Depends(get_token_verifier)]

def get_current_principal(
    credentials: CredentialsDependency,
    token_verifier: TokenVerifierDependency,
) -> AccessTokenPayload:
    if credentials is None:
        raise InvalidAccessTokenError()
    return token_verifier.decode_access_token(credentials.credentials)

CurrentPrincipalDependency = Annotated[AccessTokenPayload, Depends(get_current_principal)]

def require_admin(
   current_customer: CurrentPrincipalDependency 
) -> None:
    if current_customer.role != "admin":
        raise PermissionDeniedError()


SessionDependency = Annotated[AsyncSession, Depends(get_session)]

async def get_product_service(session: SessionDependency, request: Request) -> ProductsService:
    return ProductsService(
        session=session,
        cache=CacheProduct(request.app.state.redis),
        repository=ProductRepository(session=session),
    )

ServiceDependency = Annotated[ProductsService, Depends(get_product_service)]


async def get_media_service(
    request: Request,
    session: SessionDependency,
) -> ProductMediaService:
    storage = cast(S3MediaStorage, request.app.state.media_storage)
    return ProductMediaService(
        session=session,
        products=ProductRepository(session),
        cache=CacheProduct(request.app.state.redis),
        images=ProductImageRepository(session),
        storage=storage,
    )

MediaServiceDependency = Annotated[ProductMediaService, Depends(get_media_service)]