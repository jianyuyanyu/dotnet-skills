from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nightly_catalog_pr as prs
import nightly_skill_refresh as refresh


class CandidateTests(unittest.TestCase):
    def candidate(self):
        return {"state": "open", "draft": False, "body": prs.MARKER,
                "head": {"ref": prs.BRANCH, "sha": "head", "repo": {"full_name": "o/r"}},
                "base": {"ref": "main", "sha": "base", "repo": {"full_name": "o/r"}}}

    def test_valid_candidate(self):
        prs.verify_merge_candidate(self.candidate(), "head", "base", "base")

    def test_changed_head_is_not_merged(self):
        with self.assertRaisesRegex(ValueError, "head changed"):
            prs.verify_merge_candidate(self.candidate(), "old", "base", "base")

    def test_changed_base_is_not_merged(self):
        with self.assertRaisesRegex(ValueError, "Main changed"):
            prs.verify_merge_candidate(self.candidate(), "head", "base", "new")

    def test_fork_is_not_merged(self):
        candidate = self.candidate()
        candidate["head"]["repo"]["full_name"] = "other/r"
        with self.assertRaisesRegex(ValueError, "this repository"):
            prs.verify_merge_candidate(candidate, "head", "base", "base")

    def test_closed_or_draft_or_unmarked_candidate_is_not_merged(self):
        for field, value in [("state", "closed"), ("draft", True), ("body", "human")]:
            with self.subTest(field=field):
                candidate = self.candidate()
                candidate[field] = value
                with self.assertRaises(ValueError):
                    prs.verify_merge_candidate(candidate, "head", "base", "base")

    def test_state_and_workflow_paths_are_rejected(self):
        for path in [".github/upstream-watch-state.json", ".github/workflows/check.yml", "scripts/a.py", "AGENTS.md"]:
            self.assertFalse(prs.allowed_path(path))
        self.assertTrue(prs.allowed_path("catalog/Tools/X/skills/x/SKILL.md"))
        self.assertTrue(prs.allowed_path("external-sources/vendir.lock.yml"))

    @patch.object(prs, "output")
    @patch.object(prs, "find_pr", return_value=None)
    @patch.object(prs, "candidate_paths", return_value=["external-sources/vendir.lock.yml"])
    @patch.object(prs, "gh_api")
    def test_transport_only_changes_do_not_create_pr(self, api, paths, find, output):
        prs.publish("o/r", "token", {})
        api.assert_not_called()
        output.assert_called_once_with(changed="false")

    @patch.object(prs, "gh_api")
    @patch.object(prs, "list_labeled_issues")
    def test_failure_issue_is_updated_instead_of_duplicated(self, listing, api):
        listing.return_value = [{"number": 7, "body": prs.MARKER}]
        prs.report_status("o/r", "token", True, "validation failed")
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], "/repos/o/r/issues/7")
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")

    @patch.object(prs, "gh_api")
    @patch.object(prs, "list_labeled_issues")
    def test_recovery_closes_only_owned_failure_issues(self, listing, api):
        listing.return_value = [{"number": 7, "body": prs.MARKER}, {"number": 8, "body": "human issue"}]
        prs.report_status("o/r", "token", False, "")
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs["data"], {"state": "closed"})


class SkillRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = '---\nname: demo\ndescription: Demo\n---\n# Demo\n'
        (self.root / "SKILL.md").write_text(self.original)
        (self.root / "manifest.json").write_text(json.dumps({"version": "1.2.3", "category": "Testing"}))

    def test_real_change_bumps_version_once(self):
        response = {"summary": "Official API changed", "files": {"SKILL.md": self.original + "New usage.\n"}}
        self.assertTrue(refresh.apply_response(self.root, response))
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["version"], "1.2.4")
        self.assertFalse(refresh.apply_response(self.root, response))
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["version"], "1.2.4")

    def test_no_change_keeps_version(self):
        self.assertFalse(refresh.apply_response(self.root, {"summary": "No relevant API changes", "files": {}}))
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["version"], "1.2.3")

    def test_unsafe_paths_are_rejected_before_writing_any_file(self):
        for name in ["../AGENTS.md", "/tmp/evil.md", "references/../../escape.md", "scripts/update.py", "manifest.json", "references//x.md"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                refresh.apply_response(self.root, {"summary": "test", "files": {"SKILL.md": self.original + "change", name: "bad"}})
            self.assertEqual((self.root / "SKILL.md").read_text(), self.original)

    def test_symlink_reference_escape_is_rejected(self):
        (self.root / "references").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            refresh.apply_response(self.root, {"summary": "test", "files": {"references/escape.md": "bad"}})

    def test_frontmatter_identity_cannot_change(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            refresh.apply_response(self.root, {"summary": "test", "files": {"SKILL.md": self.original.replace("demo", "other")}})

    def test_missing_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            refresh.apply_response(self.root, {"files": {}})

    def test_missing_updater_is_failure_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            refresh.refresh_skill(self.root, {}, [])

    @patch.object(refresh.watch, "parse_open_issue")
    def test_pending_issues_are_deduplicated_and_scope_comes_from_config(self, parse):
        parse.return_value = ("group", ["injected-skill"], {"release": {"source_url": "https://evil.test"}})
        config = {"watches": [{"id": "release", "skills": ["demo"], "source_url": "https://official.test"}]}
        tasks, issues = refresh.select_work([{"number": 1}, {"number": 2}], config, {})
        self.assertEqual(list(tasks), ["demo"])
        self.assertEqual(tasks["demo"]["watches"]["release"]["source_url"], "https://official.test")
        self.assertEqual(issues, {1: ["demo"], 2: ["demo"]})

    @patch.object(refresh.watch, "parse_open_issue", return_value=("group", ["demo"], {"unknown": {}}))
    def test_unknown_watch_does_not_disappear_from_queue(self, parse):
        with self.assertRaisesRegex(ValueError, "unknown watches"):
            refresh.select_work([{"number": 1}], {"watches": []}, {})

class WatchAcknowledgementTests(unittest.TestCase):
    @patch.object(refresh.watch, "gh_api")
    def test_release_hidden_behind_extension_releases_is_found(self, api):
        api.side_effect = [
            [{"tag_name": f"extension-{n}", "published_at": "2026-09-01"} for n in range(100)],
            [{"tag_name": "2.52.0", "published_at": "2026-04-21"}],
        ]
        result = refresh.watch.fetch_github_release({"id": "worker", "owner": "Azure", "repo": "worker", "match_tag_regex": r"^\d+\.\d+\.\d+$"}, None)
        self.assertEqual(result["value"], "2.52.0")
        self.assertEqual(api.call_count, 2)
        self.assertIn("page=2", api.call_args.args[0])

    def test_failed_issue_creation_keeps_previous_state_for_retry(self):
        from argparse import Namespace
        from contextlib import ExitStack, redirect_stdout, redirect_stderr
        from io import StringIO
        w = refresh.watch
        args = Namespace(config="config", state="state", validate_config=False, dry_run=False, sync_state_only=False)
        config = {"watches": [{"id": "release", "kind": "github_release", "issue_key": "demo", "skills": ["demo"]}]}
        mocks = {
            "parse_args": args, "resolve_config_paths": [Path("config")],
            "merge_raw_configs": {}, "normalize_config": config,
            "load_json": {"watches": {"release": {"value": "v1"}}},
            "ensure_labels": None, "load_historical_watch_snapshots": {},
            "load_open_issue_groups": {}, "reconcile_open_issues": ({}, {"groups": 0, "issues_rewritten": 0, "duplicates_closed": 0}),
            "fetch_snapshot": {"kind": "github_release", "value": "v2"},
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(patch.object(w, name, return_value=value))
            stack.enter_context(patch.dict(w.os.environ, {"GH_TOKEN": "test", "GITHUB_REPOSITORY": "o/r"}))
            rotate = stack.enter_context(patch.object(w, "rotate_issue_group", side_effect=RuntimeError("GitHub unavailable")))
            save = stack.enter_context(patch.object(w, "dump_json"))
            stack.enter_context(patch.object(w, "write_summary"))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(redirect_stderr(StringIO()))
            self.assertEqual(w.main(), 1)
            rotate.assert_called_once()
            save.assert_not_called()

class PublishTests(unittest.TestCase):
    @patch.object(prs, "output")
    @patch.object(prs, "candidate_paths", return_value=["catalog/Tools/Demo/skills/demo/SKILL.md"])
    @patch.object(prs, "find_pr", return_value={"number": 9, "body": prs.MARKER})
    @patch.object(prs, "gh_api", return_value={"number": 9, "html_url": "https://github.com/o/r/pull/9"})
    @patch.object(prs, "git")
    def test_identical_candidate_reuses_branch_and_pr(self, git, api, find, paths, output):
        answers = {("rev-parse", "HEAD"): "base", ("write-tree",): "tree",
                   ("ls-remote", "--heads", "origin", f"refs/heads/{prs.BRANCH}"): "previous refs/heads/nightly",
                   ("show", "-s", "--format=%ae", "previous"): prs.BOT_EMAIL,
                   ("rev-parse", "previous^{tree}"): "tree", ("rev-parse", "previous^"): "base"}
        git.side_effect = lambda *args: answers.get(args, "")
        prs.publish("o/r", "token", {"completed_issues": [3]})
        self.assertFalse(any(call.args[0] == "push" for call in git.call_args_list))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")
        self.assertIn("Closes #3", api.call_args.kwargs["data"]["body"])
        output.assert_called_once_with(changed="true", head="previous", base="base", pr="9")

    @patch.object(prs, "candidate_paths")
    def test_failed_batch_cannot_publish(self, paths):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            prs.publish("o/r", "token", {"failed": True})
        paths.assert_not_called()

    @patch.object(prs, "output")
    @patch.object(prs, "candidate_paths", return_value=[])
    @patch.object(prs, "find_pr", return_value=None)
    @patch.object(prs, "gh_api")
    def test_reviewed_noop_closes_queue_without_commit(self, api, find, paths, output):
        prs.publish("o/r", "token", {"completed_issues": [3]})
        api.assert_called_once_with("/repos/o/r/issues/3", token="token", method="PATCH", data={"state": "closed"})
        output.assert_called_once_with(changed="false")


if __name__ == "__main__":
    unittest.main()
