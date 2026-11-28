#!/usr/bin/env python3
"""Temporary diagnostic: print BBC Learning English page structure only.

This intentionally does NOT print article/transcript text.  It prints tags,
classes and headings so the transcript parser can be written against the
real markup.  Delete this file and its workflow after use.
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
FEEDS = [
    ("6min", "https://podcasts.files.bbci.co.uk/p02pc9tn.rss"),
    ("tews", "https://podcasts.files.bbci.co.uk/p02pc9zn.rss"),
    ("6grammar", "https://podcasts.files.bbci.co.uk/p02pc9wq.rss"),
]
HEADING_RE = re.compile(r"<h([1-4])[^>]*>(.*?)</h\1>", re.I | re.S)
TRANSCRIPT_TAG_RE = re.compile(
    r"<([a-z0-9]+)[^>]*class=\"[^\"]*transcript[^\"]*\"[^>]*>", re.I
)
CLASS_RE = re.compile(r"class=\"([^\"]+)\"", re.I)
TAG_RE = re.compile(r"</?([a-z0-9]+)(?:\s+[^>]*)?>", re.I)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def main() -> int:
    for name, feed_url in FEEDS:
        print(f"===== {name} =====")
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
        if not item.link.startswith(("http://www.bbc.", "https://www.bbc.")):
            print("not a bbc page, skipping")
            continue
        try:
            html_bytes = fetch(item.link)
        except urllib.error.HTTPError as exc:
            print(f"page HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001 - diagnostics
            print(f"page failed: {exc}")
            continue
        html = html_bytes.decode("utf-8", errors="replace")
        lower = html.lower()
        print(f"page_bytes={len(html_bytes)}")
        for needle in ("transcript", "widget-richtext", "<p", "vocabulary", "worksheet"):
            print(f"count[{needle}]={lower.count(needle)}")
        headings = []
        for match in HEADING_RE.finditer(html):
            text = re.sub(r"<[^>]+>", " ", match.group(2))
            text = " ".join(text.split())
            if any(
                key in text.lower()
                for key in ("transcript", "vocabulary", "worksheet", "introduction", "download")
            ):
                headings.append((match.group(1), text[:80], match.group(0)[:160]))
        for level, text, raw in headings:
            print(f"heading h{level}: {text} :: {raw}")
        for match in TRANSCRIPT_TAG_RE.finditer(html):
            print(f"transcript_tag: {match.group(0)[:200]}")
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
