"""One GitHub Actions check with durable, secret-free state on noti-state.

The workflow serializes runs. Git pushes are fast-forward only, so a concurrent
writer cannot silently overwrite history. No Git checkout/index is modified.
"""

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading

import noti


STATE_REF = "refs/heads/noti-state"
META_KEYS = {"initialized", "source_url", "etag", "modified", "fetch_resume",
             "fetch_attempts", "delivery_resume"}


def snapshot(store):
    # Exclude transient timestamps/errors, and never serialize application config.
    # Keeping unchanged snapshots identical avoids a commit every five minutes.
    meta = {key: store.get(key) for key in sorted(META_KEYS) if store.get(key) != ""}
    jobs = []
    for row in store.db.execute("SELECT * FROM jobs ORDER BY key"):
        jobs.append({"job": json.loads(row["payload"]), "sent": row["sent"],
                     "attempts": row["attempts"], "next_attempt": row["next_attempt"]})
    return json.dumps({"version": 1, "meta": meta, "jobs": jobs}, ensure_ascii=False,
                      sort_keys=True, indent=2) + "\n"


def restore(store, text):
    state = json.loads(text)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("meta"), dict) or not isinstance(state.get("jobs"), list)):
        raise ValueError("Invalid saved state; refusing to start a fresh baseline")
    meta = state["meta"]
    if (set(meta) - META_KEYS or any(not isinstance(v, str) for v in meta.values())
            or meta.get("source_url") != noti.SOURCE
            or meta.get("initialized") not in {None, "1"}):
        raise ValueError("Invalid saved state metadata")
    if meta.get("initialized") == "1" and not state["jobs"]:
        raise ValueError("Initialized state is missing its job history")
    if state["jobs"] and meta.get("initialized") != "1":
        raise ValueError("Job history is missing its initialization marker")
    with store.db:
        if store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
            raise ValueError("Restore requires an empty temporary database")
        for key, value in meta.items():
            store.put(key, value)
        for record in state["jobs"]:
            job = noti.Job(**record["job"])
            if (not isinstance(job.key, str) or not job.key
                    or record["sent"] not in (0, 1)
                    or type(record["attempts"]) is not int or record["attempts"] < 0
                    or not isinstance(record["next_attempt"], (int, float))):
                raise ValueError("Invalid saved job")
            store.db.execute(
                "INSERT INTO jobs (key, payload, sent, attempts, next_attempt) VALUES (?, ?, ?, ?, ?)",
                (job.key, json.dumps(asdict(job), ensure_ascii=False), record["sent"],
                 record["attempts"], record["next_attempt"]))


class StateBranch:
    def __init__(self, directory):
        self.directory = directory
        self.head = None
        self.saved = None

    def git(self, *args, input=None):
        result = subprocess.run(
            ["git", "-c", "user.name=github-actions[bot]", "-c",
             "user.email=41898282+github-actions[bot]@users.noreply.github.com", *args],
            cwd=self.directory, input=input, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        if result.returncode:
            # Git errors may include credential-bearing URLs; don't log raw stderr.
            raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}). "
                               "Check origin access, contents: write permission, and noti-state branch rules.")
        return result.stdout

    def load(self, store):
        remote = self.git("ls-remote", "--heads", "origin", STATE_REF).strip()
        if not remote:
            logging.info("No noti-state branch yet; first successful check will create a silent baseline")
            return
        # Fetch the branch's current tip; a network failure must not look like missing state.
        self.git("fetch", "--no-tags", "--depth=1", "origin", STATE_REF)
        self.head = self.git("rev-parse", "FETCH_HEAD").strip()
        self.saved = self.git("show", f"{self.head}:state.json")
        restore(store, self.saved)

    def save(self, store):
        text = snapshot(store)
        if text == self.saved:
            return
        blob = self.git("hash-object", "-w", "--stdin", input=text).strip()
        tree = self.git("mktree", input=f"100644 blob {blob}\tstate.json\n").strip()
        parent = ["-p", self.head] if self.head else []
        commit = self.git("commit-tree", tree, *parent, "-m", "Save internship watcher state").strip()
        # An ordinary push rejects concurrent/non-fast-forward changes. Never force push.
        self.git("push", "origin", f"{commit}:{STATE_REF}")
        self.head, self.saved = commit, text
        logging.info("Saved job history and notification queue to noti-state")


def run(config, store, branch, stop):
    branch.load(store)
    with store.db:
        store.put("source_url", noti.SOURCE)
    ok = noti.check(config, store)
    # Persist discoveries BEFORE any job notification. If this push fails, stop.
    branch.save(store)
    try:
        # One digest request uses a bounded HTTP timeout, leaving time to save state.
        noti.deliver(config, store, stop, max_seconds=120)
    finally:
        branch.save(store)
    status = store.status()
    summary = (f"GitHub check: {'successful' if ok else 'failed or waiting to retry'}\n"
               f"Known jobs: {status['known_jobs']}\n"
               f"Queued notifications: {status['pending_notifications']}\n")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write("```text\n" + summary + "```\n")
    return 0 if ok and not status["pending_notifications"] else 1


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    topic = os.environ.get("NTFY_TOPIC", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        logging.error("Set the NTFY_TOPIC Actions secret to your existing ntfy topic name (not its URL)")
        return 1
    config = {"source_url": noti.SOURCE, "ntfy_server": "https://ntfy.sh", "ntfy_topic": topic,
              "ntfy_token": os.environ.get("NTFY_TOKEN", ""), "poll_seconds": 300}
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        config["digest_summary_path"] = os.environ["GITHUB_STEP_SUMMARY"]
        config["digest_url"] = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                                f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    try:
        if os.environ.get("SEND_TEST_NOTIFICATION") == "true":
            noti.publish(config, {"title": "GitHub Actions is connected",
                                  "message": "Your internship watcher can send notifications from GitHub."})
            logging.info("Test notification accepted by ntfy; check your phone")
        with tempfile.TemporaryDirectory(prefix="noti-actions-") as directory:
            store = noti.Store(Path(directory) / "noti.sqlite3")
            try:
                return run(config, store, StateBranch(Path.cwd()), threading.Event())
            finally:
                store.db.close()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error,
            subprocess.TimeoutExpired, noti.HTTPException) as error:
        logging.error("%s", noti.error_message(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
