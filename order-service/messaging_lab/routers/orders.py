from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from messaging_lab.config import Settings
from messaging_lab.db.models.order import Order
from messaging_lab.schemas.order import OrderCreate, OrderRead
from messaging_lab.services.orders import CreateOrderItem
from messaging_lab.routers.dependencies import CurrentPrincipalDependency, OrderServiceDependency

router = APIRouter(tags=["Orders"], prefix="/orders")

LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]

settings = Settings()

def get_client_address(request: Request) -> str:
    real_ip = request.headers.get("x-real-ip")
    if real_ip is not None:
        return real_ip
    return get_remote_address(request)

limiter = Limiter(key_func=get_client_address, storage_uri=settings.redis_url, strategy="moving-window")

@router.post(
    "",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Создать новый заказ",
)
@limiter.limit("3/minute")
async def create_order(
    request: Request,
    data: OrderCreate,
    current_customer: CurrentPrincipalDependency,
    service: OrderServiceDependency,
    idempotency_key: Annotated[str, 
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
) -> Order:
    items = [
        CreateOrderItem(
            product_id=item.product_id,
            quantity=item.quantity,
        )
        for item in data.items
    ]
    return await service.create_order(
        customer_id=current_customer.sub, 
        receipt_email=str(data.receipt_email),
        idempotency_key=idempotency_key,
        items=items
    )


@router.get(
    "/my",
    response_model=list[OrderRead],
    status_code=status.HTTP_200_OK,
    summary="Получить мои заказы"
)
async def get_my_orders(
    current_customer: CurrentPrincipalDependency,
    service: OrderServiceDependency,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> list[Order]:
    return await service.get_orders_by_customer(customer_id=current_customer.sub, limit=limit, offset=offset)



