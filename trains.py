"""Prodieyy AI train helper (imported by booking.py).

Exports: CANCEL_RE, is_train_query, parse_date, parse_route, search_trains, wants_booking.

IRCTC needs login, OTP and a captcha, so this module never books. It understands
the route and date, then gives ready-made links (IRCTC, ixigo, ConfirmTkt, Google)
plus live web results when SERPER_API_KEY works.
"""
import logging
import re
from datetime import date, timedelta
from urllib.parse import quote_plus

logger = logging.getLogger("prodieyy.trains")

TRAIN_RE = re.compile(
    r"\b(?:trains?|irctc|pnr|rajdhani|shatabdi|vande\s*bharat|duronto|tatkal|sleeper class|3ac|2ac|1ac)\b",
    re.I,
)
CANCEL_RE = re.compile(r"\b(?:cancel(?:l?ing|lation)?|refund)\b", re.I)
BOOK_RE = re.compile(r"\b(?:book|booking|open|take me|go to|buy)\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
MON_PAT = "|".join(MONTHS)

# The destination/origin ends at a date, budget or filler word.
_STOP = (r"(?=\s+(?:on|for|under|below|within|upto|up to|tomorrow|today|tonight|next|this|by|with|"
         r"in|at|after|before|please|via|\d|" + MON_PAT + r")\b|\s*[,.?!]|\s*$)")
ROUTE_RE = re.compile(r"\bfrom\s+([a-z][a-z .'\-]*?)\s+to\s+([a-z][a-z .'\-]*?)" + _STOP, re.I)
ROUTE_BARE_RE = re.compile(r"\b([a-z][a-z.'\-]*(?:\s[a-z][a-z.'\-]*)?)\s+to\s+([a-z][a-z .'\-]*?)" + _STOP, re.I)
_NOISE = {"book", "booking", "find", "show", "me", "a", "the", "get", "need", "want", "cheap", "cheapest",
          "best", "train", "trains", "bus", "buses", "flight", "flights", "ticket", "tickets", "for",
          "please", "i", "to", "any", "check", "search", "volvo", "ac", "sleeper", "seater"}


def is_train_query(q):
    return bool(TRAIN_RE.search(q or ""))


def wants_booking(q):
    return bool(BOOK_RE.search(q or ""))


def _strip_noise(name):
    words = [w for w in name.split() if w.lower() not in _NOISE]
    return " ".join(words).strip()


def parse_route(query):
    """Return (source, destination) in lower case, or None."""
    q = re.sub(r"\s+", " ", query or "").strip()
    for rx in (ROUTE_RE, ROUTE_BARE_RE):
        m = rx.search(q)
        if m:
            src, dst = _strip_noise(m.group(1)), _strip_noise(m.group(2))
            if src and dst and src.lower() != dst.lower():
                return src.lower(), dst.lower()
    return None


def parse_date(query):
    """Return (date, assumed). assumed is True when no date was given (defaults to tomorrow)."""
    q = (query or "").lower()
    today = date.today()

    if re.search(r"\bday after tomorrow\b", q):
        return today + timedelta(days=2), False
    if re.search(r"\btomorrow\b", q):
        return today + timedelta(days=1), False
    if re.search(r"\b(?:today|tonight)\b", q):
        return today, False

    def make(day, month):
        try:
            d = date(today.year, month, day)
        except ValueError:
            return None
        return d if d >= today else d.replace(year=today.year + 1)

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(" + MON_PAT + r")[a-z]*\b", q)
    if m and (d := make(int(m.group(1)), MONTHS[m.group(2)])):
        return d, False
    m = re.search(r"\b(" + MON_PAT + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", q)
    if m and (d := make(int(m.group(2)), MONTHS[m.group(1)])):
        return d, False
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", q)
    if m and (d := make(int(m.group(1)), int(m.group(2)))):
        return d, False
    m = re.search(r"\b(?:next |this )?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", q)
    if m:
        ahead = (WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), False

    return today + timedelta(days=1), True


async def _web_hits(src, dst):
    try:
        import api_search
        if not api_search.enabled():
            return []
        data = await api_search._post("search", {"q": f"trains from {src} to {dst} timetable", "num": 5})
        return [r for r in (data or {}).get("organic", [])[:4] if r.get("link")]
    except Exception as exc:
        logger.warning("train web search failed: %s", exc)
        return []


async def search_trains(query):
    q = query or ""

    if CANCEL_RE.search(q):
        return {
            "reply": ("Cancelling needs your IRCTC login and OTP, so I can't do it for you. Use the official "
                      "cancellation page or the PNR status page. Refund rules depend on how close to departure you cancel."),
            "results": [
                {"title": "IRCTC: cancel ticket", "url": "https://www.irctc.co.in/nget/booking/cancel-ticket",
                 "snippet": "Log in, open Booked Ticket History, choose the ticket and cancel."},
                {"title": "PNR status", "url": "https://www.indianrail.gov.in/enquiry/PNR/PnrEnquiry.html",
                 "snippet": "Check the live status of a booking."},
            ],
        }

    route = parse_route(q)
    if not route:
        return {"reply": "Tell me the route and date, e.g. \"train from Bengaluru to Chennai tomorrow\".",
                "results": [{"title": "IRCTC", "url": "https://www.irctc.co.in/", "snippet": "Official booking site."}]}

    src, dst = route
    when, assumed = parse_date(q)
    S, D = src.title(), dst.title()
    note = " (no date given, so I assumed tomorrow)" if assumed else ""
    g = "https://www.google.com/search?q=" + quote_plus(f"trains from {S} to {D} on {when:%d %b %Y}")

    results = [
        {"title": f"IRCTC: book {S} to {D}", "url": "https://www.irctc.co.in/nget/train-search",
         "snippet": f"Official site. Enter {S} to {D} for {when:%d %b %Y}, then log in to book."},
        {"title": "ixigo trains", "url": "https://www.ixigo.com/trains",
         "snippet": "Compare trains, seat availability and prices."},
        {"title": "ConfirmTkt", "url": "https://www.confirmtkt.com/",
         "snippet": "Availability, waitlist confirmation chances and alternatives."},
        {"title": f"Google: trains {S} to {D}", "url": g, "snippet": "Timings and train names."},
    ]
    for r in await _web_hits(S, D):
        results.append({"title": r.get("title", ""), "url": r["link"], "snippet": (r.get("snippet") or "")[:240]})

    if wants_booking(q):
        try:
            import webbrowser
            webbrowser.open("https://www.irctc.co.in/nget/train-search")
        except Exception:
            pass

    return {
        "reply": (f"Trains for {S} to {D} on {when:%a %d %b %Y}{note}. IRCTC needs your login, OTP and captcha, "
                  "so I can't read live availability or book for you. The links open the right sites. "
                  "Check availability on ixigo or ConfirmTkt, then book on IRCTC. Tatkal opens a day earlier "
                  "(10 AM for AC, 11 AM for sleeper)."),
        "results": results,
    }