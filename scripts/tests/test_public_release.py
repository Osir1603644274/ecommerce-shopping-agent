"""Focused safety checks for the public release planner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from argparse import Namespace


MODULE = Path(__file__).resolve().parents[1] / "public_release.py"
SPEC = importlib.util.spec_from_file_location("public_release", MODULE)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_diff_tracks_changes_and_deletions_without_touching_base():
    old = {"keep.txt": "same", "replace.txt": "old", "removed.txt": "old"}
    new = {"keep.txt": "same", "replace.txt": "new", "added.txt": "new"}
    assert release.diff_files(old, new) == (["added.txt", "replace.txt"], ["removed.txt"])


def test_sync_tree_preserves_jsonl_fixture_and_prunes_cache():
    with TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        stage = Path(temporary) / "stage"
        source = root / "agent" / "tests"
        source.mkdir(parents=True)
        (source / "test_case.py").write_text("assert True\n", encoding="utf-8")
        (source / "fixture.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "secret.py").write_text("stale", encoding="utf-8")
        (stage / "agent" / "tests").mkdir(parents=True)
        (stage / "agent" / "tests" / "stale.py").write_text("old", encoding="utf-8")
        with patch.object(release, "ROOT", root):
            release.sync_tree("agent/tests", stage)
        assert (stage / "agent" / "tests" / "fixture.jsonl").is_file()
        assert (stage / "agent" / "tests" / "test_case.py").is_file()
        assert not (stage / "agent" / "tests" / "stale.py").exists()
        assert not (stage / "agent" / "tests" / "__pycache__").exists()


def test_candidate_replaces_local_secret_and_rejects_known_token_shape():
    with TemporaryDirectory() as temporary:
        stage = Path(temporary)
        candidate = stage / "config.txt"
        candidate.write_text("password=local-private-value\n", encoding="utf-8")
        with patch.object(release, "local_secret_values", return_value=[b"local-private-value"]):
            assert release.redact_and_scan(stage) == 1
        assert b"local-private-value" not in candidate.read_bytes()
        candidate.write_text("ghp_" + "A" * 35, encoding="utf-8")
        with patch.object(release, "local_secret_values", return_value=[]):
            try:
                release.redact_and_scan(stage)
            except RuntimeError as exc:
                assert "GitHub token" in str(exc)
            else:
                raise AssertionError("token-shaped candidate was accepted")


def test_base_file_change_is_rejected_before_planning():
    with TemporaryDirectory() as temporary:
        base = Path(temporary)
        file = base / "README.md"
        file.write_text("published\n", encoding="utf-8")
        release.write_manifest(base)
        state = {"manifestSha256": release.sha256((base / release.MANIFEST).read_bytes())}
        assert "README.md" in release.verified_base(base, state)
        file.write_text("tampered\n", encoding="utf-8")
        try:
            release.verified_base(base, state)
        except RuntimeError as exc:
            assert "differ from their manifest" in str(exc)
        else:
            raise AssertionError("tampered base was accepted")


def test_publish_uses_verified_base_and_non_force_ref_update():
    with TemporaryDirectory() as temporary:
        run = Path(temporary)
        stage = run / "stage"
        stage.mkdir()
        content = b"new public source\n"
        (stage / "README.md").write_bytes(content)
        data = {
            "repository": "owner/repo",
            "baseSha": "base-commit",
            "changed": ["README.md"],
            "removed": [],
            "allowDelete": False,
            "files": {"README.md": release.sha256(content)},
        }

        class FakeGitHub:
            def __init__(self, repository):
                assert repository == "owner/repo"
                self.main = "base-commit"
                self.ref_payload = None

            def main_sha(self):
                return self.main

            def api(self, route, payload=None, method="POST", retry=True):
                if route == "git/commits/base-commit":
                    return {"tree": {"sha": "base-tree"}}
                if route == "git/blobs":
                    return {"sha": release.git_blob_sha(payload["content"].encode())}
                if route == "git/trees":
                    assert payload["base_tree"] == "base-tree"
                    assert payload["tree"][0]["sha"] == release.git_blob_sha(content)
                    return {"sha": "new-tree"}
                if route == "git/commits":
                    assert payload["parents"] == ["base-commit"]
                    return {"sha": "new-commit"}
                if route == "git/refs/heads/main":
                    assert method == "PATCH" and retry is False
                    self.ref_payload = payload
                    self.main = payload["sha"]
                    return {"object": {"sha": self.main}}
                raise AssertionError(f"unexpected API call: {route}")

        fake = FakeGitHub("owner/repo")
        with (
            patch.object(release, "plan_inputs", return_value=(data, run, stage)),
            patch.object(release, "scope_and_state", return_value=({"repository": "owner/repo"}, {"baseSha": "base-commit"})),
            patch.object(release, "GitHub", return_value=fake),
            patch.object(release, "verify") as verified,
        ):
            release.publish(Namespace(plan=str(run / "plan.json"), message="test publication"))
        assert fake.ref_payload == {"sha": "new-commit", "force": False}
        assert release.read_json(run / "pending.json")["tree"] == "new-tree"
        verified.assert_called_once()


def test_publish_rejects_remote_change_before_upload():
    with TemporaryDirectory() as temporary:
        run = Path(temporary)
        stage = run / "stage"
        stage.mkdir()
        data = {"repository": "owner/repo", "baseSha": "old", "changed": ["README.md"], "removed": [], "allowDelete": False}

        class MovedGitHub:
            def main_sha(self):
                return "someone-elses-commit"

            def api(self, *args, **kwargs):
                raise AssertionError("publication API must not be called")

        with (
            patch.object(release, "plan_inputs", return_value=(data, run, stage)),
            patch.object(release, "scope_and_state", return_value=({"repository": "owner/repo"}, {"baseSha": "old"})),
            patch.object(release, "GitHub", return_value=MovedGitHub()),
        ):
            try:
                release.publish(Namespace(plan=str(run / "plan.json"), message="test"))
            except RuntimeError as exc:
                assert "changed after planning" in str(exc)
            else:
                raise AssertionError("remote change was accepted")
