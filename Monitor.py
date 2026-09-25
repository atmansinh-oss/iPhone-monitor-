#!/usr/bin/env python3
"""
Apple Store pickup monitor - iPhone 18 Pro Max Burgundy, NY/NJ area.
Checks Apple's in-store pickup availability every ~2 minutes and sends a push
notification (ntfy app) the moment a nearby store has stock. Standard library only (no installs).
"""
import os, re, json, time, random, datetime as dt
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---- What to watch (unlocked models, Apple.com product page slugs) ----
MODELS = {
    "iPhone 18 Pro Max 256GB Burgundy": "6.9-inch-display-256gb-burgundy-unlocked",
    "iPhone 18 Pro Max 512GB Burgundy": "6.9-inch-display-512gb-burgundy-unlocked",
}
if os.getenv("INCLUDE_1TB", "false").lower() == "true":
    MODELS["iPhone 18 Pro Max 1TB Burgundy"] = "6.9-inch-display-1tb-burgundy-unlocked"

KNOWN_PARTS = {"6.9-inch-display-256gb-burgundy-unlocked": "MJW64LL/A"}
PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-18-pro/{slug}"
FULFILL_URL = "https://www.apple.com/shop/fulfillment-messages"

# ---- Search area: Newport/Jersey City + Midtown + Short Hills ----
SEARCH_ZIPS = [z.strip() for z in os.getenv("SEARCH_ZIPS", "07310,10001,07078").split(",") if z.strip()]
MAX_MILES = float(os.getenv("MAX_MILES", "25"))          # ~1 hour travel from Newport
INTERVAL = int(os.getenv("CHECK_EVERY_SECONDS", "120"))   # polite polling
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "52"))         # one GitHub run, then the next takes over

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S ET}] {msg}", flush=True)


def http_get(url, referer=None, timeout=20):
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    with urlopen(Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def resolve_part(slug):
    """Find the Apple part number (e.g. MJW64LL/A) from the product page."""
    override = os.getenv("PART_" + re.sub(r"[^A-Z0-9]", "_", slug.upper()))
    if override:
        return override
    try:
        html = http_get(PRODUCT_URL.format(slug=slug))
        m = re.search(r'"sku"\s*:\s*"(M[A-Z0-9]{4}LL/A)', html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"Could not load product page for {slug}: {e}")
    return KNOWN_PARTS.get(slug)


def parse_stores(data, part):
    """Return (stores_seen, available_list) from a fulfillment-messages response."""
    stores = data["body"]["content"]["pickupMessage"].get("stores", [])
    hits = []
    for s in stores:
        try:
            dist = float(s.get("storedistance") or re.sub(r"[^\d.]", "", s.get("storeDistanceWithUnit", "999")) or 999)
        except ValueError:
            dist = 999.0
        pa = (s.get("partsAvailability") or {}).get(part) or {}
        if pa.get("pickupDisplay") == "available" and dist <= MAX_MILES:
            addr = s.get("address") or {}
            hits.append({
                "store": s.get("storeName", "Apple Store"),
                "store_id": s.get("storeNumber", s.get("storeName")),
                "miles": dist,
                "quote": pa.get("pickupSearchQuote") or pa.get("storePickupQuote") or "Available for pickup",
                "address": ", ".join(x for x in [addr.get("address2"), s.get("city"), s.get("state")] if x),
            })
    return len(stores), hits


def check(part, zipcode, slug):
    q = urlencode({"fae": "true", "pl": "true", "mts.0": "regular", "parts.0": part, "location": zipcode})
    raw = http_get(f"{FULFILL_URL}?{q}", referer=PRODUCT_URL.format(slug=slug))
    return parse_stores(json.loads(raw), part)


def send_email(subject, body, click=None, urgent=False):
    """Push notification via ntfy (no email, no account). Name kept for simplicity."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        log(f"(NTFY_TOPIC not set) {subject}\n{body}")
        return
    tags = "rotating_light,iphone" if urgent else "iphone"
    title = re.sub(r"[^\x20-\x7E]", "", subject).strip()  # headers must be plain ASCII
    h = {"Title": title, "Priority": "urgent" if urgent else "default", "Tags": tags}
    if click:
        h["Click"] = click
    req = Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=h, method="POST")
    with urlopen(req, timeout=20):
        pass
    log(f"Push sent: {title}")


def main():
    parts = {}
    for label, slug in MODELS.items():
        p = resolve_part(slug)
        if p:
            parts[label] = (p, slug)
            log(f"Watching {label} -> {p}")
        else:
            log(f"WARNING: no part number for {label}; skipping")
    if not parts:
        send_email("⚠️ iPhone monitor: could not find part numbers",
                   "The monitor couldn't read Apple's product pages. Ask Claude to fix the part numbers.")
        return

    start = time.time()
    now_et = dt.datetime.now(ET)
    status_email = os.getenv("SEND_STARTUP", "false").lower() == "true" or now_et.hour == 8 and now_et.minute < 10
    alerted, fail_streak, failure_emailed, first_cycle = set(), 0, False, True

    while time.time() - start < RUN_MINUTES * 60:
        found, ok, total, stores_seen = {}, 0, 0, 0
        for label, (part, slug) in parts.items():
            for z in SEARCH_ZIPS:
                total += 1
                try:
                    n, hits = check(part, z, slug)
                    ok += 1
                    stores_seen += n
                    for h in hits:
                        found[(part, h["store_id"])] = (label, slug, h)
                except Exception as e:
                    log(f"Check failed ({label}, {z}): {e}")
                time.sleep(random.uniform(2, 5))

        fail_streak = 0 if ok else fail_streak + 1
        if fail_streak >= 3 and not failure_emailed:
            send_email("⚠️ iPhone monitor can't reach Apple",
                       "The last 3 rounds of checks all failed (Apple may be blocking the server). "
                       "Check Apple Store app manually for now and tell Claude so it can adjust.", urgent=True)
            failure_emailed = True

        new = {k: v for k, v in found.items() if k not in alerted}
        if new:
            lines, first_store, first_link = [], None, None
            for (label, slug, h) in sorted(new.values(), key=lambda x: x[2]["miles"]):
                first_store = first_store or h["store"]
                first_link = first_link or PRODUCT_URL.format(slug=slug)
                lines.append(f"• {label}\n  {h['store']} ({h['miles']:.1f} mi) — {h['quote']}\n"
                             f"  {h['address']}\n  Buy for pickup: {PRODUCT_URL.format(slug=slug)}\n")
            send_email(f"🚨 IN STOCK: iPhone 18 Pro Max Burgundy — {first_store}",
                       "Available for in-store pickup right now:\n\n" + "\n".join(lines) +
                       "\nFastest path: Apple Store app → Buy → choose 'Pick up' → select this store → Apple Pay.\n"
                       f"Checked at {dt.datetime.now(ET):%a %b %d, %I:%M %p ET}.",
                       click=first_link, urgent=True)
        alerted = set(found.keys()) | (alerted & set(found.keys()))  # re-alert if stock disappears and returns

        if first_cycle and status_email:
            summary = "\n".join(f"• {l}: {p}" for l, (p, _) in parts.items())
            send_email("✅ iPhone monitor is running",
                       f"Watching (in-store pickup, within {MAX_MILES:.0f} mi of {', '.join(SEARCH_ZIPS)}):\n{summary}\n\n"
                       f"This round: {ok}/{total} checks succeeded, {stores_seen} store results read, "
                       f"{len(found)} store(s) with stock.\nChecking about every {INTERVAL//60} minutes, 24x7.")
        first_cycle = False
        log(f"Round done: {ok}/{total} ok, {len(found)} in stock")
        time.sleep(INTERVAL + random.uniform(-15, 15))


if __name__ == "__main__":
    main()
