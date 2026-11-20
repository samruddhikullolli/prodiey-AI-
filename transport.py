"""Prodieyy AI transport suggester: which way of travelling fits your budget?

Example: "how to go from Bengaluru to Chennai under 1000 for 2 people"

Distance comes from OpenStreetMap geocoding (free, no key) with a built-in city table
as a fallback. Fares are ESTIMATES from per-km ranges, not live prices, and every reply
says so. Use the links to check real prices. Nothing is booked.
"""
import asyncio
import json
import logging
import math
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from trains import parse_date, parse_route

logger = logging.getLogger("prodieyy.transport")

INTENT_RE = re.compile(r"\b(?:go|going|travel|reach|get to|transport|way to|how to|suggest|recommend|options?|cheapest|cheap|budget)\b", re.I)
MODE_RE = re.compile(r"\b(?:trains?|irctc|bus|buses|redbus|flights?|airlines?|hotels?|movies?)\b", re.I)
BUDGET_RE = re.compile(r"\b(?:under|below|within|upto|up to|max(?:imum)?|budget(?: of)?|less than)\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k)?\b", re.I)
PEOPLE_RE = re.compile(r"\b(\d{1,2})\s*(?:people|persons?|passengers?|adults?|members|of us)\b", re.I)

# lat, lon for common cities (fallback when geocoding is unavailable)
CITIES = {
    "bengaluru": (12.9716, 77.5946), "bangalore": (12.9716, 77.5946), "chennai": (13.0827, 80.2707),
    "mysuru": (12.2958, 76.6394), "mysore": (12.2958, 76.6394), "hyderabad": (17.385, 78.4867),
    "mumbai": (19.076, 72.8777), "pune": (18.5204, 73.8567), "delhi": (28.6139, 77.209),
    "kolkata": (22.5726, 88.3639), "goa": (15.4909, 73.8278), "kochi": (9.9312, 76.2673),
    "coimbatore": (11.0168, 76.9558), "mangaluru": (12.9141, 74.856), "mangalore": (12.9141, 74.856),
    "tirupati": (13.6288, 79.4192), "pondicherry": (11.9416, 79.8083), "puducherry": (11.9416, 79.8083),
    "madurai": (9.9252, 78.1198), "ahmedabad": (23.0225, 72.5714), "jaipur": (26.9124, 75.7873),
    "visakhapatnam": (17.6868, 83.2185), "thiruvananthapuram": (8.5241, 76.9366),
}
ROAD_FACTOR = 1.25     # road/rail distance is longer than the straight line

# Per-person ranges in rupees per km, used for ESTIMATES only.
FARE_PER_KM = {
    "Train Sleeper":      (0.55, 0.95),
    "Train 3AC":          (1.60, 2.50),
    "Bus (non-AC)":       (1.00, 1.70),
    "Bus (AC seater/sleeper)": (1.60, 3.00),
}
CAB_PER_KM = (12, 16)   # whole cab, one way, shared by up to 4 people
SPEED = {"train": 55, "bus": 45, "cab": 50}


def is_transport_query(q):
    return bool(INTENT_RE.search(q or "")) and not MODE_RE.search(q or "") and parse_route(q) is not None


def parse_budget(q):
    m = BUDGET_RE.search(q or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * 1000 if m.group(2) else v


def _geocode_sync(name):
    url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=in&q=" + urllib.parse.quote(name)
    req = urllib.request.Request(url, headers={"User-Agent": "ProdieyyAI/1.0 (personal project)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (float(data[0]["lat"]), float(data[0]["lon"])) if data else None


async def locate(name):
    key = name.lower().strip()
    if key in CITIES:
        return CITIES[key]
    try:
        return await asyncio.to_thread(_geocode_sync, name)
    except Exception as exc:
        logger.info("geocode failed for %s: %s", name, exc)
        return None


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def build_options(km, people):
    """List of dicts with low/high per-person cost estimates and hours."""
    opts = []
    for name, (lo, hi) in FARE_PER_KM.items():
        mode = "train" if name.startswith("Train") else "bus"
        opts.append({"mode": name, "low": km * lo, "high": km * hi, "hours": km / SPEED[mode]})
    per_cab = lambda rate: km * rate / min(max(people, 1), 4)
    opts.append({"mode": f"Cab (shared by {min(max(people, 1), 4)})", "low": per_cab(CAB_PER_KM[0]),
                 "high": per_cab(CAB_PER_KM[1]), "hours": km / SPEED["cab"]})
    if km >= 300:   # flights only make sense on longer routes
        opts.append({"mode": "Flight (economy)", "low": 2200 + km * 1.8, "high": 3500 + km * 4.0,
                     "hours": km / 650 + 3.0})   # includes airport time
    for o in opts:
        o["low"], o["high"] = round(o["low"], -1), round(o["high"], -1)
    return opts


def fmt_hours(h):
    m = int(round(h * 60))
    return f"{m // 60}h {m % 60:02d}m"


async def suggest_transport(query):
    route = parse_route(query)
    if not route:
        return {"reply": "Tell me where you're going from and to, e.g. \"how to go from Bengaluru to Chennai under 1000\".", "results": []}
    src, dst = route[0].title(), route[1].title()
    budget = parse_budget(query)
    pm = PEOPLE_RE.search(query or "")
    people = int(pm.group(1)) if pm else 1
    when, assumed = parse_date(query)

    a, b = await asyncio.gather(locate(src), locate(dst))
    if not a or not b:
        return {"reply": f"I couldn't find the distance between {src} and {dst}. Check the spelling, or open the links to compare live prices.",
                "results": _links(src, dst, when)}
    km = round(haversine_km(a, b) * ROAD_FACTOR)

    opts = build_options(km, people)
    for o in opts:
        o["fit"] = ("unknown" if budget is None else
                    "within" if o["high"] <= budget else
                    "maybe" if o["low"] <= budget else "over")
    order = {"within": 0, "maybe": 1, "unknown": 2, "over": 3}
    opts.sort(key=lambda o: (order[o["fit"]], o["low"]))

    label = {"within": "Fits your budget", "maybe": "Might fit, check the real price",
             "over": "Likely over budget", "unknown": ""}
    lines = [f"{src} to {dst}: about {km} km by road/rail, travelling around {when:%a %d %b %Y}"
             + (" (date assumed)" if assumed else "") + ".",
             f"Estimated cost per person" + (f", budget Rs {budget:,.0f}" if budget else "") +
             (f", {people} travellers" if people > 1 else "") + ":"]
    fitting = [o for o in opts if o["fit"] in ("within", "maybe")]
    for o in opts:
        lines.append(f"- {o['mode']}: Rs {o['low']:,.0f} to {o['high']:,.0f}, about {fmt_hours(o['hours'])}. {label[o['fit']]}".rstrip(". ") + ".")
    if budget and not fitting:
        cheapest = min(opts, key=lambda o: o["low"])
        lines.append(f"Nothing looks affordable at Rs {budget:,.0f}. The cheapest estimate is {cheapest['mode']} at about Rs {cheapest['low']:,.0f}.")
    elif fitting:
        best = fitting[0]
        lines.append(f"Suggestion: {best['mode']} (about Rs {best['low']:,.0f} to {best['high']:,.0f} per person, {fmt_hours(best['hours'])}).")
    lines.append("These are estimates from typical per-km rates, not live fares. Tatkal, dynamic pricing, festivals and "
                 "last-minute flights can cost much more. Check the links for real prices and availability. I don't book anything.")
    return {"reply": "\n".join(lines), "results": _links(src, dst, when)}


def _links(src, dst, when):
    q = urllib.parse.quote_plus
    return [
        {"title": "Trains (IRCTC)", "url": "https://www.irctc.co.in/nget/train-search", "snippet": f"Search {src} to {dst} for {when:%d %b %Y}."},
        {"title": "Trains (ixigo)", "url": "https://www.ixigo.com/trains", "snippet": "Fares and seat availability."},
        {"title": "Buses (redBus)", "url": "https://www.redbus.in/", "snippet": f"Search {src} to {dst} for {when:%d %b %Y}."},
        {"title": "Flights (Google Flights)", "url": "https://www.google.com/travel/flights?q=" + q(f"Flights from {src} to {dst} on {when:%Y-%m-%d}") + "&curr=INR",
         "snippet": "Compare fares across airlines."},
        {"title": f"Driving route {src} to {dst}", "url": f"https://www.google.com/maps/dir/?api=1&origin={q(src)}&destination={q(dst)}",
         "snippet": "Road distance and travel time."},
    ]