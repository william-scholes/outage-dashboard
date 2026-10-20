"""Unit tests for customer CSV import CLI command (TODO #12)."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from outage_automation.cli import cmd_import_customers
from outage_automation.paths import resolve_paths
from outage_automation.store import read_json


class TestCustomerImport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_import_cust_"))
        self.paths = resolve_paths(str(self.tmp))
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        # Seed approved regions
        self.paths.approved_regions_csv.parent.mkdir(parents=True, exist_ok=True)
        self.paths.approved_regions_csv.write_text(
            "Service,Region Code,Display Name\n"
            "azure,australia-east,Australia East\n"
            "azure,australia-southeast,Australia Southeast\n"
            "azure,global,Global\n"
            "aws,ap-southeast-2,Sydney\n"
            "aws,global,Global\n"
            "zscaler,global,Global\n"
            "cloudflare,global,Global\n",
            encoding="utf-8",
        )
        # Seed initial customers.json
        (self.paths.customers_path).write_text(
            json.dumps([{
                "customer_id": "seed-corp",
                "name": "Seed Corp",
                "subscriptions": {"aws": ["global"]},
            }]),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_tabular_csv_merge(self) -> None:
        csv_file = self.tmp / "customers.csv"
        csv_file.write_text(
            "customer_name,service,regions,sub_services\n"
            "Alpha Corp,aws,ap-southeast-2,\n"
            "Beta Tech,zscaler,global,\"zia,zpa\"\n",
            encoding="utf-8",
        )
        rc = cmd_import_customers(str(self.tmp), str(csv_file), replace=False)
        self.assertEqual(rc, 0)

        data = read_json(self.paths.customers_path, [])
        self.assertEqual(len(data), 3)  # seed-corp + alpha-corp + beta-tech
        ids = {c["customer_id"] for c in data}
        self.assertIn("seed-corp", ids)
        self.assertIn("alpha-corp", ids)
        self.assertIn("beta-tech", ids)

        beta = next(c for c in data if c["customer_id"] == "beta-tech")
        self.assertEqual(beta["subscriptions"], {"zscaler": ["global"]})
        self.assertEqual(beta["sub_services"], {"zscaler": ["zia", "zpa"]})

    def test_import_matrix_csv_template(self) -> None:
        csv_file = self.tmp / "template.csv"
        csv_file.write_text(
            "Customer Name,Customer ID,Azure Regions,AWS Regions,Zscaler Products,Cloudflare\n"
            "Mega Corp,mega-001,global,ap-southeast-2,ZIA,global\n",
            encoding="utf-8",
        )
        rc = cmd_import_customers(str(self.tmp), str(csv_file), replace=False)
        self.assertEqual(rc, 0)

        data = read_json(self.paths.customers_path, [])
        mega = next(c for c in data if c["customer_id"] == "mega-001")
        self.assertEqual(mega["name"], "Mega Corp")
        self.assertIn("global", mega["subscriptions"]["azure"])
        self.assertIn("ap-southeast-2", mega["subscriptions"]["aws"])
        self.assertEqual(mega["sub_services"], {"zscaler": ["zia"]})

    def test_import_replace(self) -> None:
        csv_file = self.tmp / "replace.csv"
        csv_file.write_text(
            "customer_name,service,regions,sub_services\n"
            "Solo Corp,aws,ap-southeast-2,\n",
            encoding="utf-8",
        )
        rc = cmd_import_customers(str(self.tmp), str(csv_file), replace=True)
        self.assertEqual(rc, 0)

        data = read_json(self.paths.customers_path, [])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["customer_id"], "solo-corp")

    def test_import_validation_error_invalid_region(self) -> None:
        csv_file = self.tmp / "invalid.csv"
        csv_file.write_text(
            "customer_name,service,regions,sub_services\n"
            "Bad Corp,aws,mars-colony-1,\n",
            encoding="utf-8",
        )
        rc = cmd_import_customers(str(self.tmp), str(csv_file), replace=False)
        self.assertEqual(rc, 1)  # Validation error rejected

    def test_import_invoices_command(self) -> None:
        from outage_automation.cli import cmd_import_invoices
        # Write small sample invoice export matching customer invoice export CSV structure
        csv_file = self.tmp / "sample_invoices.csv"
        csv_file.write_text(
            "Customer,Group Header,Description\n"
            "ACME Corp,\\N,Microsoft Azure Plan - consumption\n"
            "ACME Corp,\\N,AWS Monthly Usage\n"
            "ACME Corp,\\N,Onefee per user support\n",
            encoding="utf-8",
        )
        rc = cmd_import_invoices(str(self.tmp), str(csv_file), backup=False)
        self.assertEqual(rc, 0)
        data = read_json(self.paths.customers_path, [])
        acme = next((c for c in data if c["name"] == "ACME Corp"), None)
        self.assertIsNotNone(acme)
        self.assertIn("azure", acme["subscriptions"])
        self.assertIn("aws", acme["subscriptions"])


if __name__ == "__main__":
    unittest.main()

