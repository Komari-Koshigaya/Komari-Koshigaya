#!/usr/bin/env python3
"""Push BBC Learning English episodes to WeChat through ServerChan.

Curation strategy
-----------------
* Fetch several BBC Learning English RSS feeds (standard library only).
* Prefer episodes that have never been sent, with the morning slot favouring
  6 Minute English and the evening slot favouring The English We Speak /
  6 Minute Grammar.
* When there is nothing new, rotate through older episodes as a review card.
* Remember what was sent in a small JSON state file so duplicate runs and the
  two daily slots do not spam the same episode.

The state file is kept in ``.state/`` and restored by actions/cache in CI.
Only links and short summaries are sent; full transcripts/audio are not
republished.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Sequence

LOG = logging.getLogger("bbc-push")
USER_AGENT = "bbc-learning-english-wechat/1.0"
BEIJING = timezone(timedelta(hours=8))
STATE_VERSION = 1
MAX_AGE_DAYS = 365
FRESH_DAYS = 90
SENT_RETENTION_DAYS = 180
MAX_STATE_ENTRIES = 500
SUMMARY_LIMIT = 300
MAX_TITLE_LENGTH = 32


@dataclass(frozen=True)
class FeedSpec:
    feed_id: str
    label: str
    urls: tuple[str, ...]


DEFAULT_FEEDS = (
    FeedSpec(
        "6min",
        "6 Minute English",
        (
            "https://podcasts.files.bbci.co.uk/p02pc9tn.rss",
            "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss",
        ),
    ),
    FeedSpec(
        "tews",
        "The English We Speak",
        (
            "https://podcasts.files.bbci.co.uk/p02pc9zn.rss",
            "https://feeds.bbci.co.uk/learningenglish/english/features/the-english-we-speak/rss",
        ),
    ),
    FeedSpec(
        "6grammar",
        "6 Minute Grammar",
        ("https://podcasts.files.bbci.co.uk/p02pc9wq.rss",),
    ),
)


@dataclass(frozen=True)
class Episode:
    feed_id: str
    series: str
    title: str
    link: str
    guid: str
    published_at: datetime | None
    duration_seconds: int | None
    audio_url: str | None
    summary: str


@dataclass(frozen=True)
class Selection:
    episode: Episode
    kind: str  # "new" or "review"
    transcript: str | None = None


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]()#])")
TRANSCRIPT_FEEDS = {
    "6min": "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss",
    "tews": "https://feeds.bbci.co.uk/learningenglish/english/features/the-english-we-speak/rss",
}
TRANSCRIPT_MAX_CHARS = 24000
HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.I | re.S)
BLOCK_RE = re.compile(
    r"<h([1-6])\b[^>]*>(.*?)</h\1>|<p\b[^>]*>(.*?)</p>", re.I | re.S
)
BR_RE = re.compile(r"<br\s*/?>", re.I)
STRONG_RE = re.compile(r"<strong\b[^>]*>(.*?)</strong>", re.I | re.S)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(element: ET.Element, name: str) -> ET.Element | None:
    wanted = name.lower()
    for child in element:
        if _local_name(child.tag) == wanted:
            return child
    return None


def _child_text(element: ET.Element, *names: str) -> str:
    for name in names:
        child = _child(element, name)
        if child is not None:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def strip_html(value: str) -> str:
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value)
    value = CONTROL_RE.sub(" ", value)
    return WS_RE.sub(" ", value).strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def parse_pubdate(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_duration(value: str) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return None


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_rss(data: bytes, spec: FeedSpec) -> tuple[str, list[Episode]]:
    """Parse a BBC RSS document into episodes."""
    root = ET.fromstring(data)
    channel = _child(root, "channel")
    if channel is None:
        channel = root
    series = strip_html(_child_text(channel, "title")) or spec.label
    episodes: list[Episode] = []
    for item in list(channel):
        if _local_name(item.tag) != "item":
            continue
        title = strip_html(_child_text(item, "title"))
        if not title:
            continue
        link = _child_text(item, "link").strip()
        description = _child_text(item, "description", "summary", "encoded")
        summary = truncate(strip_html(description), SUMMARY_LIMIT)
        guid = _child_text(item, "guid").strip()
        enclosure = _child(item, "enclosure")
        audio_url: str | None = None
        if enclosure is not None:
            candidate = enclosure.attrib.get("url", "").strip()
            mime = enclosure.attrib.get("type", "").lower()
            clean = candidate.lower().split("?", 1)[0]
            if candidate and (
                "audio" in mime or clean.endswith((".mp3", ".m4a", ".aac", ".ogg"))
            ):
                audio_url = candidate
        if not link:
            if guid.startswith(("http://", "https://")):
                link = guid
            elif audio_url:
                link = audio_url
        if not link and not audio_url:
            continue
        if not guid:
            guid = hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:32]
        episodes.append(
            Episode(
                feed_id=spec.feed_id,
                series=series,
                title=title,
                link=link,
                guid=guid,
                published_at=parse_pubdate(_child_text(item, "pubdate", "published", "updated")),
                duration_seconds=parse_duration(_child_text(item, "duration")),
                audio_url=audio_url,
                summary=summary,
            )
        )
    return series, episodes


def dedupe_episodes(episodes: Iterable[Episode]) -> list[Episode]:
    by_guid: dict[str, Episode] = {}
    for episode in episodes:
        current = by_guid.get(episode.guid)
        if current is None:
            by_guid[episode.guid] = episode
            continue
        # Keep the copy with the richer description or the first known date.
        if (current.published_at is None and episode.published_at is not None) or (
            len(episode.summary) > len(current.summary)
        ):
            by_guid[episode.guid] = episode
    return list(by_guid.values())


def fetch_bytes(url: str, timeout: float = 20.0,
                retries: int = 2) -> tuple[bytes, int]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.status
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"HTTP fetch failed: {last_error}") from last_error


def gather_episodes(
    specs: Sequence[FeedSpec],
    feed_files: Sequence[tuple[str, Path]],
    timeout: float = 20.0,
    retries: int = 2,
) -> tuple[list[Episode], list[str], list[tuple[str, str, int]]]:
    """Fetch every feed with per-feed tolerance.

    Returns ``(episodes, failures, statuses)`` where statuses are
    ``(label, source, episode_count)`` tuples for logging.
    """
    episodes: list[Episode] = []
    failures: list[str] = []
    statuses: list[tuple[str, str, int]] = []

    if feed_files:
        for name, path in feed_files:
            try:
                data = path.read_bytes()
                _, items = parse_rss(data, FeedSpec(name, name, ()))
            except (OSError, ET.ParseError, ValueError) as exc:
                LOG.warning("feed file %s failed: %s", path, exc)
                failures.append(name)
                continue
            episodes.extend(items)
            statuses.append((name, str(path), len(items)))
        return episodes, failures, statuses

    for spec in specs:
        succeeded = False
        for url in spec.urls:
            try:
                data, status = fetch_bytes(url, timeout, retries)
                _, items = parse_rss(data, spec)
            except (RuntimeError, ET.ParseError, ValueError) as exc:
                LOG.warning("feed %s failed via %s: %s", spec.label, url, exc)
                continue
            if not items:
                LOG.warning("feed %s returned no items via %s", spec.label, url)
                continue
            episodes.extend(items)
            statuses.append((spec.label, f"{url} (HTTP {status})", len(items)))
            succeeded = True
            break
        if not succeeded:
            failures.append(spec.label)
    return episodes, failures, statuses


def empty_state() -> dict:
    return {"version": STATE_VERSION, "sent": {}, "last_push": {}}


def _as_text_map(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("ignoring unreadable state %s: %s", path, exc)
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    state = empty_state()
    state["sent"] = _as_text_map(data.get("sent"))
    state["last_push"] = _as_text_map(data.get("last_push"))
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    state["version"] = STATE_VERSION
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def prune_state(state: dict, now: datetime) -> None:
    cutoff = now - timedelta(days=SENT_RETENTION_DAYS)
    kept: dict[str, str] = {}
    for guid, stamp in state["sent"].items():
        parsed = parse_iso(stamp)
        if parsed is None or parsed >= cutoff:
            kept[guid] = stamp
    if len(kept) > MAX_STATE_ENTRIES:
        def key(item: tuple[str, str]) -> datetime:
            return parse_iso(item[1]) or datetime.min.replace(tzinfo=timezone.utc)

        kept = dict(sorted(kept.items(), key=key, reverse=True)[:MAX_STATE_ENTRIES])
    state["sent"] = kept
    state["last_push"] = {
        key: value
        for key, value in state["last_push"].items()
        if (parse_iso(value) or now) >= now - timedelta(days=30)
    }


def resolve_slot(value: str, now: datetime) -> str:
    if value in ("morning", "evening"):
        return value
    hour = now.astimezone(BEIJING).hour
    return "morning" if hour < 14 else "evening"


def preferred_feed_ids(slot: str, now: datetime) -> tuple[str, ...]:
    if slot == "morning":
        return ("6min", "tews", "6grammar")
    evening = ("tews", "6grammar")
    start = now.astimezone(BEIJING).date().toordinal() % 2
    return (evening[start], evening[1 - start], "6min")


def slot_key(now: datetime, slot: str) -> str:
    return f"{now.astimezone(BEIJING).date().isoformat()}-{slot}"


def is_slot_done(state: dict, now: datetime, slot: str) -> bool:
    return slot_key(now, slot) in state.get("last_push", {})


def _timestamp(episode: Episode) -> float:
    return episode.published_at.timestamp() if episode.published_at else 0.0


def select_episodes(
    episodes: Sequence[Episode],
    state: dict,
    slot: str,
    now: datetime,
    max_items: int = 1,
) -> list[Selection]:
    sent = state.get("sent", {})
    oldest_allowed = now - timedelta(days=MAX_AGE_DAYS)
    future_limit = now + timedelta(days=2)

    candidates: list[Episode] = []
    seen: set[str] = set()
    for episode in episodes:
        if episode.guid in seen:
            continue
        seen.add(episode.guid)
        if episode.published_at is not None:
            if episode.published_at > future_limit:
                LOG.info("skipping future-dated episode: %s", episode.title)
                continue
            if episode.published_at < oldest_allowed:
                continue
        candidates.append(episode)
    if not candidates:
        return []

    preferred = preferred_feed_ids(slot, now)

    def preference_rank(episode: Episode) -> int:
        try:
            return preferred.index(episode.feed_id)
        except ValueError:
            return len(preferred)

    fresh_cutoff = now - timedelta(days=FRESH_DAYS)
    fresh = [
        episode
        for episode in candidates
        if episode.guid not in sent
        and (
            episode.published_at is None
            or episode.published_at >= fresh_cutoff
        )
    ]
    fresh.sort(key=lambda ep: (preference_rank(ep), -_timestamp(ep)))
    if fresh:
        return [Selection(episode, "new") for episode in fresh[:max_items]]

    def review_key(episode: Episode) -> tuple[datetime, float]:
        stamp = parse_iso(sent.get(episode.guid, ""))
        if stamp is None:
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        return stamp, -_timestamp(episode)

    for feed_id in preferred:
        pool = [episode for episode in candidates if episode.feed_id == feed_id]
        if pool:
            pool.sort(key=review_key)
            return [Selection(pool[0], "review")]
    candidates.sort(key=review_key)
    return [Selection(candidates[0], "review")]


def md_escape(value: str) -> str:
    return MD_ESCAPE_RE.sub(r"\\\1", value)


_FEATURE_FEED_CACHE: dict[str, dict[str, str]] = {}


def normalize_title(value: str) -> str:
    """Normalize a title so the podcast and feature feeds can be matched."""
    value = strip_html(value)
    value = re.sub(
        r"^\s*BBC Learning English\s*[-–—]\s*[^/]+\s*/\s*",
        "",
        value,
        flags=re.I,
    )
    if " / " in value:
        value = value.split(" / ", 1)[1]
    value = value.lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def load_feature_feed_map(feed_id: str, timeout: float = 20.0,
                          retries: int = 2) -> dict[str, str]:
    """Map normalized episode titles to their Learning English page URLs."""
    if feed_id in _FEATURE_FEED_CACHE:
        return _FEATURE_FEED_CACHE[feed_id]
    url = TRANSCRIPT_FEEDS.get(feed_id)
    if not url:
        return {}
    data, _ = fetch_bytes(url, timeout, retries)
    _, items = parse_rss(data, FeedSpec(feed_id, feed_id, ()))
    mapping: dict[str, str] = {}
    for item in items:
        title = normalize_title(item.title)
        if title and item.link:
            mapping.setdefault(title, item.link)
    _FEATURE_FEED_CACHE[feed_id] = mapping
    return mapping


def resolve_transcript_url(episode: Episode, timeout: float = 20.0,
                           retries: int = 2) -> str | None:
    try:
        mapping = load_feature_feed_map(episode.feed_id, timeout, retries)
    except (RuntimeError, ET.ParseError, ValueError) as exc:
        LOG.warning("could not load transcript feed for %s: %s",
                    episode.feed_id, exc)
        mapping = {}
    url = mapping.get(normalize_title(episode.title))
    if url:
        return url
    if "learningenglish" in episode.link.lower():
        return episode.link
    return None


def _inline_text(value: str) -> str:
    value = BR_RE.sub("\n", value)
    value = STRONG_RE.sub(
        lambda match: "**" + strip_html(match.group(1)) + "**", value
    )
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value)
    value = CONTROL_RE.sub(" ", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(
        r"^\*\*([A-Za-z][A-Za-z .'-]{0,29})\*\*\s*\n",
        r"**\1:** ",
        text,
        flags=re.M,
    ).strip()


def extract_transcript_html(text: str) -> str | None:
    """Extract Introduction → Next from a BBC Learning English episode page."""
    start: int | None = None
    end: int | None = None
    for match in HEADING_RE.finditer(text):
        heading_text = strip_html(match.group(2)).lower()
        if start is None and heading_text == "introduction":
            start = match.start()
        elif start is not None and heading_text == "next":
            end = match.start()
            break
    if start is None:
        return None
    fragment = text[start:end] if end is not None else text[start: start + 120000]
    blocks: list[str] = []
    for match in BLOCK_RE.finditer(fragment):
        if match.group(1):
            heading_text = strip_html(match.group(2))
            if heading_text:
                blocks.append(f"**{heading_text}**")
        else:
            paragraph = _inline_text(match.group(3))
            if paragraph:
                blocks.append(paragraph)
    content = re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()
    if len(content) < 120:
        return None
    if len(content) > TRANSCRIPT_MAX_CHARS:
        content = content[:TRANSCRIPT_MAX_CHARS].rstrip() + "\n\n…(全文过长，已截断)"
    return content


def fetch_episode_transcript(episode: Episode, timeout: float = 30.0,
                             retries: int = 2) -> str | None:
    url = resolve_transcript_url(episode, timeout, retries)
    if not url:
        return None
    LOG.info("fetching transcript for %s: %s", episode.title, url)
    try:
        data, status = fetch_bytes(url, timeout, retries)
    except RuntimeError as exc:
        LOG.warning("transcript fetch failed for %s: %s", episode.title, exc)
        return None
    text = extract_transcript_html(data.decode("utf-8", errors="replace"))
    if not text:
        LOG.warning("no transcript found at %s (HTTP %s)", url, status)
        return None
    LOG.info("transcript for %s: %d chars", episode.title, len(text))
    return text


def build_message(selections: Sequence[Selection], slot: str,
                  now: datetime,
                  transcript_enabled: bool = False) -> tuple[str, str]:
    slot_label = "早间" if slot == "morning" else "晚间"
    date_label = now.astimezone(BEIJING).strftime("%m/%d")
    kinds = {selection.kind for selection in selections}
    if kinds == {"new"}:
        kind_label = "新节目"
    elif kinds == {"review"}:
        kind_label = "复习"
    else:
        kind_label = "新+复习"
    title = f"BBC英语学习 {date_label} {slot_label}·{kind_label}"
    title = title[:MAX_TITLE_LENGTH]

    blocks: list[str] = []
    for selection in selections:
        episode = selection.episode
        lines = [
            f"## 🎧 {md_escape(episode.series)}",
            f"### {md_escape(episode.title)}",
        ]
        meta: list[str] = []
        if episode.published_at is not None:
            meta.append(
                "📅 " + episode.published_at.astimezone(BEIJING).strftime("%Y-%m-%d")
            )
        if episode.duration_seconds is not None:
            meta.append("⏱ " + format_duration(episode.duration_seconds))
        if meta:
            lines.append(" · ".join(meta))
        if episode.summary:
            lines.append(episode.summary)
        links: list[str] = []
        if episode.audio_url:
            links.append(f"[▶ 收听音频]({episode.audio_url})")
        if episode.link:
            links.append(f"[📄 节目页面]({episode.link})")
        if links:
            lines.append(" · ".join(links))
        if selection.kind == "review":
            lines.append("\n> ♻️ 今天没有新节目，推送一期往期内容复习。")
        if selection.transcript:
            lines.append("#### 📄 Full text\n\n" + selection.transcript)
        elif transcript_enabled:
            if episode.feed_id in TRANSCRIPT_FEEDS:
                lines.append("> ⚠️ 全文抓取失败，请打开节目页面查看。")
            else:
                lines.append("> ℹ️ 该节目暂无网页全文，可打开节目页面收听。")
        blocks.append("\n\n".join(lines))

    body = "\n\n---\n\n".join(blocks)
    body += "\n\n> 💡 先盲听一遍，再看文本跟读，最后复述关键句。"
    return title, body


def resolve_endpoint(sendkey: str) -> str:
    key = sendkey.strip()
    if not key:
        raise ValueError("SERVERCHAN_SENDKEY is required")
    if key.lower().startswith("sctp"):
        match = re.match(r"^sctp(\d+)t", key, re.IGNORECASE)
        if match is None:
            raise ValueError(
                "invalid Server酱³ SendKey format (expected sctp<uid>t...)"
            )
        quoted = urllib.parse.quote(key, safe="")
        return f"https://{match.group(1)}.push.ft07.com/send/{quoted}.send"
    quoted = urllib.parse.quote(key, safe="")
    return f"https://sctapi.ftqq.com/{quoted}.send"


def mask_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}/.../***.send"


def send_serverchan(endpoint: str, title: str, desp: str,
                    timeout: float = 20.0, retries: int = 2) -> dict:
    payload = json.dumps(
        {"title": title, "desp": desp}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise RuntimeError(
                    f"ServerChan rejected the request (HTTP {exc.code})"
                ) from None
            last_error = RuntimeError(f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"ServerChan returned non-JSON (HTTP {status})"
                ) from None
            if isinstance(data, dict) and data.get("code") == 0:
                return data
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("message", "") if isinstance(data, dict) else ""
            raise RuntimeError(
                f"ServerChan error code={code}: {str(message)[:200]}"
            )
        if attempt < retries - 1:
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"ServerChan request failed: {last_error}") from last_error


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(timezone.utc)


def build_specs(value: str | None) -> list[FeedSpec]:
    if not value:
        return list(DEFAULT_FEEDS)
    urls = [part.strip() for part in value.split(",") if part.strip()]
    if not urls:
        return list(DEFAULT_FEEDS)
    return [
        FeedSpec(f"feed{index}", f"Feed {index}", (url,))
        for index, url in enumerate(urls, start=1)
    ]


def parse_feed_files(values: Sequence[str]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise ValueError("--feed-file expects NAME=PATH")
        name, path = value.split("=", 1)
        result.append((name.strip(), Path(path.strip())))
    return result


def _make_stdio_robust() -> None:
    """Avoid UnicodeEncodeError on Windows consoles with a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_stdio_robust()
    parser = argparse.ArgumentParser(
        description="Push BBC Learning English to WeChat via ServerChan."
    )
    parser.add_argument(
        "--slot", choices=("auto", "morning", "evening"),
        default=os.environ.get("PUSH_SLOT", "auto"),
        help="Push slot (default: PUSH_SLOT or auto by Beijing hour).",
    )
    parser.add_argument(
        "--state", type=Path,
        default=Path(os.environ.get("STATE_FILE", ".state/bbc_learning_english.json")),
        help="JSON state file (default: .state/bbc_learning_english.json).",
    )
    parser.add_argument(
        "--feed-file", action="append", default=[], metavar="NAME=PATH",
        help="Read a local RSS file instead of the network (repeatable).",
    )
    parser.add_argument(
        "--feeds",
        help="Comma-separated RSS URLs replacing the built-in BBC feeds.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the message without touching ServerChan or state.",
    )
    parser.add_argument(
        "--check-feeds", action="store_true",
        help="Fetch and report feed status, then exit without pushing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the once-per-slot guard.",
    )
    parser.add_argument(
        "--max-items", type=int, default=1,
        help="Maximum number of new episodes per push (default: 1).",
    )
    parser.add_argument(
        "--now", help="Override the current time (ISO 8601, for tests).",
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0,
        help="HTTP timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--no-transcript", action="store_true",
        help="Do not fetch the episode's full text from BBC Learning English.",
    )
    parser.add_argument(
        "--preview-chars", type=int, default=0,
        help="In --dry-run, print only this many body characters (0 = full).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.max_items < 1:
        parser.error("--max-items must be >= 1")
    if args.preview_chars < 0:
        parser.error("--preview-chars must be >= 0")

    try:
        now = parse_now(args.now)
        feed_files = parse_feed_files(args.feed_file)
    except ValueError as exc:
        parser.error(str(exc))

    slot = resolve_slot(args.slot, now)
    state = load_state(args.state)

    if (
        not args.dry_run
        and not args.check_feeds
        and not args.force
        and is_slot_done(state, now, slot)
    ):
        LOG.info(
            "%s was already pushed at %s; skipping (use --force to override)",
            slot_key(now, slot),
            state["last_push"].get(slot_key(now, slot)),
        )
        return 0

    endpoint: str | None = None
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "")
    if args.dry_run:
        if sendkey.strip():
            try:
                endpoint = resolve_endpoint(sendkey)
            except ValueError:
                endpoint = None
    elif not args.check_feeds:
        try:
            endpoint = resolve_endpoint(sendkey)
        except ValueError as exc:
            LOG.error("%s", exc)
            return 1

    specs = build_specs(args.feeds)
    try:
        episodes, failures, statuses = gather_episodes(
            specs, feed_files, timeout=args.timeout
        )
    except (OSError, ET.ParseError, RuntimeError, ValueError) as exc:
        LOG.error("failed to read feeds: %s", exc)
        return 1

    for label, source, count in statuses:
        LOG.info("%s: %d episodes (%s)", label, count, source)
    if failures:
        LOG.warning("feed failures: %s", ", ".join(failures))
    if not episodes:
        LOG.error("no episodes found from any feed")
        return 1

    if args.check_feeds:
        print(
            json.dumps(
                {
                    "feeds": [
                        {"label": label, "source": source, "episodes": count}
                        for label, source, count in statuses
                    ],
                    "failures": failures,
                    "total_episodes": len(episodes),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    episodes = dedupe_episodes(episodes)
    selections = select_episodes(episodes, state, slot, now, args.max_items)
    if not selections:
        LOG.error("no suitable episodes found")
        return 1

    for selection in selections:
        LOG.info(
            "selected %s: %s (%s, %s)",
            selection.kind,
            selection.episode.title,
            selection.episode.series,
            selection.episode.guid,
        )

    if not args.no_transcript:
        enriched: list[Selection] = []
        for selection in selections:
            transcript = fetch_episode_transcript(
                selection.episode, timeout=args.timeout
            )
            enriched.append(
                Selection(selection.episode, selection.kind, transcript)
            )
        selections = enriched

    title, body = build_message(
        selections, slot, now, transcript_enabled=not args.no_transcript
    )

    if args.dry_run:
        print("=== TITLE ===")
        print(title)
        print("=== BODY ===")
        if args.preview_chars and len(body) > args.preview_chars:
            print(body[: args.preview_chars].rstrip())
            print(f"\n…(preview truncated; total {len(body)} chars)")
        else:
            print(body)
        print("=== ENDPOINT ===")
        print(mask_endpoint(endpoint) if endpoint else "(dry-run, no key needed)")
        if is_slot_done(state, now, slot):
            print(
                "NOTE: this slot is already marked as pushed; "
                "a real run would skip unless --force is used."
            )
        return 0

    assert endpoint is not None
    try:
        send_serverchan(endpoint, title, body, timeout=args.timeout)
    except RuntimeError as exc:
        LOG.error("push failed: %s", exc)
        return 1

    stamp = now.astimezone(BEIJING).isoformat(timespec="seconds")
    for selection in selections:
        state["sent"][selection.episode.guid] = stamp
    state["last_push"][slot_key(now, slot)] = stamp
    prune_state(state, now)
    try:
        save_state(args.state, state)
    except OSError as exc:
        LOG.error("push succeeded but state could not be saved: %s", exc)
        return 1
    LOG.info(
        "pushed %d item(s); state saved to %s", len(selections), args.state
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
