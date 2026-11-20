"""Prodieyy AI place recommendations: cafes, restaurants, resorts, malls, salons...

Reads Google Maps search results with Playwright, keeps well-reviewed places,
ranks them by rating weighted by number of reviews (so a 4.8 from 12 people does
not beat a 4.6 from 3,000), and gives a Google Maps link + embedded map for each.
"""
import logging
import os
import re
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

from api_search import api_place_search
from automation import (
    clean_space, dump_debug, extract_budget, launch, looks_blocked, with_retries,
)

logger = logging.getLogger("prodieyy.places")

DEFAULT_CITY = os.getenv("PRODIEYY_DEFAULT_CITY", "").strip()  # e.g. "Bengaluru"

PLACE_RE = re.compile(
    r"\b(?:cafes?|caf[eé]s?|coffee shops?|restaurants?|restros?|dhabas?|diners?|bakery|bakeries|"
    r"bars?|pubs?|breweries|brewery|biryani|dosa|pizza|"
    r"resorts?|spas?|salons?|saloons?|salouns?|barbers?|parlou?rs?|"
    r"malls?|shopping (?:centres?|centers?|complex)|"
    r"gyms?|parks?|temples?|museums?|beach(?:es)?|"
    r"tourist (?:places?|spots?|attractions?)|places? to (?:visit|eat|hang ?out)|"
    r"things to do|food courts?|ice cream|dessert)\b",
    re.I,
)
# Words that mean "I want to stay/book", so "resort" should go to the hotel flow.
STAY_RE = re.compile(r"\b(?:hotels?|hostels?|oyo|airbnb|lodges?|nights?|check[- ]?in|book(?:ing)?)\b", re.I)

FILLER = {
    "best", "top", "good", "great", "nice", "recommend", "recommended", "suggest", "find", "show",
    "me", "some", "please", "a", "an", "the", "i", "want", "need", "to", "go", "with", "service",
    "excellent", "popular", "famous", "highly", "rated", "reviews", "review", "give", "list", "of",
    "any", "can", "you", "tell", "for", "my", "we", "us", "looking", "around", "near", "nearby",
}
LOC_RE = re.compile(
    r"\b(?:in|near|around|at|close to)\s+([a-z0-9][a-z0-9 .'\-]*?)"
    r"(?=\s+(?:under|below|with|for|that|which|and|within|having|where|upto|up to)\b|[,?.!]|$)",
    re.I,
)
RATING_RE = re.compile(r"(\d\.\d)\s*\(\s*([\d,.]+[kK]?)\s*\)")
PRICE_RANGE_RE = re.compile(r"₹\s*([\d,]+)\s*[–\-]\s*([\d,]+)")
PRICE_LEVEL_RE = re.compile(r"(?<![\w₹])(₹{1,4})(?![\w₹\d])")


def maps_search_url(q):
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def maps_embed_url(q):
    return "https://maps.google.com/maps?q=" + quote_plus(q) + "&output=embed"


# --------------------------------------------------------------------------
# Understanding the request
# --------------------------------------------------------------------------
def parse_place_request(query):
    """Return (what, area). `area` is '' when the user gave no usable location."""
    q = clean_space(query).rstrip("?.! ")
    area = ""
    for m in LOC_RE.finditer(q):
        cand = m.group(1).strip()
        if cand.lower() not in {"me", "my location", "here", "my area", "us"}:
            area = cand
    what = LOC_RE.sub(" ", q) if area else q
    what = re.sub(r"\b(?:under|below|upto|up to|within|max)\s*₹?\s*[\d,]+k?\b(?:\s*(?:per person|pp|each))?", " ", what, flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z0-9é&'\-]+", what) if w.lower() not in FILLER]
    what = " ".join(words) or "places"
    return what, area


def _count(s):
    s = s.replace(",", "").strip().lower()
    try:
        return int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))
    except ValueError:
        return 0


def parse_card(name, href, text):
    """Turn a Google Maps result card's text into a dict."""
    lines = [clean_space(l) for l in (text or "").splitlines() if clean_space(l)]
    joined = " | ".join(lines)
    rating = reviews = None
    m = RATING_RE.search(text or "") or RATING_RE.search(joined)
    if m:
        rating, reviews = float(m.group(1)), _count(m.group(2))
    low = high = None
    pm = PRICE_RANGE_RE.search(joined)
    if pm:
        low, high = int(pm.group(1).replace(",", "")), int(pm.group(2).replace(",", ""))
    level = None
    lm = PRICE_LEVEL_RE.search(joined)
    if lm:
        level = len(lm.group(1))
    category = address = ""
    for l in lines:
        if " · " in l and l != name and not RATING_RE.search(l):
            parts = [p.strip(" ·") for p in l.split("·") if p.strip(" ·")]
            parts = [p for p in parts if not PRICE_RANGE_RE.search(p) and not re.fullmatch(r"₹{1,4}", p)]
            if parts:
                category = parts[0]
                address = parts[-1] if len(parts) > 1 else ""
                break
    status = next((l for l in lines if re.match(r"(?:Open|Closed|Opens|Closes|Temporarily|Permanently)", l)), "")
    flags = [f for f in ("Dine-in", "Takeaway", "Delivery", "Outdoor seating", "Reservations")
             if re.search(re.escape(f), joined, re.I)]
    return {
        "name": name, "url": href, "rating": rating, "reviews": reviews or 0,
        "price_low": low, "price_high": high, "price_level": level,
        "category": category, "address": address, "status": status, "flags": flags,
    }


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
PRIOR_MEAN, PRIOR_WEIGHT = 4.2, 100


def score(p):
    """Bayesian average: pulls places with few reviews toward the typical rating."""
    r, v = p.get("rating"), p.get("reviews", 0)
    if r is None:
        return 0.0
    return (v / (v + PRIOR_WEIGHT)) * r + (PRIOR_WEIGHT / (v + PRIOR_WEIGHT)) * PRIOR_MEAN


def rank_places(items, budget=None, limit=8):
    seen, uniq = set(), []
    for p in items:
        key = p["name"].lower()
        if p["name"] and key not in seen:
            seen.add(key)
            uniq.append(p)
    rated = [p for p in uniq if p["rating"] is not None]

    pool = [p for p in rated if p["rating"] >= 4.0 and p["reviews"] >= 50]
    if len(pool) < 3:                       # not enough strong places: relax
        pool = [p for p in rated if p["rating"] >= 3.8 and p["reviews"] >= 20] or rated

    over = 0
    if budget:
        kept = [p for p in pool if p["price_low"] is None or p["price_low"] <= budget]
        over = len(pool) - len(kept)
        pool = kept
    pool.sort(key=score, reverse=True)
    return pool[:limit], over


def describe(p, area):
    bits = []
    if p["category"]:
        bits.append(p["category"])
    if p["address"]:
        bits.append(p["address"])
    if p["price_low"] is not None:
        bits.append(f"₹{p['price_low']}–{p['price_high']} per person")
    elif p["price_level"]:
        bits.append("₹" * p["price_level"])
    if p["status"]:
        bits.append(p["status"])
    if p["flags"]:
        bits.append(", ".join(p["flags"]))
    return " · ".join(bits) or "Open in Google Maps for photos, menu and reviews."


def result_item(p, area, rank_no):
    stars = f"{p['rating']:.1f}/5 ({p['reviews']:,})" if p["rating"] else "No rating"
    q = f"{p['name']} {area}".strip()
    return {
        "title": f"{rank_no}. {p['name']}  {stars}",
        "url": p["url"] or maps_search_url(q),
        "snippet": describe(p, area),
        "map_url": p["url"] or maps_search_url(q),
        "embed": maps_embed_url(q),
    }


# --------------------------------------------------------------------------
# Reading Google Maps
# --------------------------------------------------------------------------
_CARDS_JS = """() => [...document.querySelectorAll('div[role="feed"] a.hfpxzc, div[role="feed"] a[href*="/maps/place/"]')]
  .slice(0, 40).map(a => {
    const card = a.closest('div.Nv2PK') || a.closest('div[jsaction]') || a.parentElement;
    return {name: a.getAttribute('aria-label') || '', href: a.href, text: card ? card.innerText.slice(0, 700) : ''};
  })"""


async def fetch_maps_cards(search_text):
    url = "https://www.google.com/maps/search/" + quote_plus(search_text) + "?hl=en&gl=in"
    async with async_playwright() as p:
        browser, context = await launch(p)
        try:
            page = await context.new_page()

            async def attempt(i):
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                for label in ("Accept all", "I agree", "Reject all"):   # consent wall, if any
                    try:
                        await page.get_by_role("button", name=label).click(timeout=1500)
                        break
                    except Exception:
                        pass
                await page.wait_for_selector('div[role="feed"]', timeout=15000 + 5000 * i)
                feed = page.locator('div[role="feed"]')
                for _ in range(4):                       # load more results
                    await feed.evaluate("e => e.scrollBy(0, 2500)")
                    await page.wait_for_timeout(700)
                rows = await page.evaluate(_CARDS_JS)
                if not rows:
                    raise RuntimeError("no result cards")
                return rows

            try:
                return await with_retries(attempt, attempts=2, label="maps"), False
            except Exception:
                blocked = await looks_blocked(page)
                await dump_debug(page, "maps")
                return [], blocked
        finally:
            await browser.close()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def search_places(query):
    what, area = parse_place_request(query)
    budget = extract_budget(query)

    if not area and not DEFAULT_CITY:
        near_me = maps_search_url(f"{what} near me")
        return {
            "reply": (
                f"Which area or city should I look in? For example: \"best {what} in Indiranagar, Bengaluru\". "
                "Until then, the link below opens the same search around your current location."
            ),
            "results": [{"title": f"{what.title()} near me", "url": near_me,
                         "snippet": "Opens Google Maps using your location.", "map_url": near_me}],
        }

    area_full = area or DEFAULT_CITY
    if DEFAULT_CITY and area and DEFAULT_CITY.lower() not in area.lower() and "," not in area:
        area_full = f"{area}, {DEFAULT_CITY}"
    search_text = f"{what} in {area_full}"
    all_url = maps_search_url(search_text)
    see_all = {"title": f"See all '{what}' in {area_full} on Google Maps", "url": all_url,
               "snippet": "Full list, photos, menus, opening hours and reviews.",
               "map_url": all_url, "embed": maps_embed_url(search_text)}

    fast = await api_place_search(what, area_full, budget)
    if fast:
        return fast

    rows, blocked = await fetch_maps_cards(search_text)
    items = [parse_card(r["name"], r["href"], r["text"]) for r in rows if r.get("name")]
    top, over = rank_places(items, budget)

    if not top:
        why = "Google Maps showed a bot check." if blocked else "I couldn't read the Google Maps results (the layout may have changed)."
        return {"reply": f"{why} Open the link to see live results for {what} in {area_full}.", "results": [see_all]}

    best = top[0]
    most = max(top, key=lambda p: p["reviews"])
    cheapest = min((p for p in top if p["price_low"] is not None), key=lambda p: p["price_low"], default=None)
    parts = [
        f"Top {len(top)} {what} in {area_full}, ranked by Google rating weighted by number of reviews.",
        f"Best overall: {best['name']} ({best['rating']:.1f}/5, {best['reviews']:,} reviews).",
    ]
    if most is not best:
        parts.append(f"Most reviewed: {most['name']} ({most['reviews']:,}).")
    if cheapest and cheapest is not best:
        parts.append(f"Lowest price: {cheapest['name']} (from ₹{cheapest['price_low']}).")
    if budget:
        parts.append(f"Filtered to about ₹{budget} per person" + (f" ({over} pricier place(s) hidden)." if over else "."))
    parts.append("Service quality isn't a separate score on Maps, so open a place's Reviews and search 'service' before you go.")
    return {"reply": " ".join(parts), "results": [result_item(p, area_full, i) for i, p in enumerate(top, 1)] + [see_all]}