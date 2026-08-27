import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.models.domain import AppConfig
from app.connectors.tally.unit import build_unit_xml, resolve_gstrepuom
from app.validation.validator import validate_entity_payload
from app.sync.dependencies import DependencyResolver
from app.mapping.store import MappingStore
from app.clients.rentasst_client import RentAsstClient
from app.services.sync_service import SyncService
from app.configuration.store import ConfigStore


class TestAssetUnitSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "state.db")
        self.store = MappingStore(self.db_path)
        self.cfg = AppConfig(
            rentasst_url="http://localhost:8000/api",
            rentasst_api_key="test_api_key",
            rentasst_tenant_id="test_tenant",
            external_url="http://localhost:9000",
            external_system_type="tally",
            tally_company_name="Test Rental Corp",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_build_unit_xml_generation(self):
        """Validates that build_unit_xml generates valid Tally Unit XML with UQC and decimal places."""
        unit_data = {
            "id": "1",
            "name": "Box",
            "symbol": "BOX",
            "formal_name": "Boxes",
            "decimal_places": 0,
        }
        xml = build_unit_xml(unit_data, action="Create", company_name="Test Rental Corp")
        self.assertIn('<UNIT NAME="Box" ACTION="Create">', xml)
        self.assertIn("<NAME>Box</NAME>", xml)
        self.assertIn("<ORIGINALNAME>Boxes</ORIGINALNAME>", xml)
        self.assertIn("<SYMBOL>BOX</SYMBOL>", xml)
        self.assertIn("<DECIMALPLACES>0</DECIMALPLACES>", xml)
        self.assertIn("<ISSIMPLEUNIT>YES</ISSIMPLEUNIT>", xml)
        self.assertIn("<GSTREPUOM>BOX-BOXES</GSTREPUOM>", xml)
        self.assertIn("<SVCURRENTCOMPANY>Test Rental Corp</SVCURRENTCOMPANY>", xml)

    def test_resolve_gstrepuom(self):
        """Tests standard GST UQC mappings for common rental units."""
        self.assertEqual(resolve_gstrepuom("Nos"), "NOS-NUMBERS")
        self.assertEqual(resolve_gstrepuom("Pcs"), "PCS-PIECES")
        self.assertEqual(resolve_gstrepuom("Box"), "BOX-BOXES")
        self.assertEqual(resolve_gstrepuom("Set"), "SET-SETS")
        self.assertEqual(resolve_gstrepuom("Hours"), "HRS-HOURS")
        self.assertEqual(resolve_gstrepuom("UnknownCustomUnit"), "OTH-OTHERS")

    def test_unit_payload_validation(self):
        """Tests pre-flight schema validation for unit payloads."""
        valid_unit = {"id": "U1", "name": "Mtr", "symbol": "MTR", "decimal_places": 2}
        is_valid, err = validate_entity_payload("units", valid_unit)
        self.assertTrue(is_valid)
        self.assertIsNone(err)

        # Missing name and id
        invalid_unit = {"decimal_places": 0}
        is_valid, err = validate_entity_payload("units", invalid_unit)
        self.assertFalse(is_valid)
        self.assertIn("missing required field", err)

        # Invalid decimal places
        invalid_dp = {"id": "U2", "name": "Kg", "decimal_places": 10}
        is_valid, err = validate_entity_payload("units", invalid_dp)
        self.assertFalse(is_valid)
        self.assertIn("decimal places", err)

    def test_equipment_unit_dependency_resolution(self):
        """Tests that DependencyResolver verifies Unit mapping for equipment before sync."""
        equip_data = {
            "id": "EQ-101",
            "name": "Heavy Crane",
            "asset_unit": {"id": "U-HR", "name": "Hours"},
        }
        # Without unit in mapping store -> dependency failure
        has_dep, reason, ent, missing_id = DependencyResolver.check_dependencies(
            "equipment", equip_data, self.store
        )
        self.assertFalse(has_dep)
        self.assertEqual(ent, "units")
        self.assertEqual(missing_id, "Hours")

        # After saving unit mapping -> dependency satisfied
        self.store.save_mapping("units", "U-HR", "Hours")
        has_dep, reason, ent, missing_id = DependencyResolver.check_dependencies(
            "equipment", equip_data, self.store
        )
        self.assertTrue(has_dep)

    def test_fetch_asset_units_fallback_to_equipment(self):
        """Tests that RentAsstClient falls back to extracting units from equipment list if unit endpoint fails."""
        client = RentAsstClient(self.cfg)
        mock_equipment = [
            {"id": "1", "name": "Drill", "asset_unit": {"id": "10", "name": "Pcs", "symbol": "PCS"}},
            {"id": "2", "name": "Generator", "asset_unit_name": "Hours (HRS)"},
            {"id": "3", "name": "Ladder", "unit": "Nos"},
        ]

        with patch.object(client, "_request_with_fallback") as mock_req:
            # First call for units raises 404
            mock_req.side_effect = Exception("Not found")
            with patch.object(client, "fetch_equipment", return_value=mock_equipment):
                units = client.fetch_asset_units()
                unit_names = [u["name"] for u in units]
                self.assertIn("Pcs", unit_names)
                self.assertIn("Hours", unit_names)
                self.assertIn("Nos", unit_names)

    def test_sync_service_executes_units_before_equipment(self):
        """Tests that SyncService executes sync_units before sync_equipment when syncing equipment or full sync."""
        cfg_store = ConfigStore(self.test_dir)
        cfg_store.save(self.cfg)

        execution_order = []

        def mock_sync_units(*args, **kwargs):
            execution_order.append("sync_units")
            return {"processed": 2, "created": 2, "updated": 0, "failed": 0, "skipped": 0}

        def mock_sync_equipment(*args, **kwargs):
            execution_order.append("sync_equipment")
            return {"processed": 3, "created": 3, "updated": 0, "failed": 0, "skipped": 0}

        with patch("app.services.sync_service.sync_units", side_effect=mock_sync_units), \
             patch("app.services.sync_service.sync_equipment", side_effect=mock_sync_equipment):
            service = SyncService(self.test_dir)
            res = service.execute_sync("equipment")

            self.assertEqual(execution_order, ["sync_units", "sync_equipment"])
            self.assertEqual(res["processed"], 5)
            self.assertEqual(res["created"], 5)


if __name__ == "__main__":
    unittest.main()
