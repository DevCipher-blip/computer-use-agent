import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import init_db
from routers.sessions import router as sessions_router
from routers.stream import router as stream_router
from services.session_manager import recover_active_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database…")
    await init_db()
    await recover_active_sessions()
    logger.info("Ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="ComputeAgent Computer Use Agent API",
    description=(
        "Scalable backend for Claude computer-use agent session management. "
        "Each session runs in an isolated Docker container with a full virtual desktop."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)
app.include_router(stream_router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


@app.exception_handler(Exception)
async def _global(request, exc):
    logger.error(f"Unhandled: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
