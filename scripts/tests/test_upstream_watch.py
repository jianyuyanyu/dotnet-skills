from __future__ import annotations

import importlib.util
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "upstream_watch.py"
SPEC = importlib.util.spec_from_file_location("upstream_watch", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Failed to load upstream_watch module from {MODULE_PATH}")
UPSTREAM_WATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPSTREAM_WATCH)


class UpstreamWatchTests(unittest.TestCase):
    def test_fetch_github_release_uses_latest_published_matching_release(self) -> None:
        releases = [
            {
                "id": 8,
                "tag_name": "v8.0.29",
                "published_at": "2026-07-14T22:35:59Z",
                "created_at": "2026-07-15T02:00:00Z",
                "html_url": "https://example.com/v8.0.29",
                "draft": False,
                "prerelease": False,
            },
            {
                "id": 10,
                "tag_name": "v10.0.10",
                "published_at": "2026-07-15T00:37:49Z",
                "created_at": "2026-07-15T01:00:00Z",
                "html_url": "https://example.com/v10.0.10",
                "draft": False,
                "prerelease": False,
            },
            {
                "id": 11,
                "tag_name": "v11.0.0-preview.4",
                "published_at": "2026-07-16T00:00:00Z",
                "created_at": "2026-07-16T00:00:00Z",
                "html_url": "https://example.com/v11.0.0-preview.4",
                "draft": False,
                "prerelease": True,
            },
        ]
        watch = {
            "id": "dotnet-release",
            "owner": "dotnet",
            "repo": "runtime",
        }

        with mock.patch.object(UPSTREAM_WATCH, "gh_api", return_value=releases):
            snapshot = UPSTREAM_WATCH.fetch_github_release(watch, token=None)

        self.assertEqual(snapshot["value"], "v10.0.10")
        self.assertEqual(snapshot["source_url"], "https://example.com/v10.0.10")
