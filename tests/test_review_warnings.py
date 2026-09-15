import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

from review_warnings import format_scan_warnings  # noqa: E402
from security import unsafe_findings, unsafe_reason  # noqa: E402


PACKAGE = """\
template:
  - sensor:
      - name: Hol
        unique_id: synthetic_unique
        state: "{{ 1 }}"
"""


class ScanWarningTests(unittest.TestCase):
    def test_unique_id_is_a_located_sensitive_field(self):
        parsed = {"template": [{"sensor": [{"name": "Hol", "unique_id": "synthetic_unique"}]}]}
        findings = unsafe_findings(parsed)
        self.assertEqual(findings[0]["reason"], "sensitive field")
        self.assertEqual(findings[0]["field"], "unique_id")
        self.assertNotIn("synthetic_unique", str(findings))
        warnings = format_scan_warnings(parsed, PACKAGE)
        self.assertEqual(warnings[0]["line"], 4)
        self.assertEqual(warnings[0]["field"], "unique_id")
        self.assertNotIn("synthetic_unique", str(warnings))

    def test_first_reason_string_stays_generic(self):
        self.assertEqual(unsafe_reason({"unique_id": "synthetic_unique"}), "sensitive field")
        self.assertEqual(
            unsafe_reason({"nested": [{"clientSecret": "synthetic"}]}),
            "sensitive field",
        )


if __name__ == "__main__":
    unittest.main()
