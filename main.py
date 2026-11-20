import asyncio
import sys
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Windows compatibility for Playwright subprocesses.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsProactorEventLoopPolicy()
    )

from automation import search_web
from handoff import router as handoff_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prodieyy-ai")

app = FastAPI(
    title="Prodieyy AI",
    description="Your AI browser search assistant",
    version="1.0.0",
)

app.include_router(handoff_router)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

if not STATIC_DIR.is_dir():
    raise RuntimeError(
        f"Static folder not found: {STATIC_DIR}"
    )

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


class ChatRequest(BaseModel):
    message: str = Field(
        default="",
        max_length=2000,
    )


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "app": "Prodieyy AI",
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    message = req.message.strip()

    if not message:
        return {
            "reply": "Please enter a question or task.",
            "results": [],
        }

    try:
        return await search_web(message)

    except Exception as exc:
        logger.exception("Prodieyy AI request failed")

        return {
            "reply": (
                "Sorry, the task failed unexpectedly. "
                "Please try again. Check the VS Code "
                "terminal if the problem continues. "
                f"[{type(exc).__name__}: {exc}]"
            ),
            "results": [],
        }