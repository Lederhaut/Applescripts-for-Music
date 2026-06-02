#!/usr/bin/env python3
"""
chartentry.py -- Chart Entry Date Tagger for macOS Music App

Searches offiziellecharts.de for the chart entry date of a track
and writes it to the file metadata.

Usage:
    python3 chartentry.py --artist "Artist Name" --title "Track Title" --file "/path/to/file.mp3"
"""

import os
import sys
import re
import time
import argparse
import logging
import unicodedata
from urllib.parse import quote_plus

try:
    import requests
    from bs4 import BeautifulSoup
    from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
    from mutagen.mp4 import MP4
except ImportError as e:
    print("[ERROR] Missing dependency: " + str(e), file=sys.stderr)
    print("[ERROR] Please run: pip3 install requests beautifulsoup4 mutagen", file=sys.stderr)
    sys.exit(1)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chartentry.log")

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
log.propagate = False

_log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
log.addHandler(_stdout_handler)

try:
    _file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    _file_handler.setFormatter(_log_formatter)
    log.addHandler(_file_handler)
except Exception as _log_ex:
    print("[WARNING] Could not open log file %s: %s" % (LOG_FILE, _log_ex), file=sys.stderr)

BASE_URL = "https://www.offiziellecharts.de"
# The /suche form supports both "artist_search" and "title_search".
# Sending both narrows the results considerably and improves match precision.
SEARCH_URL = (
    BASE_URL
    + "/suche?artist_search={artist}&title_search={title}&do_search=do"
)
SEARCH_URL_ARTIST_ONLY = BASE_URL + "/suche?artist_search={artist}&do_search=do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.offiziellecharts.de/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3

# Simple in-process cache to avoid duplicate searches in same run
_search_cache = {}


# ---------------------------------------------------------------------------
# String normalization
# ---------------------------------------------------------------------------

def normalize(text):
    """Lowercase, strip diacritics, remove (Remix), (feat. ...), punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # remove things in parentheses like (Remix), (feat. X), [Remastered]
    text = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", text)
    # remove "feat. ..." until end / ampersand
    text = re.sub(r"\bfeat\.?\s+.*$", " ", text)
    text = re.sub(r"\bft\.?\s+.*$", " ", text)
    # remove punctuation
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# HTTP fetch with retries
# ---------------------------------------------------------------------------

def fetch(url):
    """Fetch URL with retries, exponential backoff. Return text or None."""
    if url in _search_cache:
        return _search_cache[url]

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            status = response.status_code
            if 400 <= status < 500:
                log.warning("[WARNING] HTTP %s for %s", status, url)
                return None
            if status >= 500:
                raise requests.exceptions.HTTPError("HTTP {}".format(status))
            response.raise_for_status()
            text = response.text
            if not text or len(text) < 100:
                log.warning("[WARNING] Empty/short response for %s", url)
                return None
            _search_cache[url] = text
            return text
        except requests.exceptions.RequestException as ex:
            log.warning("[WARNING] Attempt %d failed for %s: %s", attempt + 1, url, ex)
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def find_chart_entry_date(html, artist, title):
    """
    Parse search result page for matching title and return tuple
    (chart entry date in YYYY-MM-DD format or None, score int).
    """
    if not html:
        return (None, 0)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.error("[ERROR] HTML parse failed: %s", e)
        return (None, 0)

    norm_artist = normalize(artist)
    norm_title = normalize(title)

    tables = soup.find_all("table", class_=re.compile(r"\bresult-table\b"))
    if not tables:
        # fallback: look for any chart-table
        tables = soup.find_all("table", class_=re.compile(r"\bchart-table\b"))

    candidates = []  # list of (score, date_str, row_text)

    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            # col 0 = result number
            # col 1 = artist + title  (title in <span><a href="/charts/titel-...">Title</a></span>)
            # col 2 = <strong>DD.MM.YYYY</strong>
            info_cell = cells[1]
            date_cell = cells[2]

            # extract title text from anchor preferably containing /titel/ or /charts/titel
            link = None
            for a in info_cell.find_all("a", href=True):
                if "/titel" in a["href"] or "/charts/titel" in a["href"]:
                    link = a
                    break
            if link is None:
                link = info_cell.find("a")

            row_title = link.get_text(strip=True) if link else ""
            # artist text = info_cell text minus title
            full_text = info_cell.get_text(" ", strip=True)
            row_artist = full_text.replace(row_title, "").strip()

            norm_row_title = normalize(row_title)
            norm_row_artist = normalize(row_artist)

            strong = date_cell.find("strong")
            if not strong:
                continue
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", strong.get_text())
            if not m:
                continue
            date_str = "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))

            # scoring
            score = 0
            if norm_row_title == norm_title:
                score += 100
            elif norm_title and norm_title in norm_row_title:
                score += 60
            elif norm_row_title and norm_row_title in norm_title:
                score += 40

            if norm_row_artist and norm_artist:
                if norm_row_artist == norm_artist:
                    score += 50
                elif norm_artist in norm_row_artist or norm_row_artist in norm_artist:
                    score += 25

            candidates.append((score, date_str, row_title, row_artist))

    if not candidates:
        return (None, 0)

    # sort by score desc; if all 0, take first
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    if best[0] <= 0:
        # no real match found
        log.info("[INFO] No good match; taking first row: %s - %s",
                 best[3], best[2])
    else:
        log.info("[INFO] Best match: %s - %s (score=%d)",
                 best[3], best[2], best[0])
    return (best[1], best[0])


# ---------------------------------------------------------------------------
# Search workflow
# ---------------------------------------------------------------------------

def search_chart_entry(artist, title):
    """
    Search offiziellecharts.de and return tuple (YYYY-MM-DD or None, score).

    Strategy:
      1. Query both artist_search AND title_search for higher precision.
      2. If that yields no usable result, fall back to artist-only search
         (broader recall) and re-score the candidates.
    """
    if not artist or not title:
        log.warning("[WARNING] Missing artist/title")
        return (None, 0)

    # 1) precise search: artist + title
    url = SEARCH_URL.format(artist=quote_plus(artist), title=quote_plus(title))
    log.info("[INFO] URL: %s", url)
    html = fetch(url)
    if html is None:
        log.error("[ERROR] Network failure for: %s - %s", artist, title)
        return (None, 0)

    date, score = find_chart_entry_date(html, artist, title)
    if date is not None and score > 0:
        return (date, score)

    # 2) fallback: artist-only search (in case the title differs slightly)
    log.info("[INFO] No good match with title_search; "
             "falling back to artist-only search.")
    url2 = SEARCH_URL_ARTIST_ONLY.format(artist=quote_plus(artist))
    log.info("[INFO] URL: %s", url2)
    html2 = fetch(url2)
    if html2 is None:
        # return whatever the precise search produced (possibly None,0)
        return (date, score)

    date2, score2 = find_chart_entry_date(html2, artist, title)
    # pick the better-scoring result (prefer the precise one on ties)
    if score2 > score and date2 is not None:
        return (date2, score2)
    if date is not None:
        return (date, score)
    return (date2, score2)


# ---------------------------------------------------------------------------
# Tag writing
# ---------------------------------------------------------------------------

def write_tag(filepath, date):
    """
    Write date tag to file. Returns True on success.

    For MP3:
      - If TXXX:RELEASETIME already exists (and is non-empty), the date is
        instead written to TXXX:OFFIZIELLECHARTS_DE_ENTRY.
      - Otherwise it is written to TXXX:RELEASETIME.
    For MP4/M4A:
      - If a release-date / year tag already exists, the date is written to a
        custom freeform atom "OFFIZIELLECHARTS_DE_ENTRY".
      - Otherwise it goes to the standard \xa9day tag.
    """
    lower = filepath.lower()
    try:
        if lower.endswith(".mp3"):
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            existing = tags.getall("TXXX:RELEASETIME")
            existing_has_value = any(
                (str(fr.text[0]).strip() if fr.text else "") for fr in existing
            )

            if existing_has_value:
                tags.delall("TXXX:OFFIZIELLECHARTS_DE_ENTRY")
                tags.add(TXXX(encoding=3,
                              desc="OFFIZIELLECHARTS_DE_ENTRY",
                              text=date))
                tags.save(filepath)
                log.info("[INFO] MP3 RELEASETIME already set; "
                         "wrote OFFIZIELLECHARTS_DE_ENTRY=%s", date)
                return "alt"
            else:
                tags.delall("TXXX:RELEASETIME")
                tags.add(TXXX(encoding=3, desc="RELEASETIME", text=date))
                tags.save(filepath)
                log.info("[INFO] MP3 RELEASETIME tag written: %s", date)
                return True
        elif lower.endswith(".m4a") or lower.endswith(".mp4") or lower.endswith(".m4p"):
            audio = MP4(filepath)
            existing_day = audio.tags.get("\xa9day") if audio.tags else None
            existing_has_value = bool(existing_day) and any(
                str(v).strip() for v in existing_day
            )
            if existing_has_value:
                key = "----:com.apple.iTunes:OFFIZIELLECHARTS_DE_ENTRY"
                audio[key] = [date.encode("utf-8")]
                audio.save()
                log.info("[INFO] M4A \xa9day already set; "
                         "wrote OFFIZIELLECHARTS_DE_ENTRY=%s", date)
                return "alt"
            else:
                audio["\xa9day"] = [date]
                audio.save()
                log.info("[INFO] M4A year tag written: %s", date)
                return True
        else:
            log.warning("[WARNING] Unsupported file type: %s", filepath)
            return False
    except Exception as e:
        log.error("[ERROR] Writing tag failed for %s: %s", filepath, e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(artist, title, filepath):
    log.info("[INFO] Processing: %s - %s", artist, title)
    if not artist or not title:
        log.warning("[WARNING] Skipping: missing metadata")
        print("SCORE=0")
        return "skip"

    date, score = search_chart_entry(artist, title)
    if not date:
        log.info("[INFO] No date found for: %s - %s", artist, title)
        print("SCORE=0")
        return "skip"

    log.info("[INFO] Chart entry date: %s (score=%d)", date, score)

    used_alt_tag = False
    if filepath:
        ok = write_tag(filepath, date)
        if ok == "alt":
            used_alt_tag = True
            ok = True
        if not ok:
            print("SCORE=%d" % score)
            return "fail"

    # also print to stdout for AppleScript to capture
    print("DATE=" + date)
    print("SCORE=%d" % score)
    if used_alt_tag:
        print("ALT_TAG=1")
    return "ok"


def main():
    parser = argparse.ArgumentParser(description="Chart Entry Date Tagger")
    parser.add_argument("--artist", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--file", required=False, default="")
    args = parser.parse_args()

    result = process(args.artist, args.title, args.file)
    sys.exit(0 if result == "ok" else 1)


if __name__ == "__main__":
    main()