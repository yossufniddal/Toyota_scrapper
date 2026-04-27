from fastapi import FastAPI

from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import health, prices, scrape

app = FastAPI(title=settings.app_name, debug=settings.debug)

# Allow the Next.js dev server to call us during local development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(scrape.router)
app.include_router(prices.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": f"Welcome to {settings.app_name}"}
