from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

import actions_runner
import noti


def job(number, location="New York, NY"):
    return noti.Job(f"https://simplify.jobs/p/{number}", f"Company {number}", "Software Engineering Intern",
                    location, "Software Engineering", f"https://jobs.example/{number}", "0d", "")


class LocationTests(unittest.TestCase):
    def test_canadian_locations_are_excluded(self):
        for location in ("Toronto, ON, Canada", "Vancouver, BC", "Remote in Canada", "Canada",
                         "Toronto", "Montréal", "Calgary, Alberta", "Remote in Ontario", "Ottawa, ON"):
            with self.subTest(location=location):
                self.assertIsNone(noti.eligible_job(job(1, location)))

    def test_california_and_ambiguous_remote_remain(self):
        for location in ("San Francisco, CA", "Ontario, CA", "Vancouver, WA", "Remote",
                         "Remote in USA", "London, UK", "New York, NY", "Richmond, VA"):
            with self.subTest(location=location):
                self.assertEqual(noti.eligible_job(job(1, location)).location, location)

    def test_mixed_location_retains_non_canadian_options(self):
        filtered = noti.eligible_job(job(1, "Toronto, ON, Canada; NYC; Vancouver, BC; Austin, TX"))
        self.assertEqual(filtered.location, "NYC; Austin, TX")


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = noti.Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.summary = Path(self.directory.name) / "summary.md"
        self.config = {"digest_summary_path": self.summary, "digest_url": "https://github.com/me/noti/actions/runs/123",
                       "database": Path(self.directory.name) / "noti.sqlite3"}
        self.store.ingest([job(0)])

    def test_forty_jobs_make_one_alert_with_full_report(self):
        self.store.ingest([job(i) for i in range(41)])
        with patch("noti.publish") as publish:
            noti.deliver(self.config, self.store)
            publish.assert_called_once()
            payload = publish.call_args.args[1]
            self.assertEqual(payload["title"], "40 new internships")
            self.assertEqual(payload["click"], self.config["digest_url"])
            self.assertLess(len(payload["message"].encode()), 4096)
        report = self.summary.read_text()
        for i in range(1, 41):
            self.assertIn(f"[Apply](https://jobs.example/{i})", report)
        self.assertEqual((Path(self.directory.name) / "latest-digest.md").read_text(), report)
        self.assertEqual(self.store.status()["pending_notifications"], 0)
        with patch("noti.publish") as publish:
            noti.deliver(self.config, self.store)
            publish.assert_not_called()

    def test_filter_applies_before_digest_count(self):
        self.store.ingest([job(0), job(1, "Canada"), job(2, "Toronto, ON; SF"), job(3, "Austin, TX")])
        with patch("noti.publish") as publish:
            noti.deliver(self.config, self.store)
            self.assertEqual(publish.call_args.args[1]["title"], "2 new internships")
        report = self.summary.read_text()
        self.assertNotIn("Company 1", report)
        self.assertNotIn("Toronto", report)
        self.assertIn("Location: SF", report)

    def test_canada_only_batch_sends_nothing_and_stays_seen(self):
        self.store.ingest([job(0), job(1, "Canada")])
        with patch("noti.publish") as publish:
            noti.deliver(self.config, self.store)
            publish.assert_not_called()
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.store.status()["known_jobs"], 2)
        self.assertEqual(self.store.status()["pending_notifications"], 0)

    def test_old_canadian_queue_filtered_even_on_unchanged_fetch(self):
        # Represents a pre-upgrade queue restored from the Actions state branch.
        self.store.ingest([job(0), job(1), job(2)])
        with self.store.db:
            self.store.put("source_url", noti.SOURCE)
            self.store.db.execute("UPDATE jobs SET payload = ? WHERE key = ?",
                                  (json.dumps(asdict(job(1, "Canada"))), job(1).key))
            self.store.db.execute("UPDATE jobs SET payload = ? WHERE key = ?",
                                  (json.dumps(asdict(job(2, "Toronto, ON; SF"))), job(2).key))
        restored = noti.Store(":memory:")
        self.addCleanup(restored.db.close)
        actions_runner.restore(restored, actions_runner.snapshot(self.store))
        with patch("noti.fetch", return_value=None), patch("noti.publish") as publish:
            self.assertTrue(noti.check({}, restored))
            noti.deliver(self.config, restored)
            publish.assert_called_once()
            payload = publish.call_args.args[1]
            self.assertEqual(payload["title"], "New internship: Company 2")
            self.assertNotIn("Toronto", payload["message"])
        self.assertEqual(restored.status()["pending_notifications"], 0)

    def test_failed_digest_retries_whole_batch_after_restore(self):
        self.store.ingest([job(i) for i in range(41)])
        with self.store.db:
            self.store.put("source_url", noti.SOURCE)
        with patch("noti.time.time", return_value=1000), patch("noti.publish", side_effect=URLError("offline")):
            noti.deliver(self.config, self.store)
        restored = noti.Store(":memory:")
        self.addCleanup(restored.db.close)
        actions_runner.restore(restored, actions_runner.snapshot(self.store))
        self.assertEqual(restored.status()["pending_notifications"], 40)
        with patch("noti.time.time", return_value=1010), patch("noti.publish") as publish:
            noti.deliver(self.config, restored)
            publish.assert_not_called()
        with patch("noti.time.time", return_value=1061), patch("noti.publish") as publish:
            noti.deliver(self.config, restored)
            publish.assert_called_once()
            self.assertEqual(publish.call_args.args[1]["title"], "40 new internships")
        self.assertEqual(restored.status()["pending_notifications"], 0)

    def test_report_failure_does_not_acknowledge_or_send_batch(self):
        self.store.ingest([job(0), job(1), job(2)])
        with patch("noti.save_digest_report", side_effect=OSError("disk full")), patch("noti.publish") as publish:
            noti.deliver(self.config, self.store)
            publish.assert_not_called()
        self.assertEqual(self.store.status()["pending_notifications"], 2)

    def test_long_unicode_preview_fits_without_losing_report_entries(self):
        jobs = [replace(job(i), company="会社" * 200, role="Intern 🧑‍💻" * 200) for i in range(40)]
        payload = noti.digest_notification(jobs, self.config["digest_url"])
        self.assertLess(len(payload["message"].encode()), 4096)
        self.assertEqual(noti.digest_report(jobs).count("[Apply]"), 40)


if __name__ == "__main__":
    unittest.main()
