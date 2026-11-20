"""Prodieyy AI booking assistant: trains, buses, flights, hotels, movies + maps.

For every request it:
  * understands the route/place, date and budget from plain English,
  * searches a public listing page with Playwright,
  * keeps only options within the budget and ranks them (best pick, cheapest,
    fastest / top rated, biggest discount),
  * shows any discounts/offers visible on the page,
  * gives seat advice and Google Maps links,
  * opens the booking page in the user's own browser when asked to "book".

It never logs in, pays, or books for the user.
"""
import logging
import re
import webbrowser
from datetime import timedelta
from urllib.parse import quote_plus

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from automation import (
    clean_space, dump_debug, extract_budget, launch, looks_blocked, with_retries,
)
from trains import (
    CANCEL_RE, is_train_query, parse_date, parse_route, search_trains, wants_booking,
)

logger = logging.getLogger("prodieyy.booking")

MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

# --------------------------------------------------------------------------
# What does the user want?
# --------------------------------------------------------------------------
MAP_STRONG_RE = re.compile(
    r"\b(?:where is|where's|location of|map of|directions? (?:to|from)|navigate to|route to)\b", re.I
)
FLIGHT_RE = re.compile(
    r"\b(?:flights?|airfare|airlines?|indigo|air india|vistara|akasa|spicejet)\b", re.I
)
BUS_RE = re.compile(r"\b(?:bus|buses|redbus|abhibus|volvo|ksrtc|tsrtc|msrtc)\b", re.I)
HOTEL_STRONG_RE = re.compile(
    r"\b(?:hotels?|resorts?|hostels?|homestays?|lodges?|accommodation|airbnb|oyo)\b", re.I
)
HOTEL_WEAK_RE = re.compile(r"\b(?:rooms?|stay)\b", re.I)
HOTEL_CONTEXT_RE = re.compile(r"\b(?:book|booking|nights?|check[- ]?in)\b", re.I)
MOVIE_RE = re.compile(
    r"\b(?:movies?|bookmyshow|showtimes?|cinemas?|pvr|inox|multiplex)\b|(?<!home )\btheat(?:re|er)s?\b",
    re.I,
)


def detect_category(q):
    if is_train_query(q):
        return "train"
    if FLIGHT_RE.search(q):
        return "flight"
    if BUS_RE.search(q):
        return "bus"
    if HOTEL_STRONG_RE.search(q) or (HOTEL_WEAK_RE.search(q) and HOTEL_CONTEXT_RE.search(q)):
        return "hotel"
    if MOVIE_RE.search(q):
        return "movie"
    return None


# --------------------------------------------------------------------------
# Maps
# --------------------------------------------------------------------------
def maps_search_url(q):
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def maps_embed_url(q):
    return "https://maps.google.com/maps?q=" + quote_plus(q) + "&output=embed"


def maps_dir_url(origin, dest):
    url = "https://www.google.com/maps/dir/?api=1&destination=" + quote_plus(dest)
    if origin:
        url += "&origin=" + quote_plus(origin)
    return url


def map_item(label, query, snippet="Open in Google Maps"):
    return {
        "title": f"{label}",
        "url": maps_search_url(query),
        "snippet": snippet,
        "map_url": maps_search_url(query),
        "embed": maps_embed_url(query),
    }


def search_map(query):
    q = clean_space(query).rstrip("?.! ")
    m = re.search(r"\b(?:directions?|route)\s+(?:from\s+(.+?)\s+)?to\s+(.+)$", q, re.I)
    if m:
        origin, dest = (m.group(1) or "").strip(), m.group(2).strip()
        embed = (
            "https://maps.google.com/maps?saddr=" + quote_plus(origin)
            + "&daddr=" + quote_plus(dest) + "&output=embed"
            if origin else maps_embed_url(dest)
        )
        return {
            "reply": f"Directions to {dest}" + (f" from {origin}" if origin else " from your current location") + ".",
            "results": [{
                "title": f"Directions to {dest}",
                "url": maps_dir_url(origin, dest),
                "snippet": "Opens Google Maps with the route, travel time and traffic.",
                "map_url": maps_dir_url(origin, dest),
                "embed": embed,
            }],
        }
    m = re.search(r"\b(?:where is|where's|location of|map of|navigate to)\s+(.+)$", q, re.I)
    place = re.sub(r"\s+on (?:the )?map$", "", (m.group(1) if m else q), flags=re.I).strip()
    if not place:
        return {"reply": "Which place should I find on the map?", "results": []}
    return {
        "reply": f"Here's {place} on the map.",
        "results": [
            map_item(place, place),
            {"title": f"Get directions to {place}", "url": maps_dir_url("", place),
             "snippet": "Route and travel time from your current location."},
        ],
    }


# --------------------------------------------------------------------------
# Seat advice (live seat maps need a login, so this is guidance, not a pick)
# --------------------------------------------------------------------------
def seat_tips(kind, query):
    q = query.lower()
    if kind == "movie":
        tip = (
            "Best seats: about 60–70% of the way back from the screen, in the centre third "
            "of the row. Avoid the first 3 rows (neck strain) and the far corners."
        )
        if re.search(r"\b(?:family|kids?|child|senior|parents)\b", q):
            tip += " With kids or seniors, take an aisle seat for easy exits."
        if re.search(r"\b(?:couple|date)\b", q):
            tip += " For couples, look for corner or couple seats in the back rows."
        return tip + " Recliner/premium rows cost more; for value, pick rows a few steps behind the centre."
    if kind == "train":
        tip = "Lower berths suit seniors, kids and anyone with knee trouble. Side-lower has the window but is more cramped."
        if re.search(r"\b(?:senior|elderly|parents|child|kids?)\b", q):
            tip = "For seniors or kids choose a lower berth (or request it in the booking preferences)."
        return tip + " Seats close to the coach ends are noisier because of doors and toilets."
    if kind == "flight":
        tip = "Window for views and a wall to lean on, aisle for legroom and easy exits."
        if re.search(r"\b(?:legroom|tall)\b", q):
            tip = "For legroom choose an exit row or the front of the cabin (often a paid add-on)."
        return tip + " Avoid the last rows (near toilets, seats don't recline fully)."
    if kind == "bus":
        return (
            "Pick seats in the front-middle half of the bus: the ride is smoother than over the "
            "wheels or at the back. In sleepers, lower berths sway less. Check the boarding "
            "and dropping points before you pay."
        )
    if kind == "hotel":
        return (
            "Choose free-cancellation rooms, look for 8.0+ ratings with 100+ reviews, and check "
            "the distance to where you'll spend your time. Higher floors away from the lift are quieter."
        )
    return ""


# --------------------------------------------------------------------------
# Discounts and offers
# --------------------------------------------------------------------------
OFFER_RE = re.compile(
    r"(?:\b\d{1,2}\s?%\s*(?:off|discount|cashback)\b|₹\s*[\d,]+\s*(?:off|cashback)\b|"
    r"\bflat\s*₹?\s*[\d,]+|\buse code\s+[A-Z0-9]{3,}|\bcoupon\b|\bpromo code\b|"
    r"\bfree cancellation\b|\bgenius\b|\bearly (?:bird )?deal\b)",
    re.I,
)


def find_offers(text, limit=5):
    offers, seen = [], set()
    for line in (text or "").splitlines():
        line = clean_space(line)
        if 4 <= len(line) <= 140 and OFFER_RE.search(line) and line.lower() not in seen:
            seen.add(line.lower())
            offers.append(line)
            if len(offers) >= limit:
                break
    return offers


# --------------------------------------------------------------------------
# Reading listings from page TEXT (survives layout changes)
# --------------------------------------------------------------------------
PRICE_RE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d+)?)")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s?(AM|PM|am|pm)?")
DUR_RE = re.compile(r"\b(\d{1,2})\s*h(?:r|rs|ours?)?\s*(?:(\d{1,2})\s*m(?:in|ins)?)?\b", re.I)
SEATS_RE = re.compile(r"\b(\d{1,3})\s*seats?\b", re.I)
STOPS_RE = re.compile(r"\b(nonstop|non-stop|direct|(\d)\s*stops?)\b", re.I)
PCT_OFF_RE = re.compile(r"\b(\d{1,2})\s?%\s*off\b", re.I)
PRICE_BOUNDS = {"flight": (500, 300000), "bus": (100, 30000), "hotel": (300, 500000)}


def _is_title_line(line):
    if not (3 <= len(line) <= 70) or line.startswith("₹"):
        return False
    letters = sum(c.isalpha() for c in line)
    digits = sum(c.isdigit() for c in line)
    if letters < 3 or letters <= digits:
        return False
    return not re.search(r"\b(?:seats?|stops?|nonstop|reviews?|ratings?|cashback|off)\b", line, re.I)


def extract_listings(text, kind):
    lines = [clean_space(l) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    lo, hi = PRICE_BOUNDS.get(kind, (1, 10 ** 9))
    items, prev = [], -1

    for idx, line in enumerate(lines):
        if idx <= prev:
            continue
        nums = [float(x.replace(",", "")) for x in PRICE_RE.findall(line)]
        nums = [n for n in nums if lo <= n <= hi]
        if not nums:
            continue

        block = lines[prev + 1: idx + 1]
        tail = []
        for extra in lines[idx + 1: idx + 3]:
            if SEATS_RE.search(extra) or OFFER_RE.search(extra):
                tail.append(extra)
            else:
                break
        prev = idx + len(tail)

        title = next((l for l in block if _is_title_line(l)), None)
        if not title:
            continue

        joined = " | ".join(block + tail)
        price = min(nums)
        original = max(nums) if len(nums) > 1 and max(nums) > price * 1.05 else None

        times = [f"{t} {ap}".strip() for t, ap in TIME_RE.findall(joined)][:2]
        dur = DUR_RE.search(joined)
        minutes = int(dur.group(1)) * 60 + int(dur.group(2) or 0) if dur else None
        stops_m = STOPS_RE.search(joined)
        stops = None
        if stops_m:
            stops = int(stops_m.group(2)) if stops_m.group(2) else 0
        rating = None
        for l in block:
            r = re.fullmatch(r"[★⭐]?\s?([1-5]\.\d)", l)
            if r:
                rating = float(r.group(1))
                break
        seats = SEATS_RE.search(joined)
        pct = PCT_OFF_RE.search(joined)
        discount = (
            round((1 - price / original) * 100) if original
            else int(pct.group(1)) if pct else None
        )

        items.append({
            "title": title, "price": price, "original": original, "discount_pct": discount,
            "depart": times[0] if times else None, "arrive": times[1] if len(times) > 1 else None,
            "minutes": minutes, "stops": stops, "rating": rating,
            "seats": int(seats.group(1)) if seats else None,
        })
    return items


# --------------------------------------------------------------------------
# Fetching pages
# --------------------------------------------------------------------------
async def fetch_text(url, need=None, settle_ms=3500):
    """Return (text, blocked). text is None if nothing usable was found."""
    async with async_playwright() as p:
        browser, context = await launch(p)
        try:
            page = await context.new_page()

            async def attempt(i):
                await page.goto(
                    url, wait_until="domcontentloaded" if i == 0 else "load", timeout=40000
                )
                await page.wait_for_timeout(settle_ms + 1500 * i)
                for _ in range(4):
                    await page.mouse.wheel(0, 2500)
                    await page.wait_for_timeout(400)
                text = await page.locator("body").inner_text(timeout=8000)
                if need and not need.search(text):
                    raise RuntimeError("expected content missing")
                return text

            try:
                return await with_retries(attempt, attempts=2, label="booking"), False
            except Exception:
                blocked = await looks_blocked(page)
                await dump_debug(page, "booking")
                return None, blocked
        finally:
            await browser.close()


_HOTEL_JS = """() => [...document.querySelectorAll('[data-testid="property-card"]')].slice(0, 40).map(c => {
  const q = s => { const e = c.querySelector(s); return e ? e.innerText.trim() : ''; };
  const a = c.querySelector('a[href]');
  return {title: q('[data-testid="title"]'),
          price: q('[data-testid="price-and-discounted-price"]'),
          rating: q('[data-testid="review-score"]'),
          address: q('[data-testid="address"]'),
          text: (c.innerText || '').slice(0, 700),
          href: a ? a.href : ''};
})"""


async def fetch_hotels(url):
    """Return (rows, text, blocked). rows come from structured cards when possible."""
    try:
        async with async_playwright() as p:
            browser, context = await launch(p)
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                try:
                    await page.wait_for_selector('[data-testid="property-card"]', timeout=15000)
                    for _ in range(3):
                        await page.mouse.wheel(0, 2500)
                        await page.wait_for_timeout(400)
                    return await page.evaluate(_HOTEL_JS), None, False
                except PWTimeout:
                    blocked = await looks_blocked(page)
                    await dump_debug(page, "hotels")
                    text = await page.locator("body").inner_text(timeout=8000)
                    return [], text, blocked
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("Hotel fetch failed: %s", exc)
        return [], None, False


def hotel_from_row(row):
    text = row.get("text", "")
    nums = [float(x.replace(",", "")) for x in PRICE_RE.findall(row.get("price") or text)]
    nums = [n for n in nums if n >= 300]
    title = clean_space(row.get("title"))
    if not title or not nums:
        return None
    price = min(nums)
    original = max(nums) if len(nums) > 1 and max(nums) > price * 1.05 else None
    r = re.search(r"(\d{1,2}(?:\.\d)?)", row.get("rating") or "")
    rating = float(r.group(1)) if r and float(r.group(1)) <= 10 else None
    reviews = re.search(r"([\d,]+)\s+reviews?", text, re.I)
    return {
        "title": title, "price": price, "original": original,
        "discount_pct": round((1 - price / original) * 100) if original else None,
        "rating": rating,
        "reviews": int(reviews.group(1).replace(",", "")) if reviews else None,
        "address": clean_space(row.get("address")),
        "free_cancel": "free cancellation" in text.lower(),
        "url": row.get("href") or None,
    }


# --------------------------------------------------------------------------
# Ranking and presenting
# --------------------------------------------------------------------------
def unit_price(item, nights):
    return item["price"] / max(nights, 1)


def effective_cost(item, kind, nights=1):
    if kind == "flight":
        return item["price"] + 200 * (item.get("minutes") or 180) / 60 + 500 * (item.get("stops") or 0)
    if kind == "bus":
        shortfall = max(0.0, 4.2 - (item.get("rating") or 3.8))
        low_seats = 100 if item.get("seats") is not None and item["seats"] <= 5 else 0
        return item["price"] * (1 + 0.25 * shortfall) + low_seats
    if kind == "hotel":
        rating = max(item.get("rating") or 7.0, 5.0)
        return unit_price(item, nights) * (8.5 / rating) ** 2
    return item["price"]


def rank(items, kind, budget, nights=1):
    """Return (ranked_items_with_labels, over_budget_note)."""
    note = ""
    pool = items
    if budget is not None:
        within = [i for i in items if unit_price(i, nights) <= budget]
        if within:
            pool = within
        elif items:
            cheapest = min(unit_price(i, nights) for i in items)
            note = f"Nothing was found within ₹{budget:,.0f}; the cheapest I found is ₹{cheapest:,.0f}, so these are the closest."
            pool = sorted(items, key=lambda i: i["price"])[:5]
    pool = sorted(pool, key=lambda i: effective_cost(i, kind, nights))[:12]
    if not pool:
        return [], note

    labels = {id(pool[0]): "Best pick"}
    labels.setdefault(id(min(pool, key=lambda i: i["price"])), "Cheapest")
    if kind == "flight":
        timed = [i for i in pool if i.get("minutes")]
        if timed:
            labels.setdefault(id(min(timed, key=lambda i: i["minutes"])), "Fastest")
    else:
        rated = [i for i in pool if i.get("rating")]
        if rated:
            labels.setdefault(id(max(rated, key=lambda i: i["rating"])), "Top rated")
    deals = [i for i in pool if (i.get("discount_pct") or 0) >= 10]
    if deals:
        labels.setdefault(id(max(deals, key=lambda i: i["discount_pct"])), "Biggest discount")
    for i in pool:
        i["label"] = labels.get(id(i))
    return pool, note


def describe(item, kind, nights=1):
    parts = [item["label"]] if item.get("label") else []
    price = f"₹{item['price']:,.0f}"
    if item.get("original"):
        price += f" (was ₹{item['original']:,.0f}, {item['discount_pct']}% off)"
    elif item.get("discount_pct"):
        price += f" ({item['discount_pct']}% off)"
    if kind == "hotel" and nights > 1:
        price += f" total · ₹{unit_price(item, nights):,.0f}/night"
    parts.append(price)
    if item.get("depart") and item.get("arrive"):
        parts.append(f"{item['depart']} → {item['arrive']}")
    if item.get("minutes"):
        parts.append(f"{item['minutes'] // 60}h {item['minutes'] % 60:02d}m")
    if item.get("stops") is not None:
        parts.append("Non-stop" if item["stops"] == 0 else f"{item['stops']} stop(s)")
    if item.get("rating"):
        parts.append(f"Rating {item['rating']}" + ("/10" if kind == "hotel" else "/5"))
    if item.get("reviews"):
        parts.append(f"{item['reviews']:,} reviews")
    if item.get("seats") is not None:
        parts.append(f"{item['seats']} seats left")
    if item.get("free_cancel"):
        parts.append("Free cancellation")
    if item.get("address"):
        parts.append(item["address"])
    return " · ".join(parts)


def tidy_city(name):
    drop = {
        "bus", "buses", "flight", "flights", "ticket", "tickets", "volvo", "ac", "non",
        "cheap", "cheapest", "best", "sleeper", "seater", "book", "booking", "airlines",
    }
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in drop]
    return " ".join(words).strip()


def parse_place(q):
    m = re.search(
        r"\b(?:in|at|near|around)\s+([a-z][a-z .'-]{2,40}?)"
        r"(?=\s+(?:on|from|for|under|below|within|upto|up to|tomorrow|today|tonight|next|this|with|check|\d|"
        + MON + r")\b|\s*[,.]|\s*$)",
        q.lower(),
    )
    return clean_space(m.group(1)).title() if m else None


def finish(kind, label, url, items, budget, offers, extra, nights=1, assumed="", wants_open=False):
    ranked, note = rank(items, kind, budget, nights)
    results = []
    for i in ranked:
        entry = {
            "title": i["title"],
            "url": i.get("url") or url,
            "snippet": describe(i, kind, nights),
        }
        if i.get("map_query"):
            entry["map_url"] = maps_search_url(i["map_query"])
            entry["embed"] = maps_embed_url(i["map_query"])
        results.append(entry)

    if offers:
        results.append({
            "title": "Offers visible on the page",
            "url": url,
            "snippet": " · ".join(offers[:4]) + ". Bank and card offers usually appear at payment, so check there too.",
        })
    results.extend(extra)
    if wants_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    tip = seat_tips(kind, label)
    reply = (
        f"Found {len(ranked)} options for {label}{assumed}. "
        + (note + " " if note else (f"All within ₹{budget:,.0f}. " if budget is not None else ""))
        + "Prices change quickly, so confirm on the booking site. "
        + (tip + " " if tip else "")
        + "Login, OTP and payment are always yours; I never book or pay."
    )
    return {"reply": reply, "results": results}


def no_results(label, url, why, extra, assumed=""):
    return {
        "reply": f"{why} {label}{assumed}. Open the links below to see live options.",
        "results": [{"title": f"Open search: {label}", "url": url, "snippet": "Live prices and availability."}] + extra,
    }


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
BUS_ALIASES = {"bengaluru": "bangalore", "mysuru": "mysore", "mangaluru": "mangalore",
               "kochi": "ernakulam", "thiruvananthapuram": "trivandrum"}


async def search_buses(query):
    route = parse_route(query)
    if not route:
        return {"reply": "Tell me the route and date, e.g. \"bus from Bengaluru to Chennai tomorrow under 1000\".", "results": []}
    src, dst = tidy_city(route[0]), tidy_city(route[1])
    when, assumed = parse_date(query)
    budget = extract_budget(query)
    s_slug = BUS_ALIASES.get(src, src).replace(" ", "-")
    d_slug = BUS_ALIASES.get(dst, dst).replace(" ", "-")
    url = (
        f"https://www.redbus.in/bus-tickets/{s_slug}-to-{d_slug}"
        f"?fromCityName={quote_plus(src.title())}&toCityName={quote_plus(dst.title())}"
        f"&onward={when:%d-%b-%Y}&doj={when:%d-%b-%Y}"
    )
    label = f"{src.title()} → {dst.title()} bus on {when:%a %d %b %Y}"
    note = " (date not given, so I assumed tomorrow)" if assumed else ""
    extra = [
        map_item(f"{src.title()} bus stand", f"{src.title()} bus stand"),
        map_item(f"{dst.title()} bus stand", f"{dst.title()} bus stand"),
    ]
    text, blocked = await fetch_text(url, need=PRICE_RE)
    items = extract_listings(text, "bus") if text else []
    if not items:
        why = "The bus site showed a bot check." if blocked else "I couldn't read bus listings (the layout may have changed)."
        return no_results(label, url, why, extra, note)
    return finish("bus", label, url, items, budget, find_offers(text), extra, assumed=note, wants_open=wants_booking(query))


async def search_flights(query):
    route = parse_route(query)
    if not route:
        return {"reply": "Tell me the route and date, e.g. \"flights from Bengaluru to Delhi on 5 nov under 6000\".", "results": []}
    src, dst = tidy_city(route[0]).title(), tidy_city(route[1]).title()
    when, assumed = parse_date(query)
    budget = extract_budget(query)
    url = (
        "https://www.google.com/travel/flights?q="
        + quote_plus(f"Flights from {src} to {dst} on {when:%Y-%m-%d}") + "&curr=INR"
    )
    label = f"{src} → {dst} flights on {when:%a %d %b %Y}"
    note = " (date not given, so I assumed tomorrow)" if assumed else ""
    extra = [map_item(f"{src} airport", f"{src} airport"), map_item(f"{dst} airport", f"{dst} airport")]
    text, blocked = await fetch_text(url, need=PRICE_RE, settle_ms=4500)
    items = extract_listings(text, "flight") if text else []
    if not items:
        why = "The flight site showed a bot check." if blocked else "I couldn't read flight listings (the layout may have changed)."
        return no_results(label, url, why, extra, note)
    return finish("flight", label, url, items, budget, find_offers(text), extra, assumed=note, wants_open=wants_booking(query))


async def search_hotels(query):
    place = parse_place(query)
    if not place:
        return {"reply": "Which city or area? e.g. \"hotel in Goa under 3000 for 2 nights from 25 dec\".", "results": []}
    when, assumed = parse_date(query)
    nm = re.search(r"(\d{1,2})\s*nights?", query, re.I)
    nights = max(1, min(int(nm.group(1)), 30)) if nm else 1
    gm = re.search(r"(\d{1,2})\s*(?:guests?|people|adults|persons)", query, re.I)
    guests = int(gm.group(1)) if gm else 2
    budget = extract_budget(query)  # treated as a per-night budget
    checkout = when + timedelta(days=nights)
    url = (
        "https://www.booking.com/searchresults.html?ss=" + quote_plus(place)
        + f"&checkin={when:%Y-%m-%d}&checkout={checkout:%Y-%m-%d}"
        + f"&group_adults={guests}&no_rooms=1&selected_currency=INR"
    )
    label = f"hotels in {place}, {when:%d %b} for {nights} night(s), {guests} guest(s)"
    note = " (date not given, so I assumed tomorrow)" if assumed else ""
    extra = [map_item(f"{place} on the map", place), map_item(f"Hotels in {place}", f"hotels in {place}")]

    rows, text, blocked = await fetch_hotels(url)
    items = [h for h in (hotel_from_row(r) for r in rows) if h]
    for h in items:
        h["map_query"] = f"{h['title']} {place}"
    if not items and text:
        items = extract_listings(text, "hotel")
        for h in items:
            h["map_query"] = f"{h['title']} {place}"
    if not items:
        why = "The hotel site showed a bot check." if blocked else "I couldn't read hotel listings (the layout may have changed)."
        return no_results(label, url, why, extra, note)
    offers = find_offers(text or " ".join(r.get("text", "") for r in rows))
    resp = finish("hotel", label, url, items, budget, offers, extra, nights=nights,
                  assumed=note, wants_open=wants_booking(query))
    if budget is not None:
        resp["reply"] = resp["reply"].replace("All within", "All within (per night)")
    return resp


async def search_movies(query):
    place = parse_place(query) or "your city"
    slug = place.lower().replace(" ", "-")
    bms = f"https://in.bookmyshow.com/explore/movies-{slug}"
    google = "https://www.google.com/search?q=" + quote_plus(f"movie showtimes in {place}")
    if wants_booking(query):
        try:
            webbrowser.open(bms)
        except Exception:
            pass
    return {
        "reply": (
            f"Movie booking needs seat selection and payment on the cinema site, so I'll take you "
            f"there and help you choose. Use the Google link to compare showtimes and theatres, then "
            f"BookMyShow to book. {seat_tips('movie', query)} For discounts, check bank/card offers "
            "and cinema-chain deals on the payment page, and prefer weekday or morning shows, which are usually cheaper."
        ),
        "results": [
            {"title": f"Showtimes in {place} (Google)", "url": google, "snippet": "Theatres and timings side by side."},
            {"title": f"BookMyShow: movies in {place}", "url": bms, "snippet": "Pick a show, select seats, pay."},
            map_item(f"Cinemas near {place}", f"movie theatres in {place}", "See theatres on the map"),
        ],
    }


async def search_train_with_maps(query):
    resp = await search_trains(query)
    route = parse_route(query)
    if route and resp.get("results"):
        resp["results"] += [
            map_item(f"{route[0].title()} railway station", f"{route[0]} railway station"),
            map_item(f"{route[1].title()} railway station", f"{route[1]} railway station"),
        ]
        tip = seat_tips("train", query)
        if tip and "Seat tip" not in resp["reply"]:
            resp["reply"] += " Seat tip: " + tip
    return resp


# --------------------------------------------------------------------------
# Entry point used by automation.search_web
# --------------------------------------------------------------------------
async def route_booking(query):
    """Return a response dict, or None if this isn't a booking/map request."""
    q = query or ""
    if MAP_STRONG_RE.search(q):
        return search_map(q)
    category = detect_category(q)
    if category is None:
        return None
    try:
        if category == "train":
            return await search_train_with_maps(q)
        if category == "bus":
            return await search_buses(q)
        if category == "flight":
            return await search_flights(q)
        if category == "hotel":
            return await search_hotels(q)
        return await search_movies(q)
    except Exception:
        logger.exception("Booking request failed")
        return {
            "reply": "Sorry, that booking search failed unexpectedly. Please try again, and check the terminal if it keeps happening.",
            "results": [],
        }