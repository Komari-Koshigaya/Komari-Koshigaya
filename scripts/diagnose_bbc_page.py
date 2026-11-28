#!/usr/bin/env python3
"""Temporary diagnostic: print BBC Learning English page structure only.

This intentionally does NOT print article/transcript text.  It prints tags,
classes, headings and candidate URLs so the transcript parser can be written
against the real markup.  Delete this file and its workflow after use.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push_bbc_learning_english as bbc  # noqa: E402

USER_AGENT = bbc.USER_AGENT
PODCAST_FEEDS = [
    ("6min", "https://podcasts.files.bbci.co.uk/p02pc9tn.rss"),
    ("tews", "https://podcasts.files.bbci.co.uk/p02pc9zn.rss"),
    ("6grammar", "https://podcasts.files.bbci.co.uk/p02pc9wq.rss"),
]
FEATURE_FEEDS = [
    ("6min", "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss"),
    ("tews", "https://feeds.bbci.co.uk/learningenglish/english/features/the-english-we-speak/rss"),
    ("6grammar", "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-grammar/rss"),
]
HEADING_RE = re.compile(r"<h([1-4])[^>]*>(.*?)</h\1>", re.I | re.S)
LINK_RE = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"", re.I)
CLASS_RE = re.compile(r"class=\"([^\"]+)\"", re.I)
TAG_RE = re.compile(r"</?([a-z0-9]+)(?:\s+[^>]*)?>", re.I)
INTERESTING = ("learningenglish", "/features/", "ep-")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def print_page_structure(label: str, url: str) -> None:
    print(f"--- page structure: {label} ---")
    print(f"url={url}")
    try:
        html = fetch(url).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"page HTTP {exc.code}")
        return
    except Exception as exc:  # noqa: BLE001 - diagnostics
        print(f"page failed: {exc}")
        return
    lower = html.lower()
    print(f"page_bytes={len(html)}")
    for needle in ("transcript", "widget-richtext", "<p", "vocabulary", "worksheet"):
        print(f"count[{needle}]={lower.count(needle)}")
    for match in HEADING_RE.finditer(html):
        text = re.sub(r"<[^>]+>", " ", match.group(2))
        text = " ".join(text.split())
        if any(
            key in text.lower()
            for key in ("transcript", "vocabulary", "worksheet", "introduction")
        ):
            print(f"heading h{match.group(1)}: {text[:80]} :: {match.group(0)[:160]}")
    index = lower.find("transcript")
    if index != -1:
        snippet = html[max(0, index - 400): index + 2500]
        tags = [tag.lower() for tag in TAG_RE.findall(snippet)]
        classes = CLASS_RE.findall(snippet)
        print(f"tags_around={tags[:100]}")
        print(f"classes_around={classes[:40]}")
    else:
        print("no 'transcript' substring found")
    print()


def main() -> int:
    print("===== feature feeds =====")
    feature_links: list[str] = []
    for name, feed_url in FEATURE_FEEDS:
        print(f"--- feature feed: {name} ---")
        try:
            data = fetch(feed_url)
            _, items = bbc.parse_rss(data, bbc.FeedSpec(name, name, ()))
        except Exception as exc:  # noqa: BLE001 - diagnostics
            print(f"feed failed: {exc}")
            continue
        print(f"items={len(items)}")
        for item in items[:3]:
            print(f"item_link={item.link}")
            print(f"summary_len={len(item.summary)}")
            if "learningenglish" in item.link:
                feature_links.append(item.link)
        print()

    print("===== podcast programme pages =====")
    for name, feed_url in PODCAST_FEEDS:
        print(f"--- {name} ---")
        try:
            data = fetch(feed_url)
            _, items = bbc.parse_rss(data, bbc.FeedSpec(name, name, ()))
        except Exception as exc:  # noqa: BLE001 - diagnostics
            print(f"feed failed: {exc}")
            continue
        if not items:
            print("no items")
            continue
        item = items[0]
        print(f"title={item.title}")
        print(f"link={item.link}")
        try:
            html = fetch(item.link).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - diagnostics
            print(f"page failed: {exc}")
            continue
        candidates = []
        for href in LINK_RE.findall(html):
            if any(key in href.lower() for key in INTERESTING):
                absolute = urllib.parse.urljoin(item.link, href)
                absolute = absolute.split("?", 1)[0]
                if absolute not in candidates:
                    candidates.append(absolute)
        for candidate in candidates[:20]:
            print(f"candidate={candidate}")
            if candidate not in feature_links and "learningenglish" in candidate:
                feature_links.append(candidate)
        print()

    print("===== learning english pages =====")
    for url in feature_links[:5]:
        print_page_structure("feature", url)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
