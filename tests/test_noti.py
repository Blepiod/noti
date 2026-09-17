import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import noti


def row(company="Example &amp; Co", role="SWE Intern", job_id="one", location="Remote", age="0d"):
    return (f"<tr><td>{company}</td><td>{role}</td><td>{location}</td>"
            f'<td><a href="https://jobs.example/apply?id={job_id}&amp;utm_source=Simplify">'
            '<img alt="Apply"></a>'
            f'<a href="https://simplify.jobs/p/{job_id}?utm_source=GHList"><img alt="Simplify"></a>'
            f"</td><td>{age}</td></tr>")


def document(*rows):
    return ("<!-- TABLE_START -->\n## 💻 Software Engineering Internship Roles\n<table><thead><tr>"
            + "".join(f"<th>{h}</th>" for h in noti.HEADERS)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>\n<!-- TABLE_END -->")


class ParserTests(unittest.TestCase):
    def test_real_structure_company_arrows_locations_and_restrictions(self):
        jobs = noti.parse_jobs(document(
            row(company="🔥 <strong>Example &amp; Co</strong>"),
            row(company="↳", role="ML Intern 🎓 🛂 🇺🇸", job_id="two",
                location="<details><summary><b>2 locations</b></summary>SF<br/>NYC</details>")))
        self.assertEqual([j.company for j in jobs], ["Example & Co"] * 2)
        self.assertEqual(jobs[1].location, "SF; NYC")
        self.assertEqual(jobs[0].url, "https://jobs.example/apply?id=one")
        self.assertEqual(jobs[0].key, "https://simplify.jobs/p/one")
        self.assertIn("Advanced degree required", jobs[1].restrictions)
        self.assertIn("No visa sponsorship", jobs[1].restrictions)
        self.assertIn("US citizenship required", jobs[1].restrictions)

    def test_deduplicates_and_ignores_age_in_identity(self):
        jobs = noti.parse_jobs(document(row(), row(age="3d")))
        self.assertEqual(len(jobs), 1)

    def test_fallback_preserves_job_query_parameters(self):
        text = document(row()).replace(
            '<a href="https://simplify.jobs/p/one?utm_source=GHList"><img alt="Simplify"></a>', "")
        self.assertEqual(noti.parse_jobs(text)[0].key, "https://jobs.example/apply?id=one")

    def test_rejects_broken_or_empty_input(self):
        for text in ("Bad Gateway", document(), document(row()).replace("<th>Role</th>", ""),
                     document(row()).replace("</table>", ""), document(row()).replace("TABLE_END", ""),
                     document(row(company="↳"))):
            with self.subTest(text=text[:40]), self.assertRaises(ValueError):
                noti.parse_jobs(text)

    def test_skips_closed_without_links(self):
        text = document(row(), "<tr><td>Old</td><td>Intern</td><td>NYC</td><td>🔒</td><td>2d</td></tr>")
        self.assertEqual(len(noti.parse_jobs(text)), 1)

    def test_notification_keeps_apply_link_and_limits_utf8(self):
        job = noti.parse_jobs(document(row(location="東京" * 2000)))[0]
        payload = noti.notification(job)
        self.assertLessEqual(len(payload["message"].encode()), 2500)
        self.assertEqual(payload["click"], job.url)
        self.assertEqual(payload["actions"][0]["url"], job.url)
        self.assertIn(job.role, payload["message"])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = noti.Store(self.path)
        self.config = {"source_url": noti.SOURCE, "ntfy_server": "https://ntfy.sh",
                       "ntfy_topic": "test-only", "ntfy_token": ""}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def ingest(self, *rows):
        self.store.ingest(noti.parse_jobs(document(*rows)))

    def test_silent_baseline_then_new_job_survives_restart(self):
        self.ingest(row())
        self.assertEqual(self.store.status()["pending_notifications"], 0)
        self.ingest(row(job_id="two"), row(age="1d", role="Updated title"))
        self.store.db.close()
        self.store = noti.Store(self.path)
        self.assertEqual(self.store.status()["pending_notifications"], 1)
        with patch("noti.publish") as send:
            noti.deliver(self.config, self.store)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1]["click"], "https://jobs.example/apply?id=two")
            self.ingest(row(), row(job_id="two", age="4d"))
            noti.deliver(self.config, self.store)
            send.assert_called_once()

    def test_removal_and_reappearance_dont_realert(self):
        self.ingest(row(), row(job_id="two"))
        self.ingest(row(job_id="two"))
        self.ingest(row(), row(job_id="two"))
        self.assertEqual(self.store.status()["pending_notifications"], 0)

    def test_failed_delivery_persists_and_retries(self):
        self.ingest(row())
        self.ingest(row(), row(job_id="two"))
        with patch("noti.time.time", return_value=1000), patch("noti.publish", side_effect=URLError("offline")):
            noti.deliver(self.config, self.store)
        self.store.db.close()
        self.store = noti.Store(self.path)
        with patch("noti.time.time", return_value=1010), patch("noti.publish") as send:
            noti.deliver(self.config, self.store)
            send.assert_not_called()
        with patch("noti.time.time", return_value=1061), patch("noti.publish") as send:
            noti.deliver(self.config, self.store)
            send.assert_called_once()
        self.assertEqual(self.store.status()["pending_notifications"], 0)

    def test_rate_limit_honors_retry_after_for_entire_queue(self):
        self.ingest(row())
        self.ingest(row(), row(job_id="two"), row(job_id="three"))
        error = HTTPError("https://ntfy.sh/", 429, "rate limited", {"Retry-After": "600"}, None)
        with patch("noti.time.time", return_value=1000), patch("noti.publish", side_effect=error) as send:
            noti.deliver(self.config, self.store)
            send.assert_called_once()
        error.close()
        self.assertEqual(float(self.store.get("delivery_resume")), 1600)
        self.assertEqual(self.store.status()["pending_notifications"], 2)

    def test_malformed_fetch_does_not_poison_cache_or_baseline(self):
        with patch("noti.fetch", return_value=("Bad Gateway", "bad-etag", "")):
            self.assertFalse(noti.check(self.config, self.store))
        self.assertFalse(self.store.get("initialized"))
        self.assertFalse(self.store.get("etag"))
        self.assertEqual(self.store.status()["known_jobs"], 0)

    def test_304_and_fetch_failure_do_not_block_pending_deliveries(self):
        self.ingest(row())
        self.ingest(row(), row(job_id="two"))
        with patch("noti.fetch", return_value=None):
            self.assertTrue(noti.check(self.config, self.store))
        with patch("noti.fetch", side_effect=URLError("offline")):
            self.assertFalse(noti.check(self.config, self.store))
        with patch("noti.publish") as send:
            noti.deliver(self.config, self.store)
            send.assert_called_once()
        self.assertEqual(self.store.status()["known_jobs"], 2)

    def test_conditional_fetch_sends_validators(self):
        self.store.ingest(noti.parse_jobs(document(row())), '"version1"', "Yesterday")
        error = HTTPError(noti.SOURCE, 304, "Not Modified", {}, None)
        with patch("noti.urlopen", side_effect=error) as request:
            self.assertIsNone(noti.fetch(self.config, self.store))
        headers = dict(request.call_args.args[0].header_items())
        self.assertEqual(headers["If-none-match"], '"version1"')
        self.assertEqual(headers["If-modified-since"], "Yesterday")

    def test_second_process_cannot_lock_same_database(self):
        with noti.locked_store(self.path):
            with self.assertRaisesRegex(ValueError, "already using"):
                with noti.locked_store(self.path):
                    pass

    def test_publish_json_contract_and_acknowledgment(self):
        payload = noti.notification(noti.parse_jobs(document(row()))[0])
        with patch("noti.urlopen", return_value=io.BytesIO(b'{"id":"123","event":"message"}')) as request:
            noti.publish(self.config, payload)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ntfy.sh/")
        self.assertEqual(sent.method, "POST")
        self.assertEqual(json.loads(sent.data)["topic"], "test-only")
        with patch("noti.urlopen", return_value=io.BytesIO(b'{"error":"bad request"}')):
            with self.assertRaises(ValueError):
                noti.publish(self.config, payload)


if __name__ == "__main__":
    unittest.main()
