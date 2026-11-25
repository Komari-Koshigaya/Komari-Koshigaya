#!/usr/bin/env python3
"""Generate an animated contribution-graph snake as a standalone SVG.

The script scrapes the public GitHub contribution calendar, builds a
Sunday-first 7-row grid and walks the grid in a serpentine (snake) order.
Every cell has the same pitch, so the snake's arrival time at cell ``i`` is
exactly ``i / (N - 1)`` of the animation cycle.  Contribution cells fade out
when the snake reaches them and reappear at the end of the loop.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 contribution-snake/1.0"
)
CONTRIBUTIONS_URL = "https://github.com/users/{username}/contributions"
TOTAL_RE = re.compile(
    r"([\d,]+)\s+contributions?\s+in\s+the\s+last\s+year", re.IGNORECASE
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FONT_FAMILY = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
)

THEMES = {
    "light": {
        "empty": "#ebedf0",
        "levels": ("#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"),
        "text": "#57606a",
        "body": "#2da44e",
        "head": "#116329",
        "eye": "#ffffff",
        "outline": "#ffffff",
    },
    "dark": {
        "empty": "#161b22",
        "levels": ("#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"),
        "text": "#8b949e",
        "body": "#39d353",
        "head": "#7ee787",
        "eye": "#0d1117",
        "outline": "#0d1117",
    },
}


@dataclass(frozen=True)
class Calendar:
    levels: dict[date, int]
    first_day: date
    last_day: date
    first_sunday: date
    columns: int
    total: int | None

    @property
    def cell_count(self) -> int:
        return self.columns * 7


@dataclass(frozen=True)
class SnakePlan:
    points: tuple[tuple[float, float], ...]
    order_index: dict[tuple[int, int], int]
    d: str


class _ContributionParser(HTMLParser):
    """Collect contribution cells and their tooltip counts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.levels: dict[date, int] = {}
        self.id_to_date: dict[str, date] = {}
        self.counts: dict[date, int] = {}
        self._tooltip_for: str | None = None
        self._tooltip_parts: list[str] = []
        self._in_tooltip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "td" and "ContributionCalendar-day" in attributes.get("class", ""):
            raw_date = attributes.get("data-date")
            raw_level = attributes.get("data-level")
            if not raw_date or raw_level is None:
                return
            try:
                day = date.fromisoformat(raw_date)
                level = int(raw_level)
            except ValueError:
                return
            if not 0 <= level <= 4:
                return
            self.levels[day] = max(self.levels.get(day, 0), level)
            cell_id = attributes.get("id")
            if cell_id:
                self.id_to_date[cell_id] = day
        elif tag == "tool-tip":
            self._in_tooltip = True
            self._tooltip_for = attributes.get("for")
            self._tooltip_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_tooltip:
            self._tooltip_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "tool-tip" or not self._in_tooltip:
            return
        self._in_tooltip = False
        day = self.id_to_date.get(self._tooltip_for or "")
        if day is None:
            return
        text = " ".join("".join(self._tooltip_parts).split())
        match = re.search(r"([\d,]+)", text)
        count = int(match.group(1).replace(",", "")) if match else 0
        self.counts[day] = count
        self._tooltip_for = None
        self._tooltip_parts = []


def fetch_contributions_html(username: str, timeout: float = 30.0,
                             retries: int = 3) -> str:
    """Fetch the public contribution calendar HTML."""
    url = CONTRIBUTIONS_URL.format(username=urllib.parse.quote(username))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.0 + attempt)
    raise RuntimeError(
        f"failed to fetch contributions for {username!r}: {last_error}"
    ) from last_error


def parse_contributions(text: str) -> tuple[dict[date, int], int | None]:
    """Return ``{date: level}`` and the displayed yearly total (if any)."""
    parser = _ContributionParser()
    parser.feed(text)
    parser.close()
    if not parser.levels:
        raise ValueError(
            "no contribution cells found; GitHub may have changed its HTML"
        )
    total_match = TOTAL_RE.search(text)
    if total_match:
        total = int(total_match.group(1).replace(",", ""))
    elif parser.counts:
        total = sum(parser.counts.values())
    else:
        total = None
    return dict(sorted(parser.levels.items())), total


def build_calendar(levels: dict[date, int],
                   total: int | None) -> Calendar:
    if not levels:
        raise ValueError("cannot build a calendar without contribution days")
    first_day = min(levels)
    last_day = max(levels)
    # Python: Monday=0 ... Sunday=6.  GitHub's grid starts on Sunday.
    first_sunday = first_day - timedelta(days=(first_day.weekday() + 1) % 7)
    columns = (last_day - first_sunday).days // 7 + 1
    return Calendar(
        levels=levels,
        first_day=first_day,
        last_day=last_day,
        first_sunday=first_sunday,
        columns=columns,
        total=total,
    )


def _center(col: int, row: int, cell: int, gap: int,
            left: float, top: float) -> tuple[float, float]:
    pitch = cell + gap
    return left + col * pitch + cell / 2.0, top + row * pitch + cell / 2.0


def build_serpentine(calendar: Calendar, cell: int = 11, gap: int = 3,
                     left: float = 30.0, top: float = 22.0) -> SnakePlan:
    """Walk every grid position column by column, alternating direction."""
    points: list[tuple[float, float]] = []
    order_index: dict[tuple[int, int], int] = {}
    for col in range(calendar.columns):
        rows = range(7) if col % 2 == 0 else range(6, -1, -1)
        for row in rows:
            order_index[(col, row)] = len(points)
            points.append(_center(col, row, cell, gap, left, top))
    path = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
    return SnakePlan(tuple(points), order_index, path)


def _text(x: float, y: float, value: str, color: str, size: int,
          anchor: str = "start") -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" fill="{color}" '
        f'font-family="{FONT_FAMILY}" font-size="{size}" '
        f'text-anchor="{anchor}">{html.escape(value)}</text>'
    )


def build_svg(calendar: Calendar, username: str, theme_name: str,
              duration: float, cell: int = 11, gap: int = 3) -> str:
    """Build one self-contained SVG with SMIL animations."""
    theme = THEMES[theme_name]
    pitch = cell + gap
    left, top, right, bottom = 30.0, 22.0, 12.0, 20.0
    grid_width = (calendar.columns - 1) * pitch + cell
    grid_height = 6 * pitch + cell
    width = left + grid_width + right
    height = top + grid_height + bottom

    plan = build_serpentine(calendar, cell, gap, left, top)
    total_cells = len(plan.points)
    denominator = max(total_cells - 1, 1)
    body_length = max(4.0, min(200.0, 12.0 * 1000.0 / denominator))
    dasharray = f"{body_length:.2f} 1000"
    dashoffset_from = body_length
    dashoffset_to = body_length - 1000.0

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'role="img">'
    )
    parts.append(
        f'<title>{html.escape(username)}\'s contribution snake</title>'
    )
    parts.append(
        "<desc>An animated snake traveling through the GitHub contribution "
        "graph and eating contribution cells.</desc>"
    )

    # Month labels.  Label the first column whose visible date starts a month.
    previous_month: tuple[int, int] | None = None
    for col in range(calendar.columns):
        week_start = calendar.first_sunday + timedelta(weeks=col)
        first_visible = max(week_start, calendar.first_day)
        month_key = (first_visible.year, first_visible.month)
        if month_key == previous_month:
            continue
        previous_month = month_key
        x = left + col * pitch
        parts.append(_text(x, 13, MONTHS[first_visible.month - 1],
                           theme["text"], 10))

    # Weekday labels.
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        y = top + row * pitch + cell * 0.82
        parts.append(_text(left - 6, y, label, theme["text"], 9, "end"))

    # Empty base cells for the whole rectangle.
    for col in range(calendar.columns):
        for row in range(7):
            x = left + col * pitch
            y = top + row * pitch
            parts.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell}" '
                f'height="{cell}" rx="2" ry="2" fill="{theme["empty"]}"/>'
            )

    # Contribution cells, fading out when the snake arrives.
    for day in sorted(calendar.levels):
        level = calendar.levels[day]
        if level <= 0:
            continue
        offset = (day - calendar.first_sunday).days
        col, row = divmod(offset, 7)
        if not 0 <= col < calendar.columns:
            continue
        index = plan.order_index.get((col, row), 0)
        eat = min(max(index / denominator, 0.001), 0.996)
        hold = min(eat + 0.003, 0.998)
        x = left + col * pitch
        y = top + row * pitch
        parts.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell}" '
            f'height="{cell}" rx="2" ry="2" '
            f'fill="{theme["levels"][level]}">'
            f'<animate attributeName="opacity" values="1;1;0;1" '
            f'keyTimes="0;{eat:.4f};{hold:.4f};1" dur="{duration:g}s" '
            f'repeatCount="indefinite"/>'
            f'</rect>'
        )

    # Snake body: a short dash that travels along the whole path.
    dash_animation = (
        f'<animate attributeName="stroke-dashoffset" '
        f'values="{dashoffset_from:.2f};{dashoffset_to:.2f}" '
        f'dur="{duration:g}s" repeatCount="indefinite" calcMode="linear"/>'
    )
    parts.append(
        f'<path d="{plan.d}" fill="none" stroke="{theme["outline"]}" '
        f'stroke-width="{cell * 0.95:.2f}" stroke-linecap="round" '
        f'stroke-linejoin="round" pathLength="1000" '
        f'stroke-dasharray="{dasharray}">{dash_animation}</path>'
    )
    parts.append(
        f'<path d="{plan.d}" fill="none" stroke="{theme["body"]}" '
        f'stroke-width="{cell * 0.68:.2f}" stroke-linecap="round" '
        f'stroke-linejoin="round" pathLength="1000" '
        f'stroke-dasharray="{dasharray}">{dash_animation}</path>'
    )

    # Head.  The outer group keeps a sensible static position if SMIL is
    # stripped; animateMotion uses coordinates relative to that position.
    first_x, first_y = plan.points[0]
    relative_path = "M 0 0" + "".join(
        f" L {x - first_x:.2f} {y - first_y:.2f}" for x, y in plan.points[1:]
    )
    eye_radius = max(1.1, cell * 0.13)
    parts.append(
        f'<g transform="translate({first_x:.2f} {first_y:.2f})"><g>'
        f'<animateMotion path="{relative_path}" dur="{duration:g}s" '
        f'repeatCount="indefinite" rotate="auto" calcMode="linear"/>'
        f'<circle r="{cell * 0.56:.2f}" fill="{theme["head"]}" '
        f'stroke="{theme["outline"]}" stroke-width="1.2"/>'
        f'<circle cx="{cell * 0.22:.2f}" cy="{-cell * 0.20:.2f}" '
        f'r="{eye_radius:.2f}" fill="{theme["eye"]}"/>'
        f'<circle cx="{cell * 0.22:.2f}" cy="{cell * 0.20:.2f}" '
        f'r="{eye_radius:.2f}" fill="{theme["eye"]}"/>'
        f'</g></g>'
    )

    if calendar.total is not None:
        label = f"{calendar.total} contributions in the last year"
        parts.append(_text(left, height - 5, label, theme["text"], 10))

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an animated GitHub contribution snake SVG."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER", "Komari-Koshigaya"),
        help="GitHub username (default: GITHUB_REPOSITORY_OWNER or Komari-Koshigaya)",
    )
    parser.add_argument(
        "--input-html", type=Path,
        help="Read contribution HTML from a local file instead of GitHub.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("dist"),
        help="Directory for snake.svg and snake-dark.svg (default: dist).",
    )
    parser.add_argument(
        "--duration", type=float, default=30.0,
        help="Seconds per animation loop (default: 30).",
    )
    parser.add_argument(
        "--min-days", type=int, default=300,
        help="Refuse to render fewer days than this (default: 300).",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print parsing details.",
    )
    args = parser.parse_args(argv)

    if args.duration <= 0:
        parser.error("--duration must be positive")

    try:
        if args.input_html:
            text = args.input_html.read_text(encoding="utf-8")
        else:
            text = fetch_contributions_html(args.username)
        levels, total = parse_contributions(text)
        if len(levels) < args.min_days:
            raise ValueError(
                f"only {len(levels)} contribution days found; "
                f"expected at least {args.min_days}"
            )
        calendar = build_calendar(levels, total)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.debug:
        active = sum(1 for level in levels.values() if level > 0)
        print(
            f"days={len(levels)} active={active} columns={calendar.columns} "
            f"range={calendar.first_day}..{calendar.last_day} total={total}"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for theme_name, filename in (("light", "snake.svg"),
                                 ("dark", "snake-dark.svg")):
        try:
            svg = build_svg(calendar, args.username, theme_name, args.duration)
            ET.fromstring(svg)
        except (ValueError, ET.ParseError) as exc:
            print(f"error: failed to build {theme_name} SVG: {exc}",
                  file=sys.stderr)
            return 1
        output = args.output_dir / filename
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(svg)
        print(f"wrote {output} ({len(svg):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
