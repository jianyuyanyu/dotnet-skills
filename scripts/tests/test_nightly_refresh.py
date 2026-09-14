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
    @patch.object(prs, "list_labeled_issues", return_value=[])
    def test_healthy_run_does_not_create_any_issue(self, listing, api):
        prs.report_status("o/r", "token", False, "healthy")
        api.assert_not_called()

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

    def test_pending_watches_are_deduplicated_and_scope_comes_from_config(self):
        config = {"watches": [{"id": "release", "skills": ["demo"], "source_url": "https://official.test"}]}
        tasks = refresh.select_work({"pending_watch_ids": ["release", "release"]}, config)
        self.assertEqual(list(tasks), ["demo"])
        self.assertEqual(tasks["demo"]["watches"]["release"]["source_url"], "https://official.test")

    def test_unknown_watch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown pending watch"):
            refresh.select_work({"pending_watch_ids": ["unknown"]}, {"watches": []})

    def test_failed_detection_prevents_refresh(self):
        with self.assertRaisesRegex(ValueError, "detection failed"):
            refresh.select_work({"pending_watch_ids": [], "errors": ["fetch failed"]}, {"watches": []})

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

    def test_detected_changes_never_write_github_issues(self):
        w = refresh.watch
        config = {"watches": [{"id": "release", "skills": ["demo"]}]}
        with patch.object(w, "fetch_snapshot", return_value={"value": "v2"}), patch.object(w, "gh_api") as api:
            first = w.detect_changes(config, {"watches": {"release": {"value": "v1"}}}, "token")
            retry = w.detect_changes(config, {"watches": {"release": {"value": "v1"}}}, "token")
            applied = w.detect_changes(config, {"watches": first["watches"]}, "token")
        api.assert_not_called()
        self.assertEqual(first["pending_watch_ids"], ["release"])
        self.assertEqual(retry["pending_watch_ids"], ["release"])
        self.assertEqual(applied["pending_watch_ids"], [])

    def test_normal_detection_does_not_acknowledge_baseline(self):
        from argparse import Namespace
        from contextlib import ExitStack, redirect_stdout
        from io import StringIO
        w = refresh.watch
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            baseline = root / "baseline.json"
            baseline.write_text('{"watches":{"release":{"value":"v1"}}}')
            args = Namespace(config="config", state=str(baseline), report_dir=root / "report", validate_config=False, dry_run=False, sync_state_only=False)
            config = {"watches": [{"id": "release", "skills": ["demo"]}]}
            for name, value in {"parse_args": args, "resolve_config_paths": [], "merge_raw_configs": {}, "normalize_config": config}.items():
                stack.enter_context(patch.object(w, name, return_value=value))
            stack.enter_context(patch.object(w, "write_summary"))
            stack.enter_context(redirect_stdout(StringIO()))
            fetch = stack.enter_context(patch.object(w, "fetch_snapshot", return_value={"value": "v2"}))
            self.assertEqual(w.main(), 0)
            self.assertEqual(json.loads(baseline.read_text())["watches"]["release"]["value"], "v1")
            self.assertEqual(json.loads((args.report_dir / "pending.json").read_text())["pending_watch_ids"], ["release"])
            args.sync_state_only = True
            fetch.side_effect = RuntimeError("network failure")
            self.assertEqual(w.main(), 1)
            self.assertEqual(json.loads(baseline.read_text())["watches"]["release"]["value"], "v1")

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
        prs.publish("o/r", "token", {})
        self.assertFalse(any(call.args[0] == "push" for call in git.call_args_list))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")
        self.assertNotIn("Closes #", api.call_args.kwargs["data"]["body"])
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
    def test_reviewed_noop_has_no_issue_writes(self, api, find, paths, output):
        prs.publish("o/r", "token", {})
        api.assert_not_called()
        output.assert_called_once_with(changed="false")

class LockedVendirTests(unittest.TestCase):
    def test_locked_verification_keeps_committed_descriptive_metadata(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "external-sources").mkdir()
            (root / "scripts").mkdir()
            lock = root / "external-sources/vendir.lock.yml"
            lock.write_text("original locked SHA and descriptive tag\n")
            (root / "scripts/import_external_catalog_sources.py").write_text("from pathlib import Path\nPath('imported').touch()\n")
            vendir = root / "vendir"
            vendir.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nassert "--locked" in sys.argv\np=Path(sys.argv[sys.argv.index("--lock-file")+1])\nassert p.read_text()=="original locked SHA and descriptive tag\\n"\np.write_text("same SHA with different descriptive tag\\n")\n')
            vendir.chmod(0o755)
            script = Path(__file__).resolve().parents[1] / "sync_external_catalog_sources.sh"
            subprocess.run(["bash", str(script), "--locked"], cwd=root,
                           env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}, check=True, capture_output=True)
            self.assertEqual(lock.read_text(), "original locked SHA and descriptive tag\n")
            self.assertTrue((root / "imported").exists())


if __name__ == "__main__":
    unittest.main()
