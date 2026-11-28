#!/usr/bin/env python3
"""Temporary diagnostic: print BBC Learning English rich-text structure only.

No article/transcript text is printed. Delete after use.
"""

from __future__ import annotations

import html as html_module
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push_bbc_learning_english as bbc  # noqa: E402

FEED = "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss"
HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.I | re.S)
TAG_ATTR_RE = re.compile(r"<([a-z0-9]+)(\s[^>]*)?>", re.I)
CLASS_OR_ID_RE = re.compile(r"(?:id|class)=\"([^\"]+)\"", re.I)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": bbc.USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def tag_sequence(fragment: str, limit: int = 60) -> list[str]:
    tags = []
    for match in TAG_ATTR_RE.finditer(fragment):
        attrs = CLASS_OR_ID_RE.findall(match.group(2) or "")
        tags.append(
            match.group(1).lower()
            + (("[" + ",".join(attrs) + "]") if attrs else "")
        )
        if len(tags) >= limit:
            break
    return tags


def main() -> int:
    data = fetch(FEED)
    _, items = bbc.parse_rss(data, bbc.FeedSpec("6min", "6min", ()))
    item = items[0]
    print(f"link={item.link}")
    page = fetch(item.link).decode("utf-8", errors="replace")
    lower = page.lower()
    print(f"page_bytes={len(page)}")
    print(f"heading_matches={len(HEADING_RE.findall(page))}")
    for match in HEADING_RE.finditer(page):
        text = re.sub(r"<[^>]+>", " ", match.group(2))
        print(f"heading h{match.group(1)}: {' '.join(text.split())[:80]}")

    rich = lower.find("widget-richtext")
    print(f"richtext_index={rich}")
    if rich == -1:
        return 0
    ends = [
        pos
        for pos in (
            lower.find("download transcript", rich),
            lower.find("widget-pagelink", rich),
            lower.find("widget-download", rich),
        )
        if pos != -1
    ]
    end = min(ends) if ends else rich + 30000
    fragment = page[rich:end]
    print(f"richtext_len={len(fragment)}")
    print(f"richtext_start_tags={tag_sequence(fragment, 80)}")

    for marker in ("Introduction", "Transcript", "Vocabulary"):
        pos = fragment.lower().find(marker.lower())
        print(f"marker[{marker}]={pos}")
        if pos != -1:
            context = fragment[max(0, pos - 250): pos + 450]
            print(f"marker_tags[{marker}]={tag_sequence(context, 40)}")

    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", fragment, re.S | re.I)
    print(f"p_count={len(paragraphs)}")
    for index, paragraph in enumerate(paragraphs[:20]):
        open_tag = re.search(r"<p\b([^>]*)>", paragraph, re.I)
        classes = CLASS_OR_ID_RE.findall(open_tag.group(1) if open_tag else "")
        has_strong = "<strong" in paragraph.lower()
        has_br = "<br" in paragraph.lower()
        text = html_module.unescape(re.sub(r"<[^>]+>", "", paragraph))
        text = " ".join(text.split())
        print(
            f"p[{index}] classes={classes} strong={has_strong} br={has_br} "
            f"text_len={len(text)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
