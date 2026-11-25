#!/usr/bin/env python3
"""Fill the README's Recent Projects block from public GitHub repositories.

The script fetches ``GET /users/{username}/repos`` (or reads a local JSON
fixture), drops forks, archived/private repos and the profile repository
itself, then writes the five most recently pushed projects between two
markers.  It is designed to run both locally (``--input-json``) and in a
GitHub Actions workflow (``GITHUB_TOKEN``).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

START = "<!-- RECENT-PROJECTS:START -->"
END = "<!-- RECENT-PROJECTS:END -->"
USER_AGENT = "recent-projects-readme/1.0"
API_URL = "https://api.github.com/users/{username}/repos"
MIN_DATE = datetime.min.replace(tzinfo=timezone.utc)
MAX_DESCRIPTION = 120
WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    description: str
    pushed_at: datetime | None


def collapse_ws(value: str) -> str:
    return WS_RE.sub(" ", value).strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def md_text(value: str) -> str:
    value = html.escape(value, quote=False)
    for char in ("\\", "`", "*", "_", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_repos(
    data: Any,
    username: str,
    include_forks: bool = False,
) -> list[Repo]:
    if not isinstance(data, list):
        raise ValueError("GitHub repositories response is not a list")
    repos: list[Repo] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("html_url") or "").strip()
        if not name or not url:
            continue
        if name.lower() == username.lower():
            continue  # the profile repository itself
        if item.get("private") or item.get("archived") or item.get("disabled"):
            continue
        if item.get("fork") and not include_forks:
            continue
        description = collapse_ws(str(item.get("description") or ""))
        pushed = (
            parse_iso(item.get("pushed_at"))
            or parse_iso(item.get("updated_at"))
        )
        repos.append(Repo(name=name, url=url, description=description,
                          pushed_at=pushed))
    return repos


def select_repos(repos: Sequence[Repo], limit: int = 5) -> list[Repo]:
    ordered = sorted(repos, key=lambda repo: repo.name.lower())
    ordered.sort(key=lambda repo: repo.pushed_at or MIN_DATE, reverse=True)
    return ordered[:limit]


def format_repo(repo: Repo) -> str:
    line = f"- [{md_text(repo.name)}]({repo.url})"
    if repo.description:
        line += " — " + html.escape(
            truncate(repo.description, MAX_DESCRIPTION), quote=False
        )
    if repo.pushed_at is not None:
        line += f" <sub>{repo.pushed_at.date().isoformat()}</sub>"
    return line


def build_block(repos: Iterable[Repo]) -> str:
    lines = [format_repo(repo) for repo in repos]
    return "\n".join(lines) if lines else "- No public projects yet."


def replace_block(text: str, block: str, newline: str = "\n") -> str:
    pattern = re.compile(
        re.escape(START) + r".*?" + re.escape(END), re.DOTALL
    )
    if not pattern.search(text):
        raise ValueError(f"README markers not found: {START} ... {END}")
    body = block.strip("\r\n").replace("\r\n", "\n").replace("\n", newline)
    replacement = START + newline + (body + newline if body else "") + END
    return pattern.sub(lambda _match: replacement, text, count=1)


def fetch_repos(
    username: str,
    token: str = "",
    timeout: float = 30.0,
    retries: int = 2,
) -> list[dict[str, Any]]:
    url = API_URL.format(username=urllib.parse.quote(username))
    url += "?per_page=100&sort=pushed&direction=desc&type=owner"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise ValueError("GitHub API returned a non-list payload")
            return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 403:
                raise RuntimeError(
                    "GitHub API rate limit hit; pass GITHUB_TOKEN"
                ) from None
            if 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"failed to fetch repositories: {last_error}") from last_error


def read_readme(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8"), newline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update README's Recent Projects block from GitHub."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER", "Komari-Koshigaya"),
    )
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--input-json", type=Path,
        help="Read a local GitHub repositories JSON array instead of the API.",
    )
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token (default: GITHUB_TOKEN).",
    )
    parser.add_argument("--include-forks", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be >= 1")

    try:
        text, newline = read_readme(args.readme)
        if args.input_json:
            raw = args.input_json.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        else:
            data = fetch_repos(args.username, args.token)
        repos = parse_repos(data, args.username, args.include_forks)
        selected = select_repos(repos, args.limit)
        block = build_block(selected)
        updated = replace_block(text, block, newline)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.debug:
        print(
            f"repos={len(repos)} selected={len(selected)} "
            f"readme={args.readme} newline={'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    if args.dry_run:
        print(block)
        return 0
    if updated == text:
        print("README is already up to date")
        return 0
    try:
        args.readme.write_bytes(updated.encode("utf-8"))
    except OSError as exc:
        print(f"error: failed to write {args.readme}: {exc}", file=sys.stderr)
        return 1
    print(f"updated {args.readme} with {len(selected)} project(s)")
    for repo in selected:
        date = repo.pushed_at.date().isoformat() if repo.pushed_at else "unknown"
        print(f"- {repo.name} ({date})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
