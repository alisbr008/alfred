"""
Live World Model — Alfred's globe as a real perception feed.

Pulls fresh headlines for 3 themes (AI / Markets / Politics) + ~23 major countries
and renders them as colour-coded pulses on the map plus a scrolling ticker.

Source: Google News RSS — FREE, no API key, effectively unlimited. (We used to use
Tavily here, but the map polls ~26 queries every 30 min, which torched Tavily's
~1,000-searches/MONTH free tier in under a day. RSS has no such ceiling.)

Cheap + polite: results cached 30 min, fetched in parallel, English-forced.
"""
from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except Exception:
    requests = None

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_CACHE: dict = {"t": 0.0, "data": {"feed": [], "markers": []}}
_TTL = 1800   # 30 min

# Thematic pulses — (label, query, colour, [lng, lat] hub)
_THEMES = [
    ("AI",       "artificial intelligence", "#4fd1e0", [-122.4, 37.77]),   # SF
    ("MARKETS",  "stock market",            "#3ad0a6", [-74.0, 40.71]),    # NY
    ("POLITICS", "world politics",          "#e2574b", [-77.04, 38.90]),   # DC
]

# Region colours
_NA, _EU, _ASIA, _ME, _SA, _AF, _OCE = (
    "#5da9ff", "#9b8cff", "#ffce5a", "#ff9f5a", "#3ad0a6", "#e26ad1", "#5ce0c8")
_COUNTRIES = [
    ("United States", "United States", _NA, [-98.0, 39.0]),
    ("Canada", "Canada", _NA, [-106.0, 56.0]),
    ("Mexico", "Mexico", _NA, [-102.5, 23.6]),
    ("United Kingdom", "United Kingdom", _EU, [-1.5, 52.5]),
    ("Germany", "Germany", _EU, [10.4, 51.1]),
    ("France", "France", _EU, [2.2, 46.2]),
    ("Ukraine", "Ukraine", _EU, [31.2, 49.0]),
    ("Russia", "Russia", _EU, [90.0, 61.5]),
    ("China", "China", _ASIA, [104.0, 35.0]),
    ("Japan", "Japan", _ASIA, [138.0, 36.5]),
    ("India", "India", _ASIA, [78.0, 22.0]),
    ("South Korea", "South Korea", _ASIA, [128.0, 36.5]),
    ("Kazakhstan", "Kazakhstan", _ASIA, [66.9, 48.0]),
    ("Saudi Arabia", "Saudi Arabia", _ME, [45.0, 24.0]),
    ("UAE", "United Arab Emirates", _ME, [54.0, 24.0]),
    ("Israel", "Israel", _ME, [34.8, 31.5]),
    ("Turkey", "Turkey", _ME, [35.2, 39.0]),
    ("Brazil", "Brazil", _SA, [-51.0, -10.0]),
    ("Argentina", "Argentina", _SA, [-64.0, -34.0]),
    ("South Africa", "South Africa", _AF, [25.0, -29.0]),
    ("Nigeria", "Nigeria", _AF, [8.0, 9.5]),
    ("Egypt", "Egypt", _AF, [30.8, 26.8]),
    ("Australia", "Australia", _OCE, [134.0, -25.0]),
]


def _headline(query: str) -> str | None:
    """Newest English headline for a query, via Google News RSS."""
    if requests is None:
        return None
    try:
        q = requests.utils.quote(f"{query} when:2d")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers=_UA, timeout=10)
        if r.status_code != 200:
            return None
        for item in re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)[:1]:
            m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
            if m:
                t = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip()
                t = html.unescape(t)                   # RSS sends "S&amp;P" — the UI escapes again
                t = re.sub(r"\s+-\s+[^-]+$", "", t)     # drop " - Source" suffix
                return t[:160]
    except Exception:
        pass
    return None


def world_feed(force: bool = False) -> dict:
    """{feed:[{cat,text,color}], markers:[{label,lng,lat,color}]}, cached 30 min."""
    if not force and time.time() - _CACHE["t"] < _TTL and _CACHE["data"]["feed"]:
        return _CACHE["data"]

    jobs = [("theme", *t) for t in _THEMES] + [("country", *c) for c in _COUNTRIES]

    def _one(job):
        _kind, label, query, color, lnglat = job
        return label, color, lnglat, _headline(query)

    feed, markers = [], []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for label, color, lnglat, headline in ex.map(_one, jobs):
            if headline:
                feed.append({"cat": label, "text": headline, "color": color})
                markers.append({"label": label, "lng": lnglat[0], "lat": lnglat[1], "color": color})

    if feed:
        _CACHE.update({"t": time.time(), "data": {"feed": feed, "markers": markers}})
    return _CACHE["data"]
