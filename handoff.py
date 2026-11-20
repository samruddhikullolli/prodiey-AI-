"""Prodieyy AI hand-off: after YOU pick an option, open the official booking page.

It never logs in, fills passenger details, pays or confirms anything. Nothing opens
unless the request has confirm=true, and only official booking domains are allowed.
"""
import logging
import webbrowser
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("prodieyy.handoff")
router = APIRouter()

OFFICIAL = {
    "train": ("irctc.co.in", "ixigo.com", "confirmtkt.com"),
    "bus": ("redbus.in", "abhibus.com", "ksrtc.in", "kstdc.co"),
    "flight": ("google.com", "goindigo.in", "airindia.com", "akasaair.com", "spicejet.com", "makemytrip.com"),
    "hotel": ("booking.com", "google.com", "oyorooms.com", "makemytrip.com"),
    "movie": ("bookmyshow.com", "pvrcinemas.com", "inoxmovies.com", "google.com"),
}
NEXT_STEPS = {
    "train": "Log in to IRCTC, enter passenger names and ages, choose the class, solve the captcha and pay.",
    "bus": "Pick seats and boarding/dropping points, enter passenger details, then pay on the site.",
    "flight": "Choose the fare, enter passenger names exactly as on ID, then pay on the airline or agent site.",
    "hotel": "Check the cancellation policy and total price with taxes, then enter guest details and pay.",
    "movie": "Choose the show and seats, then pay on the cinema site.",
}


class SelectRequest(BaseModel):
    category: str = Field(pattern="^(train|bus|flight|hotel|movie)$")
    url: str = Field(max_length=2000)
    choice: str = Field(default="", max_length=300)   # e.g. "12658 Chennai Mail, 3A"
    passengers: int = Field(default=1, ge=1, le=9)
    confirm: bool = False


def _allowed(category, url):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in OFFICIAL[category])


@router.post("/api/select")
async def select(req: SelectRequest):
    if not _allowed(req.category, req.url):
        return {"ok": False, "reply": "That link is not an official booking site for this category, so I won't open it."}
    summary = f"{req.choice or req.category} for {req.passengers} passenger(s)"
    if not req.confirm:
        return {"ok": False, "needs_confirmation": True,
                "reply": f"You picked: {summary}. I will only open the official site, and you complete login and payment. "
                         "Send the same request with confirm=true to open it."}
    try:
        webbrowser.open(req.url)
    except Exception as exc:
        logger.warning("could not open browser: %s", exc)
    return {"ok": True, "reply": f"Opened the official site for {summary}. Nothing has been booked. "
                                 + NEXT_STEPS[req.category], "url": req.url}