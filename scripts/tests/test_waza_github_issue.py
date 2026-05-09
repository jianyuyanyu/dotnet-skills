from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "waza_github_issue.py"
SPEC = importlib.util.spec_from_file_location("waza_github_issue", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Failed to load waza_github_issue module from {MODULE_PATH}")
WAZA_GITHUB_ISSUE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WAZA_GITHUB_ISSUE)


class WazaGithubIssueTests(unittest.TestCase):
    def test_imported_only_warnings_are_not_actionable_issue_findings(self) -> None:
        report = {
            "badSkills": 53,
            "repoBadSkills": 0,
            "importedBadSkills": 53,
            "skills": [
                {
                    "name": "upstream-skill",
                    "sourceKind": "imported",
                    "issues": [{"code": "compliance"}],
                }
            ],
        }

        self.assertFalse(WAZA_GITHUB_ISSUE.has_actionable_findings(report))

    def test_repo_owned_warnings_are_actionable_issue_findings(self) -> None:
        report = {
            "badSkills": 1,
            "repoBadSkills": 1,
            "importedBadSkills": 0,
            "skills": [
                {
                    "name": "repo-skill",
                    "sourceKind": "repo",
                    "issues": [{"code": "dead-links"}],
                }
            ],
        }

        self.assertTrue(WAZA_GITHUB_ISSUE.has_actionable_findings(report))

    def test_issue_body_explains_imported_warnings_are_report_only(self) -> None:
        report = {
            "totalSkills": 2,
            "badSkills": 1,
            "repoBadSkills": 1,
            "importedBadSkills": 0,
            "skills": [
                {
                    "name": "repo-skill",
                    "path": "catalog/Tools/Repo/skills/repo-skill/SKILL.md",
                    "sourceKind": "repo",
                    "compliance": "Medium",
                    "tokenCount": 100,
                    "tokenLimit": 5000,
                    "issues": [{"code": "compliance"}],
                }
            ],
        }

        body = WAZA_GITHUB_ISSUE.build_issue_body(report, "# report")

        self.assertIn("actionable repo-owned", body)
        self.assertIn("Imported upstream warnings are included", body)


if __name__ == "__main__":
    unittest.main()
