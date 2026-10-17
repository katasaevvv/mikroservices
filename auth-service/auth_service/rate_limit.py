from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from auth_service.config import Settings

settings = Settings()

def get_client_address(request: Request) -> str:
    real_ip = request.headers.get("x-real-ip")
    if real_ip is not None:
        return real_ip
    return get_remote_address(request)

limiter = Limiter(
    key_func=get_client_address, 
    storage_uri=settings.redis_url,
    strategy="moving-window",
)