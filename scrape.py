#!/usr/bin/env python3
"""Casino bonus scraper.

Fetches one public comparison page, cuts out the card for each target casino,
and asks an LLM to turn the free-text offer + T&Cs into structured fields.
Run:  python scrape.py
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.gambling.com/uk/online-casinos/bonus"
TARGETS = ["Mr Vegas Casino", "Casumo Casino", "Videoslots Casino"]
# Honest, identifiable UA. Put your repo URL in here before running.
USER_AGENT = "casino-bonus-screening-bot/1.0 (+https://github.com/maejor101/casino-offer-tracker)"
# Any OpenAI-compatible chat endpoint works. Default: Groq (free tier).
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "casinos.json"
PAGE_COPY = ROOT / "docs" / "casinos.json"  # the web page reads this copy
RAW_DIR = ROOT / "data" / "raw"

SYSTEM = (
    "You extract casino welcome-bonus terms from scraped page text. "
    "Use ONLY what the text says. Never guess or fill in typical values: if a field is "
    "not stated, use null. If wagering is phrased as 'wager X times the deposit', use X. "
    "Reply with a single JSON object and nothing else, with exactly these keys:\n"
    '  "welcome_bonus": string, the headline offer, e.g. "100% up to £50 + 11 Free Spins"\n'
    '  "wagering_multiplier": number or null (10 for "10x"; 0 if explicitly no wagering)\n'
    '  "min_deposit": string with currency, e.g. "£10", or null'
)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def check_robots(url: str) -> None:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    r = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code in (401, 403):
        die(f"robots.txt returned {r.status_code}: we are blocked. Switch source.")
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    if not rp.can_fetch(USER_AGENT, url):
        die(f"robots.txt disallows {url} for our user agent. Switch source.")
    print(f"robots.txt OK for {url}")


def fetch_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.encoding = "utf-8"
    blocked = r.status_code in (401, 403, 429, 503) or re.search(
        r"captcha|just a moment|attention required", r.text[:5000], re.I
    )
    if blocked:
        die(f"Source blocked us (HTTP {r.status_code}). Not bypassing. Switch source.")
    r.raise_for_status()
    return r.text


def extract_snippet(soup: BeautifulSoup, name: str) -> str | None:
    """Find the casino logo by its title/alt, climb to the enclosing list item,
    return that card's text (offer + fine print)."""
    for img in soup.find_all("img"):
        label = (img.get("title") or img.get("alt") or "").strip().lower()
        if label != name.lower():
            continue
        node = img
        card = None
        while node.parent is not None:
            node = node.parent
            if node.name == "li":
                card = node
                break
        if card is None:  # fallback: climb until there's enough text
            node = img
            while node.parent is not None and len(node.get_text(" ", strip=True)) < 250:
                node = node.parent
            card = node
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        if card.get("data-offer"):
            text = f"Headline offer: {card['data-offer']} | {text}"
        if len(text) >= 80:
            return text[:3000]
    return None


def extract_fields(name: str, snippet: str) -> dict:
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Casino: {name}\n\nPage text:\n{snippet}"},
            ],
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    if not isinstance(data.get("welcome_bonus"), str):
        raise ValueError(f"bad output: {data}")
    w = data.get("wagering_multiplier")
    if w is not None and not isinstance(w, (int, float)):
        raise ValueError(f"wagering not a number: {data}")
    return data


def main() -> None:
    if not LLM_API_KEY or not MODEL:
        die("Set LLM_API_KEY and LLM_MODEL (see README).")

    check_robots(SOURCE_URL)
    html = fetch_page(SOURCE_URL)
    soup = BeautifulSoup(html, "html.parser")
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    results, failures = [], []
    for name in TARGETS:
        snippet = extract_snippet(soup, name)
        if not snippet:
            failures.append(f"{name}: card not found in HTML (page layout changed?)")
            continue
        (RAW_DIR / f"{re.sub(r'[^a-z0-9]+', '_', name.lower())}.txt").write_text(snippet, encoding="utf-8")
        try:
            f = extract_fields(name, snippet)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: LLM extraction failed: {e}")
            continue

        # Guard against hallucinated numbers: the multiplier must appear in the source text.
        w = f.get("wagering_multiplier")
        needs_review = bool(w) and not re.search(rf"\b{int(w)}\s*(x|times)", snippet, re.I)
        results.append(
            {
                "name": name.replace(" Casino", ""),
                "welcome_bonus": f["welcome_bonus"],
                "wagering_multiplier": w,
                "min_deposit": f.get("min_deposit"),
                "source_url": SOURCE_URL,
                "needs_review": needs_review,
            }
        )
        print(f"OK  {name}: {f}{'  [needs_review]' if needs_review else ''}")

    if failures:
        for msg in failures:
            print(f"FAIL {msg}", file=sys.stderr)
    if len(results) < len(TARGETS):
        die("Not all casinos extracted; leaving existing JSON untouched.")

    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE_URL,
        "model": MODEL,
        "casinos": results,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(OUT, PAGE_COPY)
    print(f"Wrote {OUT} and {PAGE_COPY}")


if __name__ == "__main__":
    main()
