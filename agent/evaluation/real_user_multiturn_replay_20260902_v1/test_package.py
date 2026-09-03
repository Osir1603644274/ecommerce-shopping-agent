from __future__ import annotations

import unittest
import importlib.util
from pathlib import Path


_VERIFY_PATH = Path(__file__).resolve().with_name("verify_package.py")
_SPEC = importlib.util.spec_from_file_location("rumr_verify_package", _VERIFY_PATH)
assert _SPEC and _SPEC.loader
verify_package = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_package)


class RealUserMultiturnPackageTests(unittest.TestCase):
    def test_complete_no_model_verification(self) -> None:
        result = verify_package.verify_all()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["rawUserTurnCount"], 27)
        self.assertEqual(result["pairedFollowupCount"], 19)
        self.assertEqual(result["modelCalls"], 0)
        self.assertFalse(result["unseenOrSealedEligible"])


if __name__ == "__main__":
    unittest.main()
