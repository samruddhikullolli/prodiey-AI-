"""Prodieyy AI automation: shopping search with self-healing selectors."""
import asyncio
import base64
import ipaddress
import json
import logging
import math
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prodieyy.automation")

ROOT = Path(__file__).resolve().parent
DEBUG_DIR = ROOT / "debug"
MEMORY_FILE = ROOT / "selectors.json"

# Set PRODIEYY_HEADLESS=0 to watch the browser (also helps with bot checks).
HEADLESS = os.getenv("PRODIEYY_HEADLESS", "1") != "0"
MAX_CARDS_PER_STORE = 60
MAX_RECOMMENDATIONS = 15
PAGES_TO_INSPECT = 8

# --------------------------------------------------------------------------
# Store definitions. Each field has an ordered list of (selector, attribute)
# fallbacks. attribute=None means "read the element text".
# --------------------------------------------------------------------------
STORES = [
    {
        "name": "Amazon India",
        "domain": "amazon.in",
        "search": "https://www.amazon.in/s?k={query}",
        "cards": [
            '[data-component-type="s-search-result"][data-asin]:not([data-asin=""])',
            '[data-component-type="s-search-result"]',
        ],
        "fields": {
            "title": [
                ("h2 a span", None),
                ("h2 span", None),
                ("[data-cy='title-recipe'] h2", None),
            ],
            "link": [
                ("h2 a", "href"),
                ("a.a-link-normal.s-no-outline", "href"),
                ("a.a-link-normal[href*='/dp/']", "href"),
            ],
            "price": [
                (".a-price .a-offscreen", None),
                ("span.a-price", None),
            ],
            "rating": [
                ("span.a-icon-alt", None),
                ("[aria-label*='out of 5 stars']", "aria-label"),
            ],
            "reviews": [
                ("a[aria-label*='ratings']", "aria-label"),
                ("span.a-size-base.s-underline-text", None),
            ],
            "image": [
                ("img.s-image", "src"),
                ("img", "src"),
            ],
        },
    },
    {
        "name": "Flipkart",
        "domain": "flipkart.com",
        "search": "https://www.flipkart.com/search?q={query}",
        "cards": ["div[data-id]", "div[data-tkid]"],
        "fields": {
            "title": [
                ("a[title]", "title"),
                (".KzDlHZ", None),
                (".RG5Slk", None),
                ("._4rR01T", None),
                ("div.syl9yP", None),
            ],
            "link": [
                ("a[href*='/p/']", "href"),
                ("a[href]", "href"),
            ],
            "price": [
                (".Nx9bqj", None),
                ("._30jeq3", None),
                ("div.hZ3P6w", None),
            ],
            "rating": [
                (".XQDdHH", None),
                ("._3LWZlK", None),
            ],
            "reviews": [
                (".Wphh3N", None),
                ("._2_R_DZ", None),
            ],
            "image": [
                ("img", "src"),
                ("img", "data-src"),
            ],
        },
    },
]

# Feature evidence checks (regex). They only count when the page text
# actually mentions the feature; nothing is assumed.
FEATURES = {
    "bass": [r"\bbass\b", r"\bsubwoofer\b"],
    "sound": [r"sound quality", r"audio quality", r"clear sound", r"hi-?fi"],
    "battery": [r"\bbattery\b", r"playtime", r"hours? (?:of )?(?:playback|playtime)"],
    "comfort": [r"comfort\w*", r"lightweight", r"ergonomic"],
    "noise_cancellation": [r"noise[- ]cancel\w*", r"\banc\b", r"\benc\b"],
    "water_resistance": [r"water[- ]?resistan\w*", r"waterproof", r"\bipx?\d{1,2}\b"],
    "gaming": [r"\bgaming\b", r"low[- ]latency", r"\blatency\b"],
    "camera": [r"\bcamera\b", r"photo quality", r"night mode"],
    "performance": [r"\bperformance\b", r"\bprocessor\b", r"\bspeed\b"],
    "display": [r"\bdisplay\b", r"\bamoled\b", r"screen quality"],
    "durability": [r"durab\w*", r"build quality", r"long[- ]lasting"],
    "portable": [r"portab\w*", r"\bcompact\b", r"lightweight"],
    "charging": [r"fast[- ]charg\w*", r"quick[- ]charg\w*", r"charging speed"],
}

STOPWORDS = {
    "i", "want", "need", "find", "me", "a", "an", "the", "for", "with", "and",
    "or", "of", "to", "in", "on", "that", "this", "is", "are", "from", "good",
    "best", "excellent", "great", "please", "buy", "product", "products",
    "under", "below", "less", "than", "within", "upto", "up", "price",
    "budget", "around", "rs", "rupees", "inr", "recommend", "recommendation",
    "compare", "customer", "customers", "positive", "high", "rating",
    "ratings", "review", "reviews", "some", "show", "get", "give", "my",
}

# Budget like "under 2000", "below ₹2,000", "Rs. 50k", "₹1500".
_UNITS = (
    r"minutes?|mins?|hours?|hrs?|days?|years?|months?|weeks?|seconds?|"
    r"km|kg|gb|tb|mb|mah|inch|inches|cm|mm|mp|hz"
)
BUDGET_RE = re.compile(
    r"(?:\b(?:under|below|less than|within|upto|up to|max|maximum)\s*"
    r"(?:₹|\brs\.?|\binr)?\s*|₹\s*|\b(?:rs\.?|inr)\s*)"
    r"(\d[\d,]*(?:\.\d+)?)(?!\d)\s*(k\b)?"
    r"(?!\s*(?:" + _UNITS + r")\b)",
    re.I,
)

PRODUCT_WORDS_RE = re.compile(
    r"\b(?:buy|shop|shopping|product|products|recommend|compare|price|"
    r"earbuds|earphones|headphones|headset|laptops?|phones?|mobiles?|"
    r"smartphones?|perfumes?|shoes|sneakers|watch|watches|keyboards?|mouse|"
    r"dress|bags?|skincare|makeup|lipsticks?|tablets?|smartwatch|cameras?|"
    r"speakers?|monitors?|tv|television|fridge|refrigerator|washing machine|"
    r"powerbank|charger|backpack|trimmer|shampoo|moisturi[sz]er|sunscreen)\b",
    re.I,
)

TICKET_RE = re.compile(
    r"\b(?:train ticket|irctc|pnr|bus ticket|flight ticket|movie ticket|"
    r"movie tickets|theat(?:re|er) ticket|bookmyshow|book (?:a )?ticket|"
    r"cancel (?:my |a )?ticket|tatkal|showtimes?)\b",
    re.I,
)


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------
def clean_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_budget(query):
    match = BUDGET_RE.search(query or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if match.group(2):
        value *= 1000
    value = int(value)
    return value if value > 0 else None


def clean_query(query):
    return clean_space(BUDGET_RE.sub(" ", query or ""))


def is_ticket_query(query):
    return bool(TICKET_RE.search(query or ""))


def is_product_search(query):
    return extract_budget(query) is not None or bool(
        PRODUCT_WORDS_RE.search(query or "")
    )


def extract_preferences(query):
    """Return (requested_feature_names, core_keywords)."""
    q = (query or "").lower()
    requested = [
        feature
        for feature, patterns in FEATURES.items()
        if any(re.search(p, q) for p in patterns)
    ]

    tokens = re.findall(r"[a-z0-9]+", clean_query(q))
    core = []
    for tok in tokens:
        if tok in STOPWORDS or len(tok) < 2 or tok.isdigit():
            continue
        if any(re.fullmatch(p, tok) for pats in FEATURES.values() for p in pats):
            continue  # feature words are scored separately
        stem = tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok
        core.append(stem)
    return requested, list(dict.fromkeys(core))


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------
def extract_price(text, allow_bare=False):
    if not text:
        return None
    for pattern in (
        r"₹\s*([\d,]+(?:\.\d{1,2})?)",
        r"\bRs\.?\s*([\d,]+(?:\.\d{1,2})?)",
        r"\bINR\s*([\d,]+(?:\.\d{1,2})?)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                if value > 0:
                    return value
            except ValueError:
                pass
    if allow_bare:
        match = re.fullmatch(r"\s*([\d,]+(?:\.\d{1,2})?)\s*", text)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                if value > 0:
                    return value
            except ValueError:
                pass
    return None


def parse_rating(text, loose=False):
    """loose=True is only for text from a dedicated rating element."""
    if not text:
        return None
    match = re.search(r"([0-5](?:\.\d+)?)\s*(?:out of 5|/\s*5)", text, re.I)
    if match:
        return float(match.group(1))
    if loose:
        match = re.fullmatch(r"\s*([0-5](?:\.\d)?)\s*(?:★)?\s*", text)
        if match:
            return float(match.group(1))
    return None


def _count_from_token(token):
    token = token.replace(",", "").strip().lower()
    mult = 1
    if token.endswith("k"):
        mult, token = 1000, token[:-1]
    elif token.endswith("m"):
        mult, token = 1_000_000, token[:-1]
    try:
        value = int(float(token) * mult)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_review_count(text, loose=False):
    """loose=True is only for text from a dedicated review-count element."""
    if not text:
        return None
    match = re.search(
        r"([\d][\d,.]*\s*[kKmM]?)\s*\+?\s*(?:global\s+)?(?:ratings?|reviews?)",
        text,
        re.I,
    )
    if match:
        return _count_from_token(match.group(1).replace(" ", ""))
    if loose:
        match = re.fullmatch(r"\(?\s*([\d][\d,.]*\s*[kKmM]?)\s*\)?", text.strip())
        if match:
            return _count_from_token(match.group(1).replace(" ", ""))
    return None


def normalise_url(url, base_url, domain):
    if not url:
        return None
    url = urljoin(base_url, url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower().split(":")[0]
    if not (host == domain or host.endswith("." + domain)):
        return None
    return url.split("#")[0]


# --------------------------------------------------------------------------
# Self-healing: selector memory, retries, debug dumps, heuristics
# --------------------------------------------------------------------------
class SelectorMemory:
    """Remembers which selector last worked for each store/field."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            if path.exists():
                self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not read %s; starting fresh", path)
            self.data = {}

    def get(self, store, field):
        entry = self.data.get(store, {}).get(field)
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
            return (entry[0], entry[1])
        return None

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:
            logger.warning("Could not save selector memory: %s", exc)

    def remember(self, store, field, spec):
        self.data.setdefault(store, {})[field] = [spec[0], spec[1]]
        self._save()

    def forget(self, store, field):
        if field in self.data.get(store, {}):
            del self.data[store][field]
            self._save()


MEMORY = SelectorMemory(MEMORY_FILE)


async def with_retries(fn, attempts=2, base_delay=1.5, label=""):
    """Call `await fn(attempt_number)` with exponential backoff."""
    last = None
    for attempt in range(attempts):
        try:
            return await fn(attempt)
        except Exception as exc:
            last = exc
            logger.warning(
                "%s attempt %d/%d failed: %s", label, attempt + 1, attempts, exc
            )
            if attempt < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last


async def dump_debug(page, label):
    """Save a screenshot and the HTML so a broken layout can be inspected."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-z0-9]+", "_", label.lower())
        await page.screenshot(path=str(DEBUG_DIR / f"{safe}_{stamp}.png"))
        html = await page.content()
        (DEBUG_DIR / f"{safe}_{stamp}.html").write_text(html, encoding="utf-8")
        logger.info("Saved debug files for %s in %s", label, DEBUG_DIR)
    except Exception as exc:
        logger.info("Could not save debug files: %s", exc)


async def looks_blocked(page):
    try:
        title = (await page.title()).lower()
        body = (await page.locator("body").inner_text(timeout=2500)).lower()[:1500]
    except Exception:
        return False
    text = title + " " + body
    return any(
        key in text
        for key in (
            "captcha", "robot check", "enter the characters you see",
            "unusual traffic", "access denied", "are you a human",
        )
    )


async def read_value(card, selector, attr):
    try:
        loc = card.locator(selector)
        if await loc.count() == 0:
            return ""
        first = loc.first
        if attr:
            value = await first.get_attribute(attr, timeout=1500)
        else:
            value = await first.text_content(timeout=1500)
        return clean_space(value)
    except Exception:
        return ""


async def resolve_field(card, store, field, validate, heuristic=None, stats=None):
    """Try the learned selector, then configured fallbacks, then a heuristic."""
    specs = store["fields"][field]
    primary = specs[0]
    learned = MEMORY.get(store["name"], field)

    candidates = []
    if learned:
        candidates.append(learned)
    for spec in specs:
        if spec not in candidates:
            candidates.append(spec)

    for spec in candidates:
        raw = await read_value(card, spec[0], spec[1])
        if raw and validate(raw):
            if spec != learned:
                if spec == primary:
                    MEMORY.forget(store["name"], field)
                else:
                    MEMORY.remember(store["name"], field, spec)
                    logger.info(
                        "Self-healed %s.%s -> %s", store["name"], field, spec[0]
                    )
            if spec != primary and stats is not None:
                stats["healed"].add(field)
            return raw

    if heuristic is not None:
        try:
            raw = await heuristic(card)
        except Exception:
            raw = ""
        if raw and validate(raw):
            if stats is not None:
                stats["healed"].add(field)
            return raw
    return ""


_TITLE_JS = """els => {
  let best = '';
  for (const e of els) {
    const raw = e.getAttribute('title') || e.getAttribute('alt') ||
                (e.innerText || '').split('\\n')[0] || '';
    const t = raw.trim().replace(/\\s+/g, ' ');
    if (t.length > best.length && t.length < 250) best = t;
  }
  return best;
}"""


async def heuristic_title(card):
    return await card.locator("a, h2, h3, h4, img[alt]").evaluate_all(_TITLE_JS)


async def heuristic_link(card):
    hrefs = await card.locator("a[href]").evaluate_all(
        "els => els.map(e => e.href)"
    )
    for href in hrefs:
        if re.search(r"/dp/|/gp/product/|/p/|pid=|/itm", href):
            return href
    return hrefs[0] if hrefs else ""


async def heuristic_price(card):
    text = await card.inner_text(timeout=2000)
    value = extract_price(text)
    return f"₹{value:,.0f}" if value else ""


async def heuristic_rating(card):
    text = await card.inner_text(timeout=2000)
    value = parse_rating(text)
    if value is not None:
        return f"{value} out of 5"
    for line in text.splitlines():
        value = parse_rating(line, loose=True)
        if value is not None:
            return f"{value} out of 5"
    return ""


async def heuristic_reviews(card):
    text = await card.inner_text(timeout=2000)
    value = parse_review_count(text)
    if value is not None:
        return f"{value} ratings"
    match = re.search(r"\(\s*([\d,]+(?:\.\d+)?\s*[kK]?)\s*\)", text)
    if match:
        value = _count_from_token(match.group(1).replace(" ", ""))
        if value is not None:
            return f"{value} ratings"
    return ""


# --------------------------------------------------------------------------
# Browser setup
# --------------------------------------------------------------------------
async def _route_filter(route):
    if route.request.resource_type in ("image", "font", "media"):
        await route.abort()
    else:
        await route.continue_()


async def launch(p):
    browser = await p.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1365, "height": 900},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    await context.route("**/*", _route_filter)
    return browser, context


# --------------------------------------------------------------------------
# Store scraping
# --------------------------------------------------------------------------
async def scrape_store(context, store, query, budget):
    """Return (items, note). Never raises for ordinary scraping problems."""
    name = store["name"]
    page = await context.new_page()
    stats = {"healed": set()}
    search_url = store["search"].format(query=quote_plus(query))
    chosen = {"cards": None}

    async def attempt(i):
        await page.goto(
            search_url,
            wait_until="domcontentloaded" if i == 0 else "load",
            timeout=30000 + i * 15000,
        )
        await page.wait_for_timeout(700 + 600 * i)
        for _ in range(4):  # scroll a few times so lazy-loaded cards appear
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(350)
        for selector in store["cards"]:
            try:
                await page.wait_for_selector(selector, timeout=6000)
                chosen["cards"] = selector
                return
            except PWTimeout:
                continue
        raise RuntimeError("no result cards found")

    try:
        try:
            await with_retries(attempt, attempts=2, label=name)
        except Exception:
            blocked = await looks_blocked(page)
            await dump_debug(page, name)
            if blocked:
                return [], f"{name}: blocked by a bot check (try PRODIEYY_HEADLESS=0)"
            return [], f"{name}: no products found (layout changed or no results)"

        cards = page.locator(chosen["cards"])
        total = min(await cards.count(), MAX_CARDS_PER_STORE)
        readable = 0
        sem = asyncio.Semaphore(8)

        async def read_card(index):
            nonlocal readable
            async with sem:
                card = cards.nth(index)
                try:
                    title = await resolve_field(
                        card, store, "title", lambda v: len(v) >= 4,
                        heuristic_title, stats,
                    )
                    link = await resolve_field(
                        card, store, "link",
                        lambda v: normalise_url(v, search_url, store["domain"]) is not None,
                        heuristic_link, stats,
                    )
                    url = normalise_url(link, search_url, store["domain"])
                    if not title or not url:
                        return None
                    readable += 1

                    price_text = await resolve_field(
                        card, store, "price",
                        lambda v: extract_price(v, allow_bare=True) is not None,
                        heuristic_price, stats,
                    )
                    price = extract_price(price_text, allow_bare=True)

                    # Unknown prices are never treated as "within budget".
                    if budget is not None and (price is None or price > budget):
                        return None

                    rating_text = await resolve_field(
                        card, store, "rating",
                        lambda v: parse_rating(v, loose=True) is not None,
                        heuristic_rating, stats,
                    )
                    reviews_text = await resolve_field(
                        card, store, "reviews",
                        lambda v: parse_review_count(v, loose=True) is not None,
                        heuristic_reviews, stats,
                    )
                    image = await resolve_field(
                        card, store, "image",
                        lambda v: v.startswith(("http://", "https://")),
                    )

                    try:
                        card_text = clean_space(await card.inner_text(timeout=2000))[:1200]
                    except Exception:
                        card_text = ""

                    return {
                        "title": title,
                        "url": url,
                        "price": price,
                        "price_text": f"₹{price:,.0f}" if price is not None else "Price not verified",
                        "rating": parse_rating(rating_text, loose=True),
                        "reviews": parse_review_count(reviews_text, loose=True),
                        "image": image,
                        "merchant": name,
                        "card_text": card_text,
                    }
                except Exception as exc:
                    logger.debug("Skipping unreadable %s card: %s", name, exc)
                    return None

        results = await asyncio.gather(*(read_card(i) for i in range(total)))
        items = [r for r in results if r]

        if total > 0 and readable == 0:
            await dump_debug(page, name)
            return [], f"{name}: found result cards but could not read them (layout changed)"

        note = f"{name}: {len(items)} usable of {total} listings"
        if stats["healed"]:
            note += f" (selectors auto-adjusted for {', '.join(sorted(stats['healed']))})"
        return items, note
    finally:
        await page.close()


async def inspect_product_page(context, item):
    """Visit a product page for extra evidence. Failures are non-fatal."""
    page = await context.new_page()
    try:
        await page.goto(item["url"], wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(300)
        if await looks_blocked(page):
            return
        try:
            body = (await page.locator("body").inner_text(timeout=4000))[:8000]
        except Exception:
            body = ""

        head = body[:4000]
        if item["rating"] is None:
            item["rating"] = parse_rating(head)
        if item["reviews"] is None:
            item["reviews"] = parse_review_count(head)
        item["page_evidence"] = body

        if item["rating"] is None or item["reviews"] is None or item["price"] is None:
            raw_blocks = await page.locator(
                'script[type="application/ld+json"]'
            ).all_inner_texts()
            for raw in raw_blocks[:8]:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                nodes = data if isinstance(data, list) else [data]
                for node in nodes:
                    if not isinstance(node, dict) or node.get("@type") != "Product":
                        continue
                    agg = node.get("aggregateRating")
                    if isinstance(agg, dict):
                        if item["rating"] is None:
                            try:
                                item["rating"] = float(agg.get("ratingValue"))
                            except (TypeError, ValueError):
                                pass
                        if item["reviews"] is None:
                            try:
                                item["reviews"] = int(
                                    agg.get("reviewCount") or agg.get("ratingCount")
                                )
                            except (TypeError, ValueError):
                                pass
    except Exception as exc:
        logger.info("Product page unavailable (%s): %s", item["merchant"], exc)
    finally:
        await page.close()


async def enrich(context, items):
    sem = asyncio.Semaphore(5)

    async def work(item):
        async with sem:
            await inspect_product_page(context, item)

    await asyncio.gather(*(work(i) for i in items), return_exceptions=True)


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
def score_product(item, requested, core_keywords, budget):
    head = (item.get("title", "") + " " + item.get("card_text", "")).lower()
    evidence = head + " " + item.get("page_evidence", "").lower()

    score = 0.0
    reasons = []
    missing = []

    for feature in requested:
        label = feature.replace("_", " ")
        if any(re.search(p, evidence) for p in FEATURES[feature]):
            score += 12
            reasons.append(f"listing mentions {label}")
        else:
            missing.append(label)

    item["on_topic"] = True
    if core_keywords:
        matched = [k for k in core_keywords if re.search(r"\b" + re.escape(k), head)]
        score += 25 * len(matched) / len(core_keywords)
        if matched:
            reasons.append("matches " + ", ".join(matched[:5]))
        else:
            score -= 30
            item["on_topic"] = False

    rating = item.get("rating")
    count = item.get("reviews")
    if rating is not None:
        r = min(5.0, max(0.0, rating))
        v = count or 0
        # Bayesian average: a 5.0 from 3 ratings is pulled toward the 4.0 prior,
        # while 4.5 from thousands of ratings stays close to 4.5.
        prior_weight, prior_mean = 100, 4.0
        weighted = (v / (v + prior_weight)) * r + (prior_weight / (v + prior_weight)) * prior_mean
        item["weighted_rating"] = round(weighted, 2)
        score += weighted * 12
        if rating >= 4.3:
            reasons.append(f"strong rating {rating:.1f}/5")
        elif rating >= 4.0:
            reasons.append(f"good rating {rating:.1f}/5")
        elif rating < 3.8:
            score -= 15
            missing.append("rating is below 3.8")
    else:
        item["weighted_rating"] = None
        missing.append("rating")

    if count is not None:
        score += min(10.0, 2.5 * math.log10(count + 1))
        reasons.append(f"{count:,} ratings")
        if count < 50:
            missing.append("enough ratings (very few)")
    else:
        missing.append("rating count")

    price = item.get("price")
    if price is not None:
        if budget is not None:
            if price <= budget:
                score += 10
                reasons.append("within budget")
            else:
                score -= 100
        else:
            score += 3

    item["match_score"] = round(score, 1)
    item["match_reasons"] = reasons
    item["missing_evidence"] = list(dict.fromkeys(missing))
    return item


def build_snippet(item):
    parts = [item["label"]] if item.get("label") else []
    parts.append(item["merchant"])
    if item["match_reasons"]:
        parts.append("Why: " + "; ".join(item["match_reasons"][:5]))
    if item["missing_evidence"]:
        parts.append("Not verified: " + ", ".join(item["missing_evidence"][:4]))
    return " · ".join(parts)


def dedupe(items):
    seen, unique = set(), []
    for item in items:
        decoded = item["url"].replace("%2F", "/")
        asin = re.search(r"/dp/([A-Z0-9]{10})", decoded)
        key = (
            "amazon:" + asin.group(1) if asin
            else item["url"].split("?")[0].rstrip("/").lower()
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


PUBLIC_FIELDS = (
    "title", "url", "price", "price_text", "rating", "reviews",
    "image", "merchant", "snippet",
)


async def search_products(query):
    budget = extract_budget(query)
    cleaned = clean_query(query) or query.strip()
    requested, core_keywords = extract_preferences(query)
    notes = []
    items = []

    try:
        async with async_playwright() as p:
            browser, context = await launch(p)
            try:
                outcomes = await asyncio.gather(
                    *(scrape_store(context, s, cleaned, budget) for s in STORES),
                    return_exceptions=True,
                )
                for store, outcome in zip(STORES, outcomes):
                    if isinstance(outcome, Exception):
                        logger.warning("%s failed: %r", store["name"], outcome)
                        notes.append(f"{store['name']}: failed ({type(outcome).__name__})")
                        continue
                    got, note = outcome
                    items.extend(got)
                    notes.append(note)

                unique = dedupe(items)
                for item in unique:
                    score_product(item, requested, core_keywords, budget)
                unique.sort(key=lambda i: i["match_score"], reverse=True)
                top = unique[:PAGES_TO_INSPECT]
                # Product pages are only worth visiting when the user asked for
                # specific features, or the card was missing rating/review data.
                to_inspect = top if requested else [
                    i for i in top if i["rating"] is None or i["reviews"] is None
                ]
                if to_inspect:
                    await enrich(context, to_inspect)
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("Product research failed")
        return {
            "reply": f"Product research failed ({type(exc).__name__}). Check the terminal for details.",
            "results": [],
        }

    ranked = [score_product(i, requested, core_keywords, budget) for i in unique]
    ranked.sort(
        key=lambda i: (i["match_score"], i.get("reviews") or 0), reverse=True
    )
    def qualifies(i, min_rating, min_reviews):
        return (
            i.get("on_topic", True)
            and i.get("rating") is not None
            and i["rating"] >= min_rating
            and (i.get("reviews") or 0) >= min_reviews
        )

    # Only well-reviewed products are recommended: first a strict tier, then a
    # relaxed one if the strict tier has too few results.
    top_tier = [i for i in ranked if qualifies(i, 4.0, 100)]
    mid_tier = [i for i in ranked if qualifies(i, 3.8, 20)]
    strong = top_tier if len(top_tier) >= 5 else mid_tier
    recommendations = (strong or ranked[:5])[:MAX_RECOMMENDATIONS]

    if strong and recommendations:
        labels = {id(recommendations[0]): "Best overall"}
        most = max(recommendations, key=lambda i: i.get("reviews") or 0)
        if (most.get("reviews") or 0) > 0:
            labels.setdefault(id(most), "Most reviewed")
        priced = [
            i for i in recommendations
            if i.get("price") and (i.get("weighted_rating") or 0) >= 4.0
        ]
        if priced:
            labels.setdefault(id(min(priced, key=lambda i: i["price"])), "Best value")
        for i in recommendations:
            i["label"] = labels.get(id(i))

    for item in recommendations:
        item["snippet"] = build_snippet(item)

    notes_text = " | ".join(notes)

    if not recommendations:
        return {
            "reply": (
                "I couldn't verify any matching products"
                + (f" under ₹{budget:,}" if budget is not None else "")
                + ". " + notes_text + ". No products were invented."
            ),
            "results": [],
        }

    reply = (
        f"Here are the top {len(recommendations)} picks, ranked by rating quality "
        "(rating weighted by how many people rated it), then by how well they match your request. "
    )
    if budget is not None:
        reply += f"Listings above ₹{budget:,} or without a readable price were excluded. "
    if not strong:
        reply += "No product met the good-review bar (4.0+ stars with 100+ ratings), so these are the closest. "
    reply += (
        "Ratings come from the retailer listing; review text isn't analysed, "
        "so check the listing before buying. [" + notes_text + "]"
    )

    return {
        "reply": reply,
        "results": [{k: i.get(k) for k in PUBLIC_FIELDS} for i in recommendations],
    }


# --------------------------------------------------------------------------
# Tickets (not automated yet) 
# --------------------------------------------------------------------------
def ticket_reply():
    return {
        "reply": (
            "Ticket booking and cancelling isn't automated yet. Train and movie "
            "booking involve logins, OTPs, captchas and payment, so I won't "
            "attempt that blindly. Here are the official sites to use for now."
        ),
        "results": [
            {
                "title": "IRCTC (train tickets)",
                "url": "https://www.irctc.co.in/",
                "snippet": "Official Indian Railways booking and cancellation.",
            },
            {
                "title": "BookMyShow (movies)",
                "url": "https://in.bookmyshow.com/",
                "snippet": "Movie and event tickets with seat selection.",
            },
        ],
    }


# --------------------------------------------------------------------------
# General web search (Brave API if key set, else DuckDuckGo, then Bing)
# --------------------------------------------------------------------------
def _strip_tags(text):
    return clean_space(re.sub(r"<[^>]+>", "", text or ""))


def normalise_general_url(url):
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower()
    if any(h in host for h in ("duckduckgo.", "bing.com", "google.")):
        return None
    return url


def decode_ddg_url(href):
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or None
    return href


def decode_bing_url(href):
    try:
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            raw += "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
    except Exception:
        pass
    return href


def _brave_sync(query, key):
    req = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?"
        + urlencode({"q": query, "count": 10, "country": "in"}),
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = []
    for row in data.get("web", {}).get("results", []):
        url = normalise_general_url(row.get("url"))
        if url:
            results.append({
                "title": _strip_tags(row.get("title", "")) or url,
                "url": url,
                "snippet": _strip_tags(row.get("description", ""))[:400],
            })
    return results


async def _engine_ddg(context, query):
    page = await context.new_page()
    try:
        await page.goto(
            "https://html.duckduckgo.com/html/?q=" + quote_plus(query) + "&kl=in-en",
            wait_until="domcontentloaded", timeout=30000,
        )
        await page.wait_for_selector("div.result", timeout=8000)
        rows = await page.evaluate(
            """() => [...document.querySelectorAll('div.result')].slice(0, 12).map(r => {
                const a = r.querySelector('a.result__a');
                const s = r.querySelector('.result__snippet');
                return {title: a ? a.innerText : '',
                        href: a ? a.getAttribute('href') : '',
                        snippet: s ? s.innerText : ''};
            })"""
        )
    finally:
        await page.close()
    return [(r["title"], decode_ddg_url(r["href"]), r["snippet"]) for r in rows]


async def _engine_bing(context, query):
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.bing.com/search?setlang=en-IN&cc=IN&q=" + quote_plus(query),
            wait_until="domcontentloaded", timeout=30000,
        )
        await page.wait_for_selector("li.b_algo", timeout=8000)
        rows = await page.evaluate(
            """() => [...document.querySelectorAll('li.b_algo')].slice(0, 12).map(r => {
                const a = r.querySelector('h2 a');
                const s = r.querySelector('.b_caption p') || r.querySelector('p');
                return {title: a ? a.innerText : '',
                        href: a ? a.href : '',
                        snippet: s ? s.innerText : ''};
            })"""
        )
    finally:
        await page.close()
    return [(r["title"], decode_bing_url(r["href"]), r["snippet"]) for r in rows]


async def web_search(query):
    results, source = [], ""

    key = os.getenv("BRAVE_API_KEY")
    if key:
        try:
            results = await asyncio.to_thread(_brave_sync, query, key)
            source = "Brave"
        except Exception as exc:
            logger.warning("Brave search failed: %s", exc)

    if not results:
        try:
            async with async_playwright() as p:
                browser, context = await launch(p)
                try:
                    for name, engine in (("DuckDuckGo", _engine_ddg), ("Bing", _engine_bing)):
                        try:
                            rows = await with_retries(
                                lambda i, e=engine: e(context, query),
                                attempts=2, label=name,
                            )
                        except Exception as exc:
                            logger.warning("%s search failed: %s", name, exc)
                            continue
                        for title, href, snippet in rows:
                            url = normalise_general_url(href)
                            if title and url:
                                results.append({
                                    "title": clean_space(title),
                                    "url": url,
                                    "snippet": clean_space(snippet)[:400],
                                })
                        if results:
                            source = name
                            break
                finally:
                    await browser.close()
        except Exception as exc:
            return {"reply": f"Web search failed: {type(exc).__name__}.", "results": []}

    if not results:
        try:
            from api_search import LAST_ERROR
        except Exception:
            LAST_ERROR = ""
        why = f" Search API problem: {LAST_ERROR}." if LAST_ERROR else ""
        return {
            "reply": "No readable web results were found. The search engines may be blocking automation right now." + why,
            "results": [],
        }
    return {"reply": f"Found {len(results)} web results via {source}.", "results": results[:10]}


async def search_web(query):
    """Route: booking/maps -> places -> products -> general web. API first, Playwright as fallback."""
    # Imported here to avoid circular imports (these modules import this one).
    try:
        from booking import route_booking
    except ImportError:
        # Never hide this: a missing module (e.g. trains.py) silently disabled ALL booking before.
        logger.exception("booking.py failed to import, so booking/hotel search is OFF")
        route_booking = None
    from place import PLACE_RE, STAY_RE, search_places
    from api_search import api_product_search, api_web_search

    query = (query or "").strip()
    if not query:
        return {"reply": "Please enter your question or product preferences.", "results": []}

    # Trip planning comes first: "under Rs 1,500" would otherwise look like a shopping search.
    from trip import is_trip_query, plan_trip
    if is_trip_query(query):
        return await plan_trip(query)

    from transport import is_transport_query, suggest_transport
    if is_transport_query(query):
        return await suggest_transport(query)

    if route_booking:
        resp = await route_booking(query)
        if resp is not None:
            return resp

    if is_ticket_query(query):
        return ticket_reply()

    if PLACE_RE.search(query) and not STAY_RE.search(query):
        return await search_places(query)

    if is_product_search(query):
        budget = extract_budget(query)
        fast = await api_product_search(clean_query(query) or query, budget)
        return fast or await search_products(query)

    return await api_web_search(query) or await web_search(query)


# --------------------------------------------------------------------------
# Inspect a URL
# --------------------------------------------------------------------------
def _is_private_host(host):
    host = (host or "").lower().split(":")[0]
    if host in ("localhost", "") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


async def inspect_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"reply": "Enter a valid URL starting with http:// or https://.", "results": []}
    if _is_private_host(parsed.hostname):
        return {"reply": "Local and private addresses can't be inspected.", "results": []}

    try:
        async with async_playwright() as p:
            browser, context = await launch(p)
            try:
                page = await context.new_page()

                async def attempt(i):
                    await page.goto(
                        url,
                        wait_until="domcontentloaded" if i == 0 else "load",
                        timeout=40000,
                    )

                await with_retries(attempt, attempts=2, label="inspect")
                title = await page.title()
                try:
                    body = (await page.locator("body").inner_text(timeout=8000))[:2500]
                except Exception:
                    body = "No readable page text was found."
                blocked = await looks_blocked(page)
            finally:
                await browser.close()

        reply = f"Website: {title}\n\nPage preview:\n{body}"
        if blocked:
            reply = "This site showed a bot check, so the preview may not be the real page.\n\n" + reply
        return {
            "reply": reply,
            "results": [{"title": title or url, "url": url, "snippet": clean_space(body)[:400]}],
        }
    except Exception as exc:
        return {
            "reply": (
                f"Could not inspect the website: {type(exc).__name__}. "
                "The site may block automated access."
            ),
            "results": [],
        }
