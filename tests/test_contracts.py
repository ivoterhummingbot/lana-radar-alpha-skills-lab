from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from radar_alpha_skills_lab.config import DEFAULT_SOURCE, OUTPUT_DIR, PROJECT_ROOT as CONFIG_PROJECT_ROOT
from radar_alpha_skills_lab.data_contract import REQUIRED_CONTRACTS, REQUIRED_DATABASES, audit_to_dict, run_input_audit
from radar_alpha_skills_lab.blueprint import render_blueprint


class ProjectBoundaryTest(unittest.TestCase):
    def test_project_is_separate_from_source_project(self) -> None:
        self.assertNotEqual(CONFIG_PROJECT_ROOT, DEFAULT_SOURCE.source_root)
        self.assertEqual(CONFIG_PROJECT_ROOT.name, "lana-radar-alpha-skills-lab")
        self.assertEqual(DEFAULT_SOURCE.source_root.name, "lana-community-hotcoin-analyzer")

    def test_source_dbs_open_readonly(self) -> None:
        db_paths = {
            "maker_attention_db": DEFAULT_SOURCE.maker_attention_db,
            "community_history_db": DEFAULT_SOURCE.community_history_db,
        }
        for name in REQUIRED_DATABASES:
            path = db_paths[name]
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("create table __write_probe__(id integer)")

    def test_required_contracts_have_tables(self) -> None:
        self.assertIn("community_history_db", REQUIRED_CONTRACTS)
        self.assertIn("community_history_db", REQUIRED_DATABASES)
        self.assertNotIn("maker_attention_db", REQUIRED_DATABASES)
        self.assertIn("maker_attention_db", REQUIRED_CONTRACTS)  # historical/optional contract
        self.assertIn("lana_community_scores", REQUIRED_CONTRACTS["community_history_db"])

    def test_input_audit_passes_on_current_data(self) -> None:
        audit = run_input_audit()
        self.assertTrue(audit.ok, audit_to_dict(audit))

    def test_blueprint_contains_non_negotiable_boundaries(self) -> None:
        text = render_blueprint()
        self.assertIn("Do not modify `lana-community-hotcoin-analyzer`", text)
        self.assertIn("Do not overwrite the existing cap5/cap20 new-radar shadows", text)
        self.assertIn("signal/control", text)

    def test_output_dir_is_local_to_new_project(self) -> None:
        self.assertEqual(OUTPUT_DIR.parent, CONFIG_PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
