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
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push_bbc_learning_english as bbc  # noqa: E402

USER_AGENT = bbc.USER_AGENT
FEATURE_FEEDS = [
    ("6min", "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss"),
    ("tews", "https://feeds.bbci.co.uk/learningenglish/english/features/the-english-we-speak/rss"),
]
HEADING_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.I | re.S)
TRANSCRIPT_ELEMENT_RE = re.compile(
    r"<([a-z0-9]+)([^>]*(?:id|class)=\"[^\"]*transcript[^\"]*\"[^>]*)>", re.I
)
CLASS_OR_ID_RE = re.compile(r"(?:id|class)=\"([^\"]+)\"", re.I)
TAG_ATTR_RE = re.compile(r"<([a-z0-9]+)(\s[^>]*)?>", re.I)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.split())


def print_page_structure(url: str) -> None:
    print(f"--- learning english page ---")
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

    print(f"count[transcript]={lower.count('transcript')}")
    print("--- all headings ---")
    for match in HEADING_RE.finditer(html):
        text = clean_text(match.group(2))[:100]
        print(f"h{match.group(1)}: {text}")

    print("--- elements with transcript in id/class ---")
    count = 0
    for match in TRANSCRIPT_ELEMENT_RE.finditer(html):
        print(f"{match.group(1).lower()} :: {match.group(0)[:240]}")
        count += 1
        if count >= 10:
            break
    if count == 0:
        print("(none)")

    print("--- tag/class sequence around each 'transcript' occurrence ---")
    for idx_match in list(re.finditer(r"transcript", lower))[:8]:
        start = max(0, idx_match.start() - 250)
        snippet = html[start: idx_match.start() + 600]
        tags = []
        for tag_match in TAG_ATTR_RE.finditer(snippet):
            attrs = CLASS_OR_ID_RE.findall(tag_match.group(2) or "")
            tags.append(tag_match.group(1).lower() + (("[" + ",".join(attrs) + "]") if attrs else ""))
        print(f"occurrence@{idx_match.start()}: {tags[:40]}")
    print()


def main() -> int:
    for name, feed_url in FEATURE_FEEDS:
        print(f"===== feature feed: {name} =====")
        try:
            data = fetch(feed_url)
            _, items = bbc.parse_rss(data, bbc.FeedSpec(name, name, ()))
        except Exception as exc:  # noqa: BLE001 - diagnostics
            print(f"feed failed: {exc}")
            continue
        print(f"items={len(items)}")
        for item in items[:2]:
            print(f"title={item.title}")
            print(f"link={item.link}")
            print(f"published={item.published_at}")
            print(f"duration={item.duration_seconds} audio={bool(item.audio_url)}")
            print(f"summary_len={len(item.summary)}")
        if items:
            print_page_structure(items[0].link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
