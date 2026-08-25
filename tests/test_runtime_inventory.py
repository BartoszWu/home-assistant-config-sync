import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "export"))

from runtime_inventory import (  # noqa: E402
    build_runtime_inventory,
    safe_icon,
    safe_registry_id,
    safe_runtime_attributes,
    safe_runtime_text,
)


class RuntimeInventoryTests(unittest.TestCase):
    def test_exports_all_entities_from_allowlisted_integrations(self):
        entities = [
            {
                "entity_id": "sensor.dishwasher_status",
                "platform": "home_connect",
                "device_id": "device-dishwasher",
                "original_name": "Operation state",
                "disabled_by": None,
            },
            {
                "entity_id": "sensor.washer_status",
                "platform": "lg_thinq",
                "device_id": "device-washer",
                "disabled_by": None,
            },
            {
                "entity_id": "sensor.private_text",
                "platform": "template",
                "disabled_by": None,
            },
        ]
        devices = {
            "device-dishwasher": {
                "name": "Dishwasher",
                "area_id": "kitchen",
            },
            "device-washer": {
                "name_by_user": "Washer",
                "area_id": "laundry",
            },
        }
        areas = {
            "kitchen": {"name": "Kitchen"},
            "laundry": {"name": "Laundry"},
        }
        states = [
            {
                "entity_id": "sensor.dishwasher_status",
                "state": "run",
                "last_changed": "2026-08-25T10:00:00+00:00",
                "last_updated": "2026-08-25T10:01:00+00:00",
                "attributes": {
                    "friendly_name": "Dishwasher operation state",
                    "device_class": "enum",
                    "options": ["inactive", "run", "finished"],
                    "icon": "mdi:dishwasher",
                },
            },
            {
                "entity_id": "sensor.washer_status",
                "state": "washing",
                "attributes": {},
            },
            {
                "entity_id": "sensor.private_text",
                "state": "do not export",
                "attributes": {},
            },
        ]

        result = build_runtime_inventory(
            entities,
            states,
            devices,
            areas,
        )

        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["record_type"], "current_state_snapshot")
        self.assertEqual(
            [item["entity_id"] for item in result["states"]],
            ["sensor.dishwasher_status", "sensor.washer_status"],
        )

        dishwasher = result["states"][0]
        self.assertEqual(dishwasher["record_type"], "entity_runtime_state")
        self.assertEqual(dishwasher["source"], "home_assistant_websocket")
        self.assertEqual(dishwasher["friendly_name"], "Dishwasher operation state")
        self.assertEqual(dishwasher["device"], "Dishwasher")
        self.assertEqual(dishwasher["device_id"], "device-dishwasher")
        self.assertEqual(dishwasher["area"], "Kitchen")
        self.assertEqual(dishwasher["status"], "available")
        self.assertTrue(dishwasher["enabled"])
        self.assertEqual(dishwasher["state"], "run")
        self.assertEqual(dishwasher["device_class"], "enum")
        self.assertEqual(
            dishwasher["options"],
            ["inactive", "run", "finished"],
        )
        self.assertEqual(dishwasher["icon"], "mdi:dishwasher")
        self.assertEqual(
            dishwasher["last_updated"],
            "2026-08-25T10:01:00+00:00",
        )

    def test_distinguishes_disabled_unavailable_unknown_and_available(self):
        entities = [
            {
                "entity_id": "sensor.disabled",
                "platform": "home_connect",
                "disabled_by": "integration",
            },
            {
                "entity_id": "sensor.unavailable",
                "platform": "lg_thinq",
                "disabled_by": None,
            },
            {
                "entity_id": "sensor.unknown",
                "platform": "lg_thinq",
                "disabled_by": None,
            },
            {
                "entity_id": "sensor.available",
                "platform": "lg_thinq",
                "disabled_by": None,
            },
        ]
        states = [
            {
                "entity_id": "sensor.disabled",
                "state": "stale-value-must-not-export",
                "attributes": {},
            },
            {
                "entity_id": "sensor.unavailable",
                "state": "unavailable",
                "attributes": {},
            },
            {
                "entity_id": "sensor.unknown",
                "state": "unknown",
                "attributes": {},
            },
            {
                "entity_id": "sensor.available",
                "state": "running",
                "attributes": {},
            },
        ]

        result = {
            item["entity_id"]: item
            for item in build_runtime_inventory(entities, states)["states"]
        }

        self.assertFalse(result["sensor.disabled"]["enabled"])
        self.assertFalse(result["sensor.disabled"]["loaded"])
        self.assertEqual(result["sensor.disabled"]["status"], "disabled")
        self.assertIsNone(result["sensor.disabled"]["state"])
        self.assertEqual(result["sensor.disabled"]["disabled_by"], "integration")

        self.assertTrue(result["sensor.unavailable"]["enabled"])
        self.assertTrue(result["sensor.unavailable"]["loaded"])
        self.assertEqual(result["sensor.unavailable"]["status"], "unavailable")
        self.assertEqual(result["sensor.unknown"]["status"], "unknown")
        self.assertEqual(result["sensor.available"]["status"], "available")

    def test_uses_per_integration_attribute_allowlists(self):
        attributes = safe_runtime_attributes("home_connect", {
            "device_class": "duration",
            "unit_of_measurement": "min",
            "state_class": "measurement",
            "options": [
                "ready",
                "running",
                "finished",
                {"api_key": "must-not-export"},
            ],
            "program": "eco_50",
            "operation_state": "run",
            "energy_today": 1.2,
            "host": "private-host",
            "hostname": "private-hostname",
            "ssid": "private-network",
            "serial_number": "private-serial",
            "access_token": "must-not-export",
            "refresh_token": "must-not-export",
            "api_key": "must-not-export",
            "nested": {
                "access_token": "must-not-export",
            },
            "remaining_program_time": {
                "value": 42,
                "api_key": "must-not-export",
            },
        })

        self.assertEqual(
            attributes,
            {
                "device_class": "duration",
                "operation_state": "run",
                "options": ["ready", "running", "finished"],
                "program": "eco_50",
                "state_class": "measurement",
                "unit_of_measurement": "min",
            },
        )

        lg_attributes = safe_runtime_attributes("lg_thinq", {
            "current_status": "washing",
            "energy_today": 1.2,
            "program": "must-not-cross-integrations",
        })

        self.assertEqual(
            lg_attributes,
            {
                "current_status": "washing",
                "energy_today": 1.2,
            },
        )

    def test_redacts_secrets_network_values_and_blobs(self):
        self.assertEqual(
            safe_runtime_text("access_" + "token=very-secret-value"),
            "[redacted-secret]",
        )
        self.assertEqual(
            safe_runtime_text("Bear" + "er abcdefghijklmnopqrstuvwxyz123456"),
            "[redacted-secret]",
        )
        self.assertEqual(
            safe_runtime_text("gh" + "p_abcdefghijklmnopqrstuvwxyz123456"),
            "[redacted-secret]",
        )
        self.assertEqual(safe_runtime_text("192.168.10.20"), "[redacted-ip]")
        self.assertEqual(
            safe_runtime_text("AA:BB:CC:DD:EE:FF"),
            "[redacted-mac]",
        )
        self.assertEqual(
            safe_runtime_text("x" * 300),
            "[redacted-long-value]",
        )

    def test_exports_only_mdi_icons(self):
        self.assertEqual(safe_icon("mdi:washing-machine"), "mdi:washing-machine")
        self.assertIsNone(safe_icon("https://example.test/private.png"))

    def test_preserves_only_bounded_internal_registry_ids(self):
        self.assertEqual(
            safe_registry_id("a" * 32),
            "a" * 32,
        )
        self.assertIsNone(safe_registry_id("id with spaces"))
        self.assertIsNone(safe_registry_id("x" * 65))


if __name__ == "__main__":
    unittest.main()
