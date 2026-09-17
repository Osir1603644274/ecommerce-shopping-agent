"""Read-only discovery plus explicitly confirmed one-per-user flash purchase."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from .commerce_workspace import _identity
from . import commerce_demo as auth

router = APIRouter(prefix="/api/commerce-demo/workspace/benefits")


def ids(value):
    # Preserve Java BIGINT identifiers before the browser parses JSON numbers.
    if isinstance(value, list):
        return [ids(v) for v in value]
    if isinstance(value, dict):
        return {k: str(v) if k in {"id", "itemId", "campaignId", "templateId", "orderId"} and v is not None else ids(v)
                for k, v in value.items()}
    return value


@router.get("")
async def benefits(request: Request, response: Response):
    _, _, session = await _identity(request, response, authenticated=True)
    token = session["accessToken"]
    return {"coupons": ids(await auth._java("GET", "/api/coupons/mine", access_token=token)),
            "campaigns": ids(await auth._java("GET", "/api/flash-sales", access_token=token)),
            "purchases": ids(await auth._java("GET", "/api/flash-sales/orders", access_token=token))}


class Purchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


@router.post("/{campaign_id}/purchase")
async def purchase(campaign_id: str, body: Purchase, request: Request, response: Response):
    _, _, session = await _identity(request, response, authenticated=True)
    if not campaign_id.isascii() or not campaign_id.isdigit() or not 0 < int(campaign_id) <= 9223372036854775807:
        raise HTTPException(422, "invalid campaign")
    if body.confirmation != "确认参加抢购":
        raise HTTPException(400, "需要明确确认参加抢购")
    # Natural business identity is (campaignId, authenticated user). No automatic retry.
    return ids(await auth._java("POST", f"/api/flash-sales/{campaign_id}/purchase", access_token=session["accessToken"]))
