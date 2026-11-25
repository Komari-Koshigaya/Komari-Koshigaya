from __future__ import annotations

import math
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import contribution_snake as snake  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "contributions.html"


class ContributionParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.levels, self.total = snake.parse_contributions(self.text)

    def test_parses_every_cell(self) -> None:
        self.assertEqual(len(self.levels), 14)
        self.assertEqual(self.levels[date(2026, 11, 1)], 0)
        self.assertEqual(self.levels[date(2026, 11, 2)], 1)
        self.assertEqual(self.levels[date(2026, 11, 5)], 4)
        self.assertEqual(self.levels[date(2026, 11, 14)], 3)

    def test_parses_total(self) -> None:
        self.assertEqual(self.total, 26)

    def test_empty_html_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            snake.parse_contributions("<html><body>nothing</body></html>")


class CalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        levels, total = snake.parse_contributions(FIXTURE.read_text(encoding="utf-8"))
        self.calendar = snake.build_calendar(levels, total)

    def test_grid_is_sunday_first(self) -> None:
        self.assertEqual(self.calendar.first_sunday, date(2026, 11, 1))
        self.assertEqual(self.calendar.columns, 2)
        self.assertEqual(self.calendar.cell_count, 14)

    def test_serpentine_covers_every_position(self) -> None:
        plan = snake.build_serpentine(self.calendar)
        self.assertEqual(len(plan.points), 14)
        self.assertEqual(plan.order_index[(0, 0)], 0)
        self.assertEqual(plan.order_index[(0, 6)], 6)
        self.assertEqual(plan.order_index[(1, 6)], 7)
        self.assertEqual(plan.order_index[(1, 0)], 13)
        self.assertTrue(plan.d.startswith("M "))

    def test_adjacent_points_have_equal_pitch(self) -> None:
        plan = snake.build_serpentine(self.calendar, cell=11, gap=3)
        distances = [
            math.dist(plan.points[i], plan.points[i + 1])
            for i in range(len(plan.points) - 1)
        ]
        for distance in distances:
            self.assertAlmostEqual(distance, 14.0, places=6)


class SvgTests(unittest.TestCase):
    def setUp(self) -> None:
        levels, total = snake.parse_contributions(FIXTURE.read_text(encoding="utf-8"))
        self.calendar = snake.build_calendar(levels, total)

    def test_light_svg_is_valid_xml(self) -> None:
        svg = snake.build_svg(self.calendar, "Komari-Koshigaya", "light", 30)
        ET.fromstring(svg)
        self.assertIn("<animateMotion", svg)
        self.assertIn('attributeName="stroke-dashoffset"', svg)
        self.assertIn("#9be9a8", svg)
        self.assertIn("26 contributions in the last year", svg)

    def test_dark_svg_uses_dark_palette(self) -> None:
        svg = snake.build_svg(self.calendar, "Komari-Koshigaya", "dark", 30)
        ET.fromstring(svg)
        self.assertIn("#161b22", svg)
        self.assertIn("#39d353", svg)

    def test_every_active_cell_gets_an_eat_animation(self) -> None:
        svg = snake.build_svg(self.calendar, "Komari-Koshigaya", "light", 30)
        active = sum(1 for level in self.calendar.levels.values() if level > 0)
        self.assertEqual(svg.count('attributeName="opacity"'), active)

    def test_head_stays_at_first_cell_without_smil(self) -> None:
        svg = snake.build_svg(self.calendar, "Komari-Koshigaya", "light", 30)
        # The first cell center is left+cell/2, top+cell/2 = 35.5, 27.5.
        self.assertIn('transform="translate(35.50 27.50)"', svg)

    def test_cli_writes_both_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code = snake.main([
                "--input-html", str(FIXTURE),
                "--output-dir", str(out),
                "--min-days", "0",
            ])
            self.assertEqual(code, 0)
            light = out / "snake.svg"
            dark = out / "snake-dark.svg"
            self.assertTrue(light.is_file())
            self.assertTrue(dark.is_file())
            ET.parse(light)
            ET.parse(dark)

    def test_cli_rejects_too_few_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = snake.main([
                "--input-html", str(FIXTURE),
                "--output-dir", tmp,
                "--min-days", "300",
            ])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
