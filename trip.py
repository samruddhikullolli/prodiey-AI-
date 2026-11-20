"""Prodieyy AI trip planner: one-day trips, getaways and itineraries.

For Bengaluru it compares a built-in list of popular one-day destinations on
travel time, transport, entry fees, scenery, safety and (when SERPER_API_KEY
works) live Google Maps ratings, then builds an hour-by-hour itinerary with
Google Maps links. For other cities it falls back to top-rated tourist places.

Distances, fees and costs are approximate. The reply says so, and every result
links to Google Maps so the user can confirm live details.
"""
import asyncio
import logging
import os
import re
from urllib.parse import quote_plus

import api_search
from automation import clean_space, extract_budget

logger = logging.getLogger("prodieyy.trip")

# Trip-planning words. Booking words are excluded so "bus tickets for a trip"
# still goes to the booking flow.
TRIP_RE = re.compile(
    r"\b(?:trips?|itinerar(?:y|ies)|getaways?|day[- ]?trips?|one[- ]day|1[- ]day|"
    r"weekend (?:plan|trip|getaway)|plan (?:a|my|an) (?:day|tour|outing|picnic))\b",
    re.I,
)
BOOKING_WORDS_RE = re.compile(
    r"\b(?:tickets?|pnr|irctc|redbus|trains?|buses|bus|flights?|hotels?|hostels?|cancel)\b", re.I
)
ORIGIN_RE = re.compile(
    r"\b(?:near|from|around|in|close to)\s+([A-Za-z][A-Za-z .'\-]*?)"
    r"(?=\s+(?:under|below|within|for|with|that|which|and|upto|up to|compare|suggest)\b|[,?.!]|$)",
    re.I,
)

BENGALURU = {"bengaluru", "bangalore", "blr", "bengalooru"}
DEFAULT_CITY = os.getenv("PRODIEYY_DEFAULT_CITY", "").strip()

# Cost model (all assumptions are repeated to the user in the reply).
CAB_RATE_PER_KM = 12      # rupees per km, round trip, whole cab
CAB_SHARING = 4           # people sharing the cab
BUS_RATE_PER_KM = 1.8     # rupees per km, state bus, per person
FOOD = 350                # rupees per person for the day
START_HOUR = 6            # leave home at 6 AM

# Approximate, from general knowledge. "bus" is None when there is no practical
# public bus, otherwise the extra last-mile cost per person in rupees.
DESTINATIONS = [
    {
        "name": "Nandi Hills", "q": "Nandi Hills Chikkaballapur", "km": 60, "hours": (1.5, 2.0),
        "transport": "Own vehicle or cab. By bus, go to Chikkaballapur, then a local cab up the hill.",
        "bus": 150, "entry": "Small gate fee, roughly Rs 20 to 50 per person plus parking (verify)", "entry_est": 50,
        "scenic": "Sunrise above the clouds, old fort walls, rolling hill views",
        "safety": "Fog and a narrow winding road at dawn. Weekend traffic is heavy, so check current vehicle entry rules. Keep food covered because of monkeys.",
        "stops": ["Nandi Hills viewpoint", "Tipu's Drop Nandi Hills", "Bhoga Nandeeshwara Temple"],
    },
    {
        "name": "Savandurga", "q": "Savandurga Hill Magadi", "km": 60, "hours": (1.5, 2.0),
        "transport": "Own vehicle or cab. Buses reach Magadi, but the last stretch needs a cab.",
        "bus": 200, "entry": "Small forest fee, roughly Rs 50 (verify)", "entry_est": 50,
        "scenic": "One of Asia's largest monolith hills, forest trails, Arkavathi river",
        "safety": "Steep granite slabs are slippery after rain. Start before 8 AM to avoid the midday heat and carry water.",
        "stops": ["Savandurga Hill", "Manchanabele Dam"],
    },
    {
        "name": "Bannerghatta Biological Park", "q": "Bannerghatta Biological Park", "km": 25, "hours": (1.0, 1.5),
        "transport": "City buses and cabs reach it directly, which makes it the easiest trip.",
        "bus": 0, "entry": "Zoo roughly Rs 100 to 300, safari extra (check current rates)", "entry_est": 350,
        "scenic": "Wildlife safari, zoo, butterfly park, forested hills",
        "safety": "Safari tickets sell out on weekends. Follow safari rules strictly and check the weekly closing day (usually Tuesday).",
        "stops": ["Bannerghatta Biological Park", "Bannerghatta Butterfly Park"],
    },
    {
        "name": "Ramanagara and Ramadevara Betta", "q": "Ramadevara Betta Ramanagara", "km": 50, "hours": (1.25, 1.5),
        "transport": "Trains and buses to Ramanagara are frequent. Take an auto or cab for the last stretch.",
        "bus": 100, "entry": "Free or a very small fee (verify)", "entry_est": 20,
        "scenic": "Rocky hills from the film Sholay, vulture sanctuary, sunset views",
        "safety": "Rocky climb to the hilltop, so wear proper shoes and avoid noon heat. Stay on marked paths.",
        "stops": ["Ramadevara Betta", "Janapada Loka Ramanagara"],
    },
    {
        "name": "Shivanasamudra Falls", "q": "Gaganachukki Falls Shivanasamudra", "km": 130, "hours": (3.0, 3.5),
        "transport": "Own vehicle or cab. By bus, go to Malavalli, then local transport.",
        "bus": 250, "entry": "Small viewpoint fee, roughly Rs 20 to 50 (verify)", "entry_est": 50,
        "scenic": "Twin waterfalls on the Cauvery, best in and after the monsoon",
        "safety": "Strong currents. Stay behind railings and do not enter the water, especially during monsoon.",
        "stops": ["Gaganachukki Falls", "Barachukki Falls"],
    },
    {
        "name": "Mysuru (Mysore Palace and Chamundi Hills)", "q": "Mysore Palace", "km": 145, "hours": (3.0, 3.5),
        "transport": "Frequent trains and state buses from Bengaluru. Cab or self drive is fastest.",
        "bus": 0, "entry": "Palace roughly Rs 100, zoo Rs 100 to 150 (verify)", "entry_est": 200,
        "scenic": "Royal palace, Chamundi Hills viewpoint, city markets",
        "safety": "A long day with about 6 to 7 hours of travel. Book trains or buses early and avoid driving back late and tired.",
        "stops": ["Mysore Palace", "Chamundi Hill"],
    },
    {
        "name": "Lepakshi", "q": "Lepakshi Veerabhadra Temple", "km": 120, "hours": (2.5, 3.0),
        "transport": "Own vehicle or cab. By public transport, go to Hindupur, then an auto.",
        "bus": 150, "entry": "Low entry or camera fee (verify)", "entry_est": 50,
        "scenic": "Vijayanagara temple with hanging pillar and a giant Nandi monolith",
        "safety": "Temple dress code applies. It is across the Andhra Pradesh border, so carry ID, and avoid noon heat.",
        "stops": ["Veerabhadra Temple Lepakshi", "Lepakshi Nandi"],
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def is_trip_query(query):
    q = query or ""
    return bool(TRIP_RE.search(q)) and not BOOKING_WORDS_RE.search(q)


def parse_origin(query):
    q = clean_space(query)
    origin = ""
    for m in ORIGIN_RE.finditer(q):
        cand = m.group(1).strip()
        if cand.lower() not in {"me", "my location", "here", "my area", "us", "one day", "a day"}:
            origin = cand
    return origin


def maps_dir_url(origin, destination, waypoints=None):
    url = ("https://www.google.com/maps/dir/?api=1&origin=" + quote_plus(origin)
           + "&destination=" + quote_plus(destination) + "&travelmode=driving")
    if waypoints:
        url += "&waypoints=" + "%7C".join(quote_plus(w) for w in waypoints)
    return url


def maps_search_url(q):
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def maps_embed_url(q):
    return "https://maps.google.com/maps?q=" + quote_plus(q) + "&output=embed"


def clock_text(minutes):
    h, m = divmod(int(round(minutes)), 60)
    h %= 24
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def hours_text(lo, hi):
    f = lambda x: f"{x:g}"
    return f"{f(lo)} to {f(hi)} hours" if lo != hi else f"{f(lo)} hours"


def estimate_cost(d):
    """Per-person estimate for a day trip: cheapest transport + entry + food."""
    cab = round(d["km"] * 2 * CAB_RATE_PER_KM / CAB_SHARING, -1)
    bus = None if d["bus"] is None else round(d["km"] * 2 * BUS_RATE_PER_KM + d["bus"], -1)
    transport = min(x for x in (cab, bus) if x is not None)
    return {"cab": int(cab), "bus": None if bus is None else int(bus),
            "total": int(transport + d["entry_est"] + FOOD)}


def bayes(rating, votes, mean=4.2, weight=100):
    return (votes / (votes + weight)) * rating + (weight / (votes + weight)) * mean


async def live_rating(d):
    """Google Maps rating for a destination via Serper, or None."""
    data = await api_search._post("places", {"q": d["q"]})
    for r in (data or {}).get("places") or []:
        if r.get("rating"):
            cid = r.get("cid")
            return {
                "rating": float(r["rating"]),
                "votes": int(r.get("ratingCount") or 0),
                "link": f"https://www.google.com/maps?cid={cid}" if cid else None,
            }
    return None


def build_itinerary(d, origin):
    """Hour-by-hour plan as a list of (clock_text, description)."""
    travel = sum(d["hours"]) / 2 * 60
    t = START_HOUR * 60
    rows = [(t, f"Leave {origin}")]
    t += travel
    for i, stop in enumerate(d["stops"]):
        rows.append((t, f"Arrive at {stop}" if i == 0 else f"Move on to {stop}"))
        t += 90
        if i == 0:
            rows.append((t, "Lunch and rest (about 1 hour)"))
            t += 60
    rows.append((t, f"Start back to {origin}"))
    t += travel
    rows.append((t, f"Reach {origin}"))
    late = t > 21 * 60 + 30
    return [(clock_text(m), text) for m, text in rows], late


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def plan_trip(query):
    origin = parse_origin(query) or DEFAULT_CITY or "Bengaluru"
    budget = extract_budget(query)

    if origin.lower() not in BENGALURU:
        return await _generic_trip(origin, budget)

    origin = "Bengaluru"
    ratings = await asyncio.gather(*(live_rating(d) for d in DESTINATIONS), return_exceptions=True)

    trips = []
    for d, rt in zip(DESTINATIONS, ratings):
        rt = None if isinstance(rt, Exception) else rt
        cost = estimate_cost(d)
        base = bayes(rt["rating"], rt["votes"]) if rt else 4.0
        trips.append({**d, "cost": cost, "live": rt, "score": base - d["km"] / 200})

    over = 0
    if budget:
        kept = [t for t in trips if t["cost"]["total"] <= budget]
        over = len(trips) - len(kept)
        if kept:
            trips = kept
        else:
            over = 0   # nothing fits: show the cheapest and say so below
            trips.sort(key=lambda t: t["cost"]["total"])
            trips = trips[:3]
    trips.sort(key=lambda t: t["score"], reverse=True)
    top = trips[:5]
    best = top[0]
    cheapest = min(top, key=lambda t: t["cost"]["total"])
    nearest = min(top, key=lambda t: t["km"])

    lines = []
    head = f"One-day trips from {origin}"
    head += f" within about Rs {budget:,} per person." if budget else "."
    lines.append(head)
    if budget and not any(t["cost"]["total"] <= budget for t in top):
        lines.append(f"Nothing fit Rs {budget:,} on my estimates, so these are the cheapest options.")
    lines.append(
        f"Best overall: {best['name']}. Closest: {nearest['name']} ({nearest['km']} km). "
        f"Cheapest: {cheapest['name']} (about Rs {cheapest['cost']['total']:,})."
    )
    lines.append("")
    lines.append("Comparison")
    for n, t in enumerate(top, 1):
        cost = t["cost"]
        how = f"round trip by shared cab Rs {cost['cab']:,}" + (f" or by bus Rs {cost['bus']:,}" if cost["bus"] is not None else "")
        review = (f"Google rating {t['live']['rating']:.1f}/5 from {t['live']['votes']:,} reviews"
                  if t["live"] else "Reviews: open the Maps link")
        lines.append(
            f"{n}. {t['name']}: {t['km']} km, about {hours_text(*t['hours'])} each way.\n"
            f"   Transport: {t['transport']}\n"
            f"   Entry: {t['entry']}\n"
            f"   Estimated cost per person: about Rs {cost['total']:,} ({how}, plus entry and food).\n"
            f"   Scenic: {t['scenic']}.\n"
            f"   Safety: {t['safety']}\n"
            f"   {review}."
        )

    itin, late = build_itinerary(best, origin)
    lines.append("")
    lines.append(f"Suggested itinerary: {best['name']}")
    for when, what in itin:
        lines.append(f"{when}  {what}")
    if late:
        lines.append("This gets you home late, so consider leaving earlier or picking a closer trip.")
    lines.append("")
    lines.append(
        f"Costs are estimates: a cab shared by {CAB_SHARING} people at about Rs {CAB_RATE_PER_KM} per km round trip, "
        f"state bus at about Rs {BUS_RATE_PER_KM} per km, and Rs {FOOD} for food. "
        "Distances, entry fees, opening days and weather rules change, so confirm on the Maps link before you go."
    )
    if not api_search.enabled() or not any(t["live"] for t in top):
        why = api_search.LAST_ERROR or "SERPER_API_KEY is not set"
        lines.append(f"Live Google ratings were unavailable ({why}).")

    results = []
    route = maps_dir_url(origin, origin, best["stops"])
    results.append({
        "title": f"Itinerary route: {origin} to {best['name']} and back",
        "url": route, "map_url": route,
        "snippet": "Stops: " + ", ".join(best["stops"]) + ". Opens turn-by-turn directions in Google Maps.",
    })
    for n, t in enumerate(top, 1):
        place_link = (t["live"] or {}).get("link") or maps_search_url(t["q"])
        results.append({
            "title": f"{n}. {t['name']}  {t['km']} km, about Rs {t['cost']['total']:,}",
            "url": maps_dir_url(origin, t["q"]),
            "snippet": f"{hours_text(*t['hours'])} each way. {t['scenic']}.",
            "map_url": place_link,
            "embed": maps_embed_url(t["q"]),
        })

    src = await api_search._post("search", {"q": f"best one day trips from {origin}", "num": 5})
    for r in ((src or {}).get("organic") or [])[:3]:
        if r.get("link"):
            results.append({"title": f"Read more: {r.get('title', '')}", "url": r["link"],
                            "snippet": (r.get("snippet") or "")[:240]})

    return {"reply": "\n".join(lines), "results": results}


async def _generic_trip(origin, budget):
    """Cities without built-in data: top-rated tourist places plus a map link."""
    from api_search import api_place_search
    fast = await api_place_search("tourist places", origin, budget)
    link = maps_search_url(f"tourist places near {origin}")
    note = (f"I only have a built-in one-day trip comparison for Bengaluru. For {origin}, here are the "
            "top-rated tourist places instead. Tell me the place you like and I can plan around it.")
    if fast:
        fast["reply"] = note + "\n\n" + fast["reply"]
        return fast
    return {"reply": note, "results": [{"title": f"Tourist places near {origin}", "url": link,
                                        "snippet": "Open in Google Maps.", "map_url": link}]}