import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import claude

router = APIRouter(tags=["scrape"])


class ToyotaScrapeRequest(BaseModel):
    make: str = Field(..., min_length=1, examples=["toyota"])
    model: str = Field(..., min_length=1, examples=["fortuner"])
    year: int = Field(..., ge=1990, le=2100, examples=[2026])


class HarajScrapeRequest(ToyotaScrapeRequest):
    trims: list[str] = Field(
        ..., min_length=1, examples=[["GX", "GX2", "VX", "VXR"]]
    )


class ScrapeResponse(BaseModel):
    make: str
    model: str
    year: int
    code: str


@router.post("/scrape", response_model=ScrapeResponse)
def scrape_toyota(payload: ToyotaScrapeRequest) -> ScrapeResponse:
    try:
        code = claude.generate_toyota_scraper(payload.make, payload.model, payload.year)
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except anthropic.APIConnectionError as e:
        raise HTTPException(status_code=502, detail="Failed to reach Claude API") from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ScrapeResponse(
        make=payload.make, model=payload.model, year=payload.year, code=code
    )


@router.post("/scrape/haraj", response_model=ScrapeResponse)
def scrape_haraj(payload: HarajScrapeRequest) -> ScrapeResponse:
    try:
        code = claude.generate_haraj_scraper(
            payload.make, payload.model, payload.year, payload.trims
        )
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except anthropic.APIConnectionError as e:
        raise HTTPException(status_code=502, detail="Failed to reach Claude API") from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ScrapeResponse(
        make=payload.make, model=payload.model, year=payload.year, code=code
    )
