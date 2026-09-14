"""
iSpace.ge product monitor -> Telegram notifications.

Watches one or more category pages on ispace.ge (grouped into labeled
sections, e.g. "Ipad" / "Open Box") for:
  - a brand-new product appearing
  - a product switching from "out of stock" (Notify me) to "in stock" (Add to cart)
  - a product disappearing from the list

Behavior:
  - FIRST RUN: no prior snapshot exists, so there's nothing to diff against.
    Instead of staying silent, it sends you the full current listing for
    each watched section, so you know exactly what's live right now.
  - EVERY RUN AFTER: only sends what actually changed. Each individual
    change is sent as its own message, with a photo when one is available
    (Telegram fetches the image directly from the CDN URL, so this adds
    no real overhead), and falls back to plain text if no image is found
    or if Telegram can't fetch it.

Run this on a schedule (cron / Render Cron Job / GitHub Actions schedule).
Each run is stateless except for the JSON snapshot file it reads/writes.

SETUP
-----
1. pip install -r requirements.txt
2. Set environment variables (or edit the CONFIG block below):
     TELEGRAM_BOT_TOKEN  - token from @BotFather
     TELEGRAM_CHAT_ID    - your chat id
     WATCH_URLS          - comma-separated list of category URLs to watch
3. Run once manually first: python monitor.py  (sends the full listing)
4. Schedule it (see README.md).
"""

import os
import re
import sys
import json
import time
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

WATCH_URLS = [
    u.strip()
    for u in os.environ.get(
        "WATCH_URLS",
        "https://ispace.ge/en/category/ipad,https://ispace.ge/en/category/open-box",
    ).split(",")
    if u.strip()
]

SNAPSHOT_FILE = Path(os.environ.get("SNAPSHOT_FILE", "snapshot.json"))

# Send a product photo for individual change notifications (new listing /
# back in stock). Telegram fetches the image straight from its URL, so
# this doesn't require downloading anything ourselves. Set to "0" to
# disable and always use plain text instead.
SEND_IMAGES = os.environ.get("SEND_IMAGES", "1") != "0"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 20
# Button texts on a product tile that mean "this can be ordered right now".
# ispace.ge has used "Add to cart" everywhere, and switched Open Box tiles to
# "Check condition" (2026-09). Keep both so a future markup change in either
# direction doesn't silently zero out a whole category again.
IN_STOCK_PHRASES = ("add to cart", "check condition")

# Telegram hard limit is 4096 chars per text message; stay comfortably under it.
TELEGRAM_TEXT_LIMIT = 3500
# Small delay between messages so a first-run dump of many items doesn't
# trip Telegram's rate limiting.
SEND_DELAY_SECONDS = 0.4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ispace-monitor")


def label_for_url(url: str) -> str:
    """Turns .../category/open-box into 'Open Box', .../category/ipad into 'Ipad'."""
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()


# --------------------------------------------------------------------------
# TELEGRAM
# --------------------------------------------------------------------------

def _telegram_post(method: str, data: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured, skipping send. Payload:\n%s", data)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        log.error("Telegram %s failed: %s %s", method, resp.status_code, resp.text)
        return False
    return True


def send_telegram_message(text: str) -> bool:
    return _telegram_post(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def send_telegram_photo(image_url: str, caption: str) -> bool:
    ok = _telegram_post(
        "sendPhoto",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        },
    )
    if not ok:
        log.info("Photo send failed, falling back to text for: %s", caption)
        return send_telegram_message(caption)
    return True


def send_long_text(text: str) -> None:
    """Splits text into <= TELEGRAM_TEXT_LIMIT chunks on line boundaries."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > TELEGRAM_TEXT_LIMIT:
            send_telegram_message(chunk)
            time.sleep(SEND_DELAY_SECONDS)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        send_telegram_message(chunk)


# --------------------------------------------------------------------------
# SCRAPING
# --------------------------------------------------------------------------

def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _extract_image(container) -> str:
    img = container.find("img")
    if not img:
        return ""
    for attr in ("src", "data-src", "data-lazy-src"):
        val = img.get(attr)
        if val and val.startswith("http"):
            return val
    return ""


def parse_products(html: str, page_url: str, label: str) -> dict:
    """
    Returns {product_url: {"name", "available", "price", "image", "source", "label"}}
    keyed by absolute product URL (stable identifier across runs).
    """
    soup = BeautifulSoup(html, "lxml")
    products = {}

    product_links = [a for a in soup.find_all("a", href=True) if "/product/" in a["href"]]

    for link in product_links:
        href = link["href"]
        if href.startswith("/"):
            href = "https://ispace.ge" + href
        elif not href.startswith("http"):
            continue

        container = link
        for _ in range(6):
            parent = container.parent
            if parent is None:
                break
            sibling_links = {
                a.get("href")
                for a in parent.find_all("a", href=True)
                if "/product/" in a.get("href", "")
            }
            if len(sibling_links) > 1:
                # Parent now spans more than one product tile — stop here,
                # keep the tighter container from the previous iteration.
                break
            container = parent

        text = container.get_text(" ", strip=True).replace("\xa0", " ")
        img_alt = link.img.get("alt", "").strip() if link.img else ""
        name = img_alt or link.get_text(strip=True)
        if not name:
            continue

        available = any(phrase in text.lower() for phrase in IN_STOCK_PHRASES)
        not_available = "notify me" in text.lower()
        is_in_stock = available and not not_available
        if not is_in_stock:
            continue  # not interested in "Notify me" (out of stock) items

        # Tiles show up to three "₾" amounts (original price, current
        # discounted price, discount delta) plus a "From X ₾/mon."
        # installment line. Skip the delta ("-610 ₾") and the
        # installment ("From 123 ₾/mon.") and keep the real current price.
        price_matches = [
            m.strip()
            for m in re.findall(r"(?<!-)(?<!From )(\d[\d ]{0,7})\s*₾", text)
        ]
        if len(price_matches) >= 2:
            price = price_matches[1] + " ₾"
        elif price_matches:
            price = price_matches[0] + " ₾"
        else:
            price = ""

        products[href] = {
            "name": name,
            "url": href,
            "price": price,
            "image": _extract_image(container),
            "source": page_url,
            "label": label,
        }

    return products


# --------------------------------------------------------------------------
# SNAPSHOT
# --------------------------------------------------------------------------

def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Snapshot file corrupted, starting fresh.")
    return {}


def save_snapshot(data: dict) -> None:
    SNAPSHOT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# FIRST-RUN DUMP
# --------------------------------------------------------------------------

def send_full_listing(all_products: dict) -> None:
    by_label = {}
    for info in all_products.values():
        by_label.setdefault(info["label"], []).append(info)

    for label, items in by_label.items():
        header = f"📋 <b>{label}</b> — current listing ({len(items)} items):\n"
        lines = []
        for info in items:
            price = f" — {info['price']}" if info["price"] else ""
            lines.append(f'• <a href="{info["url"]}">{info["name"]}</a>{price}')
        send_long_text(header + "\n".join(lines))
        time.sleep(SEND_DELAY_SECONDS)


# --------------------------------------------------------------------------
# DIFF + NOTIFY
# --------------------------------------------------------------------------

def diff_and_notify(old: dict, new: dict) -> int:
    """Sends one message per change (photo when available). Returns count sent."""
    sent = 0

    for url, info in new.items():
        if url in old:
            continue  # already known and still in stock — nothing to say
        label = info["label"]
        price = f"\n{info['price']}" if info["price"] else ""
        caption = (
            f'🆕 <b>[{label}]</b> New / back in stock: '
            f'<a href="{url}">{info["name"]}</a>{price}'
        )
        if SEND_IMAGES and info.get("image"):
            send_telegram_photo(info["image"], caption)
        else:
            send_telegram_message(caption)
        sent += 1
        time.sleep(SEND_DELAY_SECONDS)

    for url, info in old.items():
        if url not in new:
            send_telegram_message(
                f'❌ <b>[{info["label"]}]</b> No longer available: '
                f'<a href="{url}">{info["name"]}</a>'
            )
            sent += 1
            time.sleep(SEND_DELAY_SECONDS)

    return sent


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main() -> int:
    old_snapshot = load_snapshot()
    is_first_run = not old_snapshot
    new_snapshot = dict(old_snapshot)
    any_page_ok = False
    all_new_products = {}
  # URLs whose page we actually re-scraped successfully this run. Only
  # these are allowed to report "item disappeared" — a page that failed
  # to fetch or parse (0 products: dead selector, temporary block, a
  # redesigned tile like Open Box's "Check condition" switch) must never
  # make its previously-known items look like they vanished. Without
  # this guard, a broken page gets stuck: its old items are never
  # refreshed in the snapshot, so every future run keeps re-diffing them
  # against nothing and re-sending "No longer available" for the same
  # still-in-stock items forever.
  succeeded_urls = set()

    for url in WATCH_URLS:
        label = label_for_url(url)
        try:
            html = fetch_page(url)
        except requests.RequestException as e:
            log.error("Fetch failed for %s: %s", url, e)
            continue

        page_products = parse_products(html, url, label)
        if not page_products:
            log.warning("0 products parsed from %s — selectors may need updating, skipping.", url)
            continue

        any_page_ok = True
              succeeded_urls.add(url)
        all_new_products.update(page_products)

        for u, i in old_snapshot.items():
            if i.get("source") == url and u not in page_products:
                new_snapshot.pop(u, None)
        new_snapshot.update(page_products)

    if not any_page_ok:
        log.error("All pages failed to fetch/parse this run — not touching snapshot, not notifying.")
        return 1

    if is_first_run:
        log.info("First run — sending full listing (%d products) and saving baseline.", len(all_new_products))
        send_full_listing(all_new_products)
    else:
        old_for_diff = {u: i for u, i in old_snapshot.items() if i.get("source") in succeeded_urls}
        sent = diff_and_notify(old_for_diff, all_new_products)
        if sent:
            log.info("Sent %d change notification(s).", sent)
        else:
            log.info("No changes.")

    save_snapshot(new_snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
