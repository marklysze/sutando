#!/usr/bin/env python3
"""The schema-owned config-key catalogs must be generated and current."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENERATOR = REPO / "scripts" / "gen-config-key-contract.py"


class ConfigKeyGenerationTests(unittest.TestCase):
    def test_generated_contract_is_current(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            cwd=REPO,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("generated loaders are up to date", result.stdout)

    def test_schema_is_named_as_the_single_source_in_both_loaders(self):
        for relative in ("src/sutando_config.py", "src/sutando_config.ts"):
            source = (REPO / relative).read_text()
            self.assertIn("BEGIN GENERATED CONFIG KEYS", source)
            self.assertIn("scripts/gen-config-key-contract.py", source)


if __name__ == "__main__":
    unittest.main()
