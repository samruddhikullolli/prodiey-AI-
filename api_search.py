"""Prodieyy AI fast search via the Serper.dev API (Google web, shopping, places).

Set SERPER_API_KEY in your environment (free signup at https://serper.dev).
Every function returns None when the key is missing or the call fails, so the
caller can fall back to the Playwright scrapers.
"""
import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import quote_plus

logger = logging.getLogger("prodieyy.api")

BASE = "https://google.serper.dev"
TIMEOUT = 12


def enabled():
    return bool(os.getenv("SERPER_API_KEY"))


LAST_ERROR = ""   # why the last API call failed, shown to the user instead of a vague message


def _post_sync(endpoint, payload, key):
    req = urllib.request.Request(
        f"{BASE}/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "X-API-KEY": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; ProdieyyAI/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:160]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {body}".strip())


async def _post(endpoint, payload):
    global LAST_ERROR
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        LAST_ERROR = "SERPER_API_KEY is not set in the terminal running the app"
        return None
    payload = {"gl": "in", "hl": "en", **payload}
    for attempt in range(2):
        try:
            data = await asyncio.to_thread(_post_sync, endpoint, payload, key)
            LAST_ERROR = ""
            return data
        except Exception as exc:
            LAST_ERROR = f"Serper {endpoint}: {exc}"
            logger.warning("%s (attempt %d)", LAST_ERROR, attempt + 1)
            if "HTTP 4" in str(exc) and "HTTP 429" not in str(exc):
                break                      # a bad key or request will not fix itself
            await asyncio.sleep(0.8)
    return None


def _num(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _bayes(rating, votes, mean=4.1, weight=100):
    return (votes / (votes + weight)) * rating + (weight / (votes + weight)) * mean


# ------------------------------------------------------------------ web
async def api_web_search(query):
    global LAST_ERROR
    data = await _post("search", {"q": query, "num": 10})
    if not data:
        return None
    if not data.get("organic"):
        LAST_ERROR = "Serper returned no web results for this query"
        return None
    results = [
        {"title": r.get("title", ""), "url": r["link"], "snippet": (r.get("snippet") or "")[:400]}
        for r in data["organic"] if r.get("link")
    ]
    reply = f"Found {len(results)} web results."
    box = data.get("answerBox") or {}
    direct = box.get("answer") or box.get("snippet")
    if direct:
        reply = f"{direct}\n\n{reply}"
    return {"reply": reply, "results": results[:10]}


# ------------------------------------------------------------- shopping
async def api_product_search(query, budget=None, limit=15):
    data = await _post("shopping", {"q": query, "num": 40})
    rows = (data or {}).get("shopping") or []
    if not rows:
        return None

    items, seen = [], set()
    for r in rows:
        title, url = r.get("title"), r.get("link")
        price = _num(r.get("price"))
        if not title or not url or title.lower() in seen:
            continue
        if budget is not None and (price is None or price > budget):
            continue
        seen.add(title.lower())
        rating = r.get("rating")
        votes = int(r.get("ratingCount") or 0)
        items.append({
            "title": title, "url": url, "price": price,
            "price_text": r.get("price") or "Price not verified",
            "rating": float(rating) if rating else None,
            "reviews": votes or None,
            "image": r.get("imageUrl"), "merchant": r.get("source", ""),
            "_score": _bayes(float(rating), votes) if rating else 0.0,
        })

    good = [i for i in items if i["rating"] and i["rating"] >= 4.0 and (i["reviews"] or 0) >= 50]
    pool = good if len(good) >= 3 else [i for i in items if i["rating"]] or items
    pool.sort(key=lambda i: i["_score"], reverse=True)
    top = pool[:limit]
    if not top:
        return None

    for n, i in enumerate(top):
        tags = ["Best overall"] if n == 0 else []
        bits = tags + [i["merchant"]]
        if i["rating"]:
            bits.append(f"{i['rating']:.1f}/5 ({i['reviews'] or 0:,} ratings)")
        i["snippet"] = " · ".join(b for b in bits if b)

    reply = (
        f"Top {len(top)} picks from Google Shopping, ranked by rating weighted by number of ratings."
        + (f" Only items up to ₹{budget:,} with a visible price are shown." if budget else "")
        + " Confirm price and stock on the store page before buying."
    )
    keys = ("title", "url", "price", "price_text", "rating", "reviews", "image", "merchant", "snippet")
    return {"reply": reply, "results": [{k: i[k] for k in keys} for i in top]}


# --------------------------------------------------------------- places
async def api_place_search(what, area, budget=None, limit=8):
    q = f"{what} in {area}"
    data = await _post("places", {"q": q})
    rows = (data or {}).get("places") or []
    if not rows:
        return None

    places = []
    for r in rows:
        if not r.get("title") or not r.get("rating"):
            continue
        votes = int(r.get("ratingCount") or 0)
        places.append({**r, "_votes": votes, "_score": _bayes(float(r["rating"]), votes, 4.2)})
    strong = [p for p in places if p["rating"] >= 4.0 and p["_votes"] >= 50]
    pool = strong if len(strong) >= 3 else places
    pool.sort(key=lambda p: p["_score"], reverse=True)
    top = pool[:limit]
    if not top:
        return None

    all_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)
    results = []
    for n, p in enumerate(top, 1):
        mq = f"{p['title']} {p.get('address', '')}".strip()
        link = (f"https://www.google.com/maps?cid={p['cid']}" if p.get("cid")
                else "https://www.google.com/maps/search/?api=1&query=" + quote_plus(mq))
        bits = [p.get("category"), p.get("address"), p.get("phoneNumber")]
        results.append({
            "title": f"{n}. {p['title']}  {p['rating']:.1f}/5 ({p['_votes']:,})",
            "url": p.get("website") or link,
            "snippet": " · ".join(b for b in bits if b) or "Open in Google Maps.",
            "map_url": link,
            "embed": "https://maps.google.com/maps?q=" + quote_plus(mq) + "&output=embed",
        })
    results.append({"title": f"See all '{what}' in {area} on Google Maps", "url": all_url,
                    "snippet": "Full list, photos, menus and reviews.", "map_url": all_url})
    best = top[0]
    reply = (f"Top {len(top)} {what} in {area}, ranked by Google rating weighted by review count. "
             f"Best overall: {best['title']} ({best['rating']:.1f}/5, {best['_votes']:,} reviews).")
    return {"reply": reply, "results": results}