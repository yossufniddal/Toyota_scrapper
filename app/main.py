from fastapi import FastAPI

from app.config import settings
from app.routers import health, prices, scrape

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(health.router)
app.include_router(scrape.router)
app.include_router(prices.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": f"Welcome to {settings.app_name}"}
