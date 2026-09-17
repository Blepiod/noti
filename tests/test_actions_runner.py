import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import actions_runner as runner
import noti


def job(key="one"):
    return noti.Job(f"https://simplify.jobs/p/{key}", "Example", "SWE Intern", "Remote",
                    "Software Engineering", f"https://jobs.example/{key}", "0d", "")


class SnapshotTests(unittest.TestCase):
    def test_round_trip_preserves_queue_but_excludes_secrets_and_diagnostics(self):
        original = noti.Store(":memory:")
        restored = noti.Store(":memory:")
        self.addCleanup(original.db.close)
        self.addCleanup(restored.db.close)
        original.ingest([job()])
        original.ingest([job(), job("two")], '"etag"', "yesterday")
        with original.db:
            original.put("source_url", noti.SOURCE)
            original.put("ntfy_token", "SECRET_TOKEN")
            original.put("ntfy_topic", "SECRET_TOPIC")
            original.put("notification_error", "SECRET_DIAGNOSTIC")
            original.put("delivery_resume", "1234")
            original.db.execute("UPDATE jobs SET attempts = 2, next_attempt = 1234 WHERE sent = 0")
        text = runner.snapshot(original)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("last_success", text)
        runner.restore(restored, text)
        self.assertEqual(runner.snapshot(restored), text)
        self.assertEqual(restored.status()["pending_notifications"], 1)
        self.assertEqual(restored.pending(1235)[0]["attempts"], 2)
        self.assertEqual(restored.get("etag"), '"etag"')

    def test_corrupt_state_never_becomes_new_baseline(self):
        examples = ["not json", "{}", json.dumps({"version": 1, "meta": {
            "initialized": "1", "source_url": noti.SOURCE}, "jobs": []})]
        for text in examples:
            with self.subTest(text=text):
                store = noti.Store(":memory:")
                try:
                    with self.assertRaises(ValueError):
                        runner.restore(store, text)
                    self.assertFalse(store.get("initialized"))
                finally:
                    store.db.close()

    def test_no_notifications_when_discovery_checkpoint_fails(self):
        store = noti.Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([job()])
        store.ingest([job(), job("two")])
        branch = Mock()
        branch.save.side_effect = RuntimeError("push rejected")
        with patch("noti.check", return_value=True), patch("noti.publish") as publish:
            with self.assertRaisesRegex(RuntimeError, "push rejected"):
                runner.run({}, store, branch, threading.Event())
        publish.assert_not_called()

    def test_delivery_budget_leaves_unsent_jobs_pending(self):
        store = noti.Store(":memory:")
        self.addCleanup(store.db.close)
        store.ingest([job()])
        store.ingest([job(), job("two")])
        with patch("noti.publish") as publish:
            noti.deliver({}, store, max_seconds=0)
            publish.assert_not_called()
        self.assertEqual(store.status()["pending_notifications"], 1)


class StateBranchIntegrationTests(unittest.TestCase):
    """Exercise actual Git pushes/clones locally, without a GitHub account."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="noti-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.checkout = self.root / "checkout"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.checkout))
        (self.checkout / "app.txt").write_text("app code\n")
        self.git(self.checkout, "add", "app.txt")
        self.git(self.checkout, "commit", "-m", "Initial app")
        self.git(self.checkout, "push", "origin", "main")
        self.initial_head = self.git(self.checkout, "rev-parse", "HEAD")

    def git(self, directory, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=directory, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        ).stdout.strip()

    def store(self):
        store = noti.Store(":memory:")
        self.addCleanup(store.db.close)
        return store

    def remote_state(self):
        return json.loads(self.git(self.remote, "show", "noti-state:state.json"))

    def test_fresh_runner_restores_baseline_and_does_not_change_code(self):
        first = self.store()
        branch = runner.StateBranch(self.checkout)
        branch.load(first)
        first.ingest([job()])
        with first.db:
            first.put("source_url", noti.SOURCE)
        branch.save(first)
        head = self.git(self.remote, "rev-parse", "noti-state")
        with first.db:
            first.put("last_success", "another timestamp")
        branch.save(first)
        self.assertEqual(head, self.git(self.remote, "rev-parse", "noti-state"))

        fresh_checkout = self.root / "next-run"
        self.git(self.root, "clone", str(self.remote), str(fresh_checkout))
        second = self.store()
        runner.StateBranch(fresh_checkout).load(second)
        self.assertEqual(second.status()["known_jobs"], 1)
        self.assertEqual(second.status()["pending_notifications"], 0)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_discovery_persisted_before_sending_and_retry_after_fresh_checkout(self):
        first = self.store()
        first.ingest([job()])
        with first.db:
            first.put("source_url", noti.SOURCE)
        runner.StateBranch(self.checkout).save(first)

        def check(config, store):
            store.ingest([job(), job("two")])
            return True

        def offline(config, payload):
            self.assertEqual(sum(r["sent"] == 0 for r in self.remote_state()["jobs"]), 1)
            raise URLError("offline")

        with patch("noti.check", side_effect=check), patch("noti.publish", side_effect=offline):
            code = runner.run({}, self.store(), runner.StateBranch(self.checkout), threading.Event())
        self.assertEqual(code, 1)
        pending = [r for r in self.remote_state()["jobs"] if not r["sent"]]
        self.assertEqual(pending[0]["attempts"], 1)

        fresh_checkout = self.root / "retry"
        self.git(self.root, "clone", str(self.remote), str(fresh_checkout))
        with patch("noti.check", return_value=True), patch("noti.publish") as publish, \
                patch("noti.time.time", return_value=pending[0]["next_attempt"] + 1), \
                patch.object(threading.Event, "wait"):
            code = runner.run({}, self.store(), runner.StateBranch(fresh_checkout), threading.Event())
            publish.assert_called_once()
        self.assertEqual(code, 0)
        self.assertTrue(all(r["sent"] for r in self.remote_state()["jobs"]))

    def test_concurrent_writer_cannot_overwrite_remote_state(self):
        initial = self.store()
        initial.ingest([job()])
        with initial.db:
            initial.put("source_url", noti.SOURCE)
        runner.StateBranch(self.checkout).save(initial)
        left, right = runner.StateBranch(self.checkout), runner.StateBranch(self.checkout)
        left_store, right_store = self.store(), self.store()
        left.load(left_store)
        right.load(right_store)
        left_store.ingest([job(), job("left")])
        right_store.ingest([job(), job("right")])
        left.save(left_store)
        with self.assertRaisesRegex(RuntimeError, "push failed"):
            right.save(right_store)
        keys = [r["job"]["key"] for r in self.remote_state()["jobs"]]
        self.assertIn(job("left").key, keys)
        self.assertNotIn(job("right").key, keys)

    def test_existing_branch_with_missing_snapshot_fails_closed(self):
        self.git(self.checkout, "push", "origin", "main:noti-state")
        store = self.store()
        with self.assertRaisesRegex(RuntimeError, "show failed"):
            runner.StateBranch(self.checkout).load(store)
        self.assertFalse(store.get("initialized"))


if __name__ == "__main__":
    unittest.main()
