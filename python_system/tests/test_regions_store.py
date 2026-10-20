"""Unit tests for dashboard.regions_store — the approved-regions.csv I/O
and validation module used by the dashboard editor and the
`validate-regions` CLI subcommand.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from outage_automation.dashboard.regions_store import (
    CSV_HEADER,
    RegionRow,
    add_region,
    load_regions_csv,
    remove_region,
    slugify,
    unique_id,
    update_display_name,
    validate_regions,
    write_regions_csv,
)


def _write_csv(path: Path, body: str) -> None:
    """Write a CSV file with the editor's 3-column header."""
    if not body.startswith(",".join(CSV_HEADER[:2])):
        body = ",".join(CSV_HEADER) + "\n" + body
    path.write_text(body, encoding="utf-8", newline="")


# ---------------------------------------------------------------------------
# Slug / id
# ---------------------------------------------------------------------------


class TestSlugify(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(slugify("Acme Corp"), "acme-corp")

    def test_punctuation_collapsed(self) -> None:
        self.assertEqual(slugify("Test / Client 2!"), "test-client-2")

    def test_strips_edges(self) -> None:
        self.assertEqual(slugify("  / acme / "), "acme")

    def test_empty_fallback(self) -> None:
        self.assertEqual(slugify(""), "customer")
        self.assertEqual(slugify("---"), "customer")
        self.assertEqual(slugify("   "), "customer")


class TestUniqueId(unittest.TestCase):
    def test_unique_base(self) -> None:
        self.assertEqual(unique_id("acme", set()), "acme")

    def test_dedupe_first(self) -> None:
        self.assertEqual(unique_id("acme", {"acme"}), "acme-2")

    def test_dedupe_skip(self) -> None:
        self.assertEqual(unique_id("acme", {"acme", "acme-2", "acme-3"}), "acme-4")

    def test_unrelated_ids_dont_conflict(self) -> None:
        self.assertEqual(unique_id("acme", {"foo", "bar"}), "acme")


# ---------------------------------------------------------------------------
# Load / write
# ---------------------------------------------------------------------------


class TestLoadRegionsCsv(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                load_regions_csv(Path(d) / "nope.csv"),
                [],
            )

    def test_parses_3_columns(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            _write_csv(
                p,
                "azure,australia-east,Australia East\n"
                "azure,global,Global\n",
            )
            rows = load_regions_csv(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0],
            RegionRow(
                service="azure",
                region_code="australia-east",
                display_name="Australia East",
            ),
        )
        self.assertEqual(
            rows[1],
            RegionRow(
                service="azure",
                region_code="global",
                display_name="Global",
            ),
        )

    def test_skips_blank_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            _write_csv(
                p,
                "azure,australia-east,Australia East\n"
                "  ,  ,  \n"  # whitespace-only row
                "azure,global,Global\n",
            )
            rows = load_regions_csv(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.region_code for r in rows], ["australia-east", "global"])

    def test_normalises_case(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            _write_csv(
                p,
                "Azure,Australia-East,Australia East\n"
                "AZURE,GLOBAL,Global\n",
            )
            rows = load_regions_csv(p)
        self.assertEqual([r.service for r in rows], ["azure", "azure"])
        self.assertEqual([r.region_code for r in rows], ["australia-east", "global"])

    def test_missing_required_column_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            p.write_text("Service,Display Name\nazure,Australia East\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_regions_csv(p)
        self.assertIn("Region Code", str(ctx.exception))

    def test_extra_columns_ignored(self) -> None:
        """A legacy 5-column CSV (with Description + Is Global) loads
        cleanly — only the first 3 columns are read. The next editor
        write rewrites the file in 3-column form."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            p.write_text(
                "Service,Region Code,Display Name,Description,Is Global\n"
                "azure,australia-east,Australia East,Primary region,No\n",
                encoding="utf-8",
            )
            rows = load_regions_csv(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].service, "azure")
        self.assertEqual(rows[0].region_code, "australia-east")
        self.assertEqual(rows[0].display_name, "Australia East")


class TestWriteRegionsCsv(unittest.TestCase):
    def test_round_trip(self) -> None:
        rows = [
            RegionRow("azure", "australia-east", "Australia East"),
            RegionRow("azure", "global", "Global"),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            write_regions_csv(p, rows)
            reloaded = load_regions_csv(p)
        self.assertEqual(reloaded, rows)

    def test_atomic_no_tmp_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            write_regions_csv(p, [RegionRow("aws", "us-east-1", "US East")])
            self.assertFalse(p.with_suffix(p.suffix + ".tmp").exists())
            self.assertTrue(p.exists())

    def test_creates_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nested" / "approved-regions.csv"
            write_regions_csv(p, [RegionRow("aws", "us-east-1", "US East")])
            self.assertTrue(p.exists())

    def test_writes_3_columns(self) -> None:
        rows = [
            RegionRow("azure", "global", "Global"),
            RegionRow("aws", "us-east-1", "US East"),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved-regions.csv"
            write_regions_csv(p, rows)
            text = p.read_text(encoding="utf-8")
        # Header line + two data lines; the new schema is exactly
        # `Service,Region Code,Display Name` — no Description, no Is Global.
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Service,Region Code,Display Name")


# ---------------------------------------------------------------------------
# validate_regions
# ---------------------------------------------------------------------------


class TestValidateRegions(unittest.TestCase):
    def test_clean(self) -> None:
        rows = [
            RegionRow("azure", "australia-east", "Australia East"),
            RegionRow("aws", "us-east-1", "US East"),
            RegionRow("azure", "global", "Global"),
        ]
        self.assertEqual(validate_regions(rows), [])

    def test_unknown_service(self) -> None:
        rows = [RegionRow("foobar", "us-east-1", "US East")]
        errs = validate_regions(rows)
        self.assertEqual(len(errs), 1)
        self.assertIn("service", errs[0].path)
        self.assertIn("unknown service", errs[0].message)

    def test_duplicate_pair(self) -> None:
        rows = [
            RegionRow("azure", "australia-east", "Australia East"),
            RegionRow("azure", "australia-east", "Australia East 2"),
        ]
        errs = validate_regions(rows)
        self.assertEqual(len(errs), 1)
        self.assertIn("duplicate", errs[0].message)

    def test_empty_display_name(self) -> None:
        rows = [RegionRow("azure", "australia-east", "")]
        errs = validate_regions(rows)
        self.assertEqual(len(errs), 1)
        self.assertIn("display_name", errs[0].path)

    def test_invalid_region_code(self) -> None:
        # Uppercase letters and underscores are not allowed.
        rows = [RegionRow("azure", "US_East_1", "US East")]
        errs = validate_regions(rows)
        self.assertEqual(len(errs), 1)
        self.assertIn("region_code", errs[0].path)
        self.assertIn("kebab-case", errs[0].message)

    def test_global_literal_allowed(self) -> None:
        rows = [RegionRow("azure", "global", "Global")]
        self.assertEqual(validate_regions(rows), [])


# ---------------------------------------------------------------------------
# add_region / update / remove (pure mutations)
# ---------------------------------------------------------------------------


class TestAddRegion(unittest.TestCase):
    def _base(self) -> list:
        return [RegionRow("azure", "australia-east", "Australia East")]

    def test_succeeds(self) -> None:
        out, errs = add_region(self._base(), "aws", "us-east-1", "US East")
        self.assertEqual(errs, [])
        self.assertEqual(len(out), 2)
        self.assertEqual(
            out[-1],
            RegionRow("aws", "us-east-1", "US East"),
        )

    def test_global_allowed(self) -> None:
        out, errs = add_region(self._base(), "azure", "global", "Global")
        self.assertEqual(errs, [])
        self.assertEqual(out[-1].region_code, "global")

    def test_missing_service(self) -> None:
        out, errs = add_region(self._base(), "", "us-east-1", "US East")
        self.assertGreater(len(errs), 0)
        self.assertEqual(out, self._base())  # no change

    def test_unknown_service(self) -> None:
        out, errs = add_region(self._base(), "foobar", "us-east-1", "US East")
        self.assertEqual(len(errs), 1)
        self.assertIn("unknown service", errs[0].message)
        self.assertEqual(out, self._base())

    def test_missing_region_code(self) -> None:
        out, errs = add_region(self._base(), "aws", "", "US East")
        self.assertEqual(len(errs), 1)
        self.assertIn("region_code", errs[0].path)
        self.assertEqual(out, self._base())

    def test_invalid_region_code(self) -> None:
        out, errs = add_region(self._base(), "aws", "US_East", "US East")
        self.assertEqual(len(errs), 1)
        self.assertIn("kebab-case", errs[0].message)
        self.assertEqual(out, self._base())

    def test_empty_display_name(self) -> None:
        out, errs = add_region(self._base(), "aws", "us-east-1", "")
        self.assertEqual(len(errs), 1)
        self.assertIn("display_name", errs[0].path)
        self.assertEqual(out, self._base())

    def test_duplicate_pair(self) -> None:
        out, errs = add_region(self._base(), "azure", "australia-east", "Anything")
        self.assertEqual(len(errs), 1)
        self.assertIn("duplicate", errs[0].message)
        self.assertEqual(out, self._base())

    def test_case_normalised(self) -> None:
        out, errs = add_region(self._base(), "AWS", "us-east-1", "US East")
        self.assertEqual(errs, [])
        self.assertEqual(out[-1].service, "aws")
        self.assertEqual(out[-1].region_code, "us-east-1")


class TestUpdateDisplayName(unittest.TestCase):
    def _base(self) -> list:
        return [
            RegionRow("azure", "australia-east", "Australia East"),
        ]

    def test_updates_display_name(self) -> None:
        out, errs = update_display_name(
            self._base(), "azure", "australia-east", "AU East"
        )
        self.assertEqual(errs, [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].display_name, "AU East")

    def test_not_found(self) -> None:
        out, errs = update_display_name(
            self._base(), "azure", "nope", "Anything"
        )
        self.assertEqual(len(errs), 1)
        self.assertIn("no row found", errs[0].message)
        self.assertEqual(out, self._base())

    def test_empty_display_name_rejected(self) -> None:
        out, errs = update_display_name(
            self._base(), "azure", "australia-east", ""
        )
        self.assertEqual(len(errs), 1)
        self.assertIn("display_name", errs[0].path)
        self.assertEqual(out, self._base())

    def test_case_insensitive_match(self) -> None:
        out, errs = update_display_name(
            self._base(), "AZURE", "AUSTRALIA-EAST", "AU East"
        )
        self.assertEqual(errs, [])
        self.assertEqual(out[0].display_name, "AU East")


class TestRemoveRegion(unittest.TestCase):
    def _base(self) -> list:
        return [
            RegionRow("azure", "australia-east", "Australia East"),
            RegionRow("aws", "us-east-1", "US East"),
        ]

    def test_filters_pair(self) -> None:
        out, errs = remove_region(self._base(), "azure", "australia-east")
        self.assertEqual(errs, [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].service, "aws")

    def test_missing_pair_is_noop(self) -> None:
        out, errs = remove_region(self._base(), "azure", "nope")
        self.assertEqual(errs, [])
        self.assertEqual(out, self._base())


if __name__ == "__main__":
    unittest.main()
