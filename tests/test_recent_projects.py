from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_recent_projects as rp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "github_repos.json"
MARKERS = f"{rp.START}\nold content\n{rp.END}"


def fixture_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repos = rp.parse_repos(fixture_data(), "Komari-Koshigaya")

    def test_filters_profile_forks_archived_and_private(self) -> None:
        names = {repo.name for repo in self.repos}
        self.assertEqual(len(self.repos), 6)
        self.assertNotIn("Komari-Koshigaya", names)
        self.assertNotIn("v2rayNG", names)
        self.assertNotIn("old-archived", names)
        self.assertNotIn("private-one", names)
        self.assertIn("no-description", names)

    def test_include_forks(self) -> None:
        repos = rp.parse_repos(
            fixture_data(), "Komari-Koshigaya", include_forks=True
        )
        self.assertIn("v2rayNG", {repo.name for repo in repos})

    def test_select_is_sorted_and_limited(self) -> None:
        selected = rp.select_repos(self.repos, 5)
        self.assertEqual(
            [repo.name for repo in selected],
            [
                "gitcode-npusim-review",
                "chromeScripts",
                "FirstCoder",
                "duplicate-cleaner",
                "media-server",
            ],
        )

    def test_missing_description_still_formats(self) -> None:
        repo = next(repo for repo in self.repos if repo.name == "no-description")
        line = rp.format_repo(repo)
        self.assertIn("[no-description]", line)
        self.assertIn("2026-05-01", line)
        self.assertNotIn("—", line)

    def test_format_escapes_markdown_and_html(self) -> None:
        repo = rp.Repo(
            name="a_b",
            url="https://example.com/a_b",
            description="<b>hello</b> & *world*",
            pushed_at=rp.parse_iso("2026-11-25T00:00:00Z"),
        )
        line = rp.format_repo(repo)
        self.assertIn(r"a\_b", line)
        self.assertIn("&lt;b&gt;hello&lt;/b&gt; &amp; *world*", line)

    def test_repo_without_pushed_at_sorts_last(self) -> None:
        repos = [
            rp.Repo("new", "https://example.com/new",
                    "new", rp.parse_iso("2026-01-01T00:00:00Z")),
            rp.Repo("unknown", "https://example.com/unknown", "unknown", None),
        ]
        selected = rp.select_repos(repos, 2)
        self.assertEqual([repo.name for repo in selected], ["new", "unknown"])

    def test_invalid_payload_raises(self) -> None:
        with self.assertRaises(ValueError):
            rp.parse_repos({"not": "a list"}, "Komari-Koshigaya")


class BlockTests(unittest.TestCase):
    def test_replace_block_lf(self) -> None:
        text = "before\n" + rp.START + "\nold\n" + rp.END + "\nafter\n"
        updated = rp.replace_block(text, "- A\n- B", "\n")
        self.assertIn(rp.START + "\n- A\n- B\n" + rp.END, updated)
        self.assertTrue(updated.startswith("before\n"))
        self.assertTrue(updated.endswith("after\n"))

    def test_replace_block_crlf(self) -> None:
        text = "before\r\n" + rp.START + "\r\nold\r\n" + rp.END + "\r\nafter\r\n"
        updated = rp.replace_block(text, "- A\n- B", "\r\n")
        self.assertIn(
            rp.START + "\r\n- A\r\n- B\r\n" + rp.END, updated
        )
        self.assertIn("before\r\n", updated)
        self.assertIn("after\r\n", updated)

    def test_missing_markers_raises(self) -> None:
        with self.assertRaises(ValueError):
            rp.replace_block("no markers here", "- A")


class MainTests(unittest.TestCase):
    def make_readme(self, directory: Path, newline: str = "\n") -> Path:
        path = directory / "README.md"
        text = newline.join([
            "# Profile",
            "",
            "### Recent Projects",
            rp.START,
            "old content",
            rp.END,
            "",
            "### Other",
            "keep me",
            "",
        ])
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_updates_readme_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = self.make_readme(Path(tmp))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                ])
            self.assertEqual(code, 0)
            text = readme.read_text(encoding="utf-8")
            self.assertIn("gitcode-npusim-review", text)
            self.assertIn("chromeScripts", text)
            self.assertNotIn("v2rayNG", text)
            self.assertNotIn("private-one", text)
            self.assertIn("### Other\nkeep me", text)
            first = text

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                ])
            self.assertEqual(code, 0)
            self.assertIn("already up to date", output.getvalue())
            self.assertEqual(readme.read_text(encoding="utf-8"), first)

    def test_preserves_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = self.make_readme(Path(tmp), newline="\r\n")
            with contextlib.redirect_stdout(io.StringIO()):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                ])
            self.assertEqual(code, 0)
            raw = readme.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = self.make_readme(Path(tmp))
            before = readme.read_bytes()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                    "--dry-run",
                ])
            self.assertEqual(code, 0)
            self.assertIn("gitcode-npusim-review", output.getvalue())
            self.assertEqual(readme.read_bytes(), before)

    def test_missing_markers_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = Path(tmp) / "README.md"
            readme.write_text("# Profile\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                ])
            self.assertEqual(code, 1)

    def test_missing_fixture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = self.make_readme(Path(tmp))
            with contextlib.redirect_stderr(io.StringIO()):
                code = rp.main([
                    "--input-json", str(Path(tmp) / "missing.json"),
                    "--readme", str(readme),
                ])
            self.assertEqual(code, 1)

    def test_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readme = self.make_readme(Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                code = rp.main([
                    "--input-json", str(FIXTURE),
                    "--readme", str(readme),
                    "--limit", "2",
                ])
            self.assertEqual(code, 0)
            text = readme.read_text(encoding="utf-8")
            self.assertIn("gitcode-npusim-review", text)
            self.assertIn("chromeScripts", text)
            self.assertNotIn("FirstCoder", text)


if __name__ == "__main__":
    unittest.main()
