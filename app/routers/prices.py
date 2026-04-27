from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from price_aggregator import aggregate_prices
from app.config import settings

router = APIRouter(tags=["prices"])


class PriceRequest(BaseModel):
    brand: str = Field(..., min_length=1, examples=["toyota"])
    model: str = Field(..., min_length=1, examples=["camry"])
    year: int = Field(..., ge=1990, le=2100, examples=[2025])
    enrich_with_ai: bool = Field(
        False,
        description=(
            "If true, Claude generates `marketInsight` + "
            "`competitorAnalysis` from the real aggregated data. "
            "Adds ~15-20s to the request."
        ),
    )


@router.post(
    "/prices",
    response_model=None,
    summary="Aggregate per-trim prices from 5 KSA sources",
    description=(
        "For a given (brand, model, year):\n\n"
        "1. **Official MSRP** from `toyota.com.sa` or `lexus.com.sa`\n"
        "2. **Marketplace listings** from `haraj.com.sa`, `syarah.com`, "
        "`ksa.Motory.com`, `ksa.yallamotor.com`\n\n"
        "Listings are attributed to the official trim list using a "
        "multi-pass matcher (strict word-boundary + engine/hyphen/variant "
        "relaxed + price proximity). Returns the market-intelligence "
        "schema with per-listing detail and per-source reference URLs.\n\n"
        "Typical latency: ~50s without AI, ~70s with `enrich_with_ai=true`."
    ),
)
async def get_prices(payload: PriceRequest) -> dict[str, Any]:
    try:
        return await aggregate_prices(
            payload.brand,
            payload.model,
            payload.year,
            enrich_with_ai=payload.enrich_with_ai,
            anthropic_key=settings.anthropic_api_key if payload.enrich_with_ai else "",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
