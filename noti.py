#!/usr/bin/env python3
"""Watch Simplify's Summer 2027 listings and send new jobs to ntfy.

Python 3.10+, standard library only. Run `python3 noti.py --help`.
"""

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SOURCE = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"
LISTINGS_URL = "https://github.com/SimplifyJobs/Summer2027-Internships"
LOG = logging.getLogger("noti")
HEADERS = ["Company", "Role", "Location", "Application", "Age"]
FLAGS = {"🛂": "No visa sponsorship", "🇺🇸": "US citizenship required",
         "🎓": "Advanced degree required"}


def clean(text):
    return " ".join(text.split())


def canonical_url(url):
    """Remove known tracking fields; preserve job-identifying query parameters."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")
             and not (k.lower() == "ref" and v.lower() == "simplify")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(sorted(query)), ""))


def web_url(url):
    parts = urlsplit(url)
    return parts.scheme in {"https", "http"} and bool(parts.hostname)


@dataclass(frozen=True)
class Job:
    key: str
    company: str
    role: str
    location: str
    category: str
    url: str
    age: str
    restrictions: str
    closed: bool = False


def canadian_location(location):
    """Recognize explicit Canadian countries/provinces without mistaking CA for Canada."""
    location = clean(location)
    if re.search(r"\b(canada|canadian)\b|🇨🇦", location, re.I):
        return True
    provinces = r"AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT"
    if re.search(rf"(?:^|,\s*|\(\s*)(?:{provinces})(?:\s*\)|$)", location, re.I):
        return True
    full_names = (r"Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland(?: and Labrador)?|"
                  r"Nova Scotia|Northwest Territories|Nunavut|Ontario|Prince Edward Island|"
                  r"Qu[eé]bec|Saskatchewan|Yukon")
    if re.search(rf"(?:^|,\s*|\bin\s+)(?:{full_names})$", location, re.I):
        return True
    # Bare city names sometimes appear without a country/province in upstream data.
    return location.casefold() in {"toronto", "montreal", "montréal", "ottawa", "vancouver",
                                  "calgary", "edmonton", "winnipeg", "saskatoon", "regina"}


def eligible_job(job):
    if job.closed:
        return None
    locations = [clean(part) for part in job.location.split(";") if clean(part)]
    remaining = [part for part in locations if not canadian_location(part)]
    return replace(job, location="; ".join(remaining)) if remaining else None


class TableParser(HTMLParser):
    """Collect table cells, including links whose contents are button images."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.summary = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self.row is not None:
                raise ValueError("Unclosed table row")
            self.row = []
        elif tag in {"td", "th"}:
            if self.row is None or self.cell is not None:
                raise ValueError("Malformed table cell")
            self.cell = {"text": [], "links": [], "header": tag == "th"}
        elif self.cell is not None:
            if tag == "summary":
                self.summary = True
            elif tag == "br":
                self.cell["text"].append("; ")
            elif tag == "a":
                href = dict(attrs).get("href", "")
                if web_url(href):
                    self.cell["links"].append(href)

    def handle_endtag(self, tag):
        if tag == "summary":
            self.summary = False
        elif tag in {"td", "th"} and self.cell is not None:
            self.cell["text"] = clean("".join(self.cell["text"]))
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                raise ValueError("Unclosed table cell")
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.summary:
            self.cell["text"].append(data)


def parse_jobs(readme):
    if "TABLE_START" in readme and "TABLE_END" not in readme:
        raise ValueError("Truncated README; missing end-of-list marker")
    jobs = {}
    recognized = 0
    # Split by Markdown headings so every table retains its category.
    sections = re.split(r"(?m)^\s*## (.+?)\s*$", readme)
    for i in range(1, len(sections), 2):
        category, body = sections[i:i + 2]
        if "Internship Roles" not in category:
            continue
        tables = re.findall(r"<table\b[^>]*>.*?</table>", body, re.S | re.I)
        if len(tables) != len(re.findall(r"<table\b", body, re.I)):
            raise ValueError("Incomplete job table; keeping previous state")
        if not tables:
            raise ValueError(f"Missing job table in {category}")
        for table in tables:
            parser = TableParser()
            parser.feed(table)
            parser.close()
            if parser.row is not None or parser.cell is not None:
                raise ValueError("Incomplete table row")
            if not parser.rows or [c["text"] for c in parser.rows[0]] != HEADERS:
                raise ValueError("Job table headers changed; parser needs updating")
            recognized += 1
            previous_company = ""
            for row in parser.rows[1:]:
                if len(row) != 5:
                    raise ValueError("Job table column count changed")
                company, role, location, application, age = [c["text"] for c in row]
                if company == "↳":
                    company = previous_company
                else:
                    previous_company = company
                if not company or not role or not location:
                    raise ValueError("Job is missing company, role, or location")
                links = row[3]["links"]
                closed = "🔒" in application
                if closed and not links:
                    # No application to notify about or stable ID to persist.
                    continue
                if not links:
                    raise ValueError(f"Missing application link for {company}: {role}")
                simplify = [u for u in links if urlsplit(u).hostname == "simplify.jobs"
                            and urlsplit(u).path.startswith("/p/")]
                direct = [u for u in links if u not in simplify]
                key = canonical_url((simplify or links)[0])
                url = canonical_url((direct or simplify)[0])
                restrictions = "; ".join(v for k, v in FLAGS.items() if k in company + role)
                company = clean(company.replace("🔥", ""))
                jobs[key] = Job(key, company, role, location, clean(category), url,
                                age, restrictions, closed)
    if not recognized or not jobs:
        raise ValueError("No jobs parsed; refusing to replace previous state")
    return list(jobs.values())


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0
            );
        """)

    def get(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def ingest(self, jobs, etag="", modified=""):
        baseline = not self.get("initialized")
        added = 0
        with self.db:
            for job in jobs:
                eligible = eligible_job(job)
                skipped = eligible is None
                job = eligible or job
                payload = json.dumps(asdict(job), ensure_ascii=False)
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO jobs (key, payload, sent) VALUES (?, ?, ?)",
                    (job.key, payload, int(baseline or skipped))).rowcount
                added += inserted
                if not inserted:
                    # Refresh unsent details without re-alerting on edits/reordering.
                    self.db.execute("UPDATE jobs SET payload = ?, sent = MAX(sent, ?) WHERE key = ?",
                                    (payload, int(skipped), job.key))
            self.put("initialized", "1")
            self.put("etag", etag)
            self.put("modified", modified)
            self.put("last_success", time.time())
        LOG.info("%s: %d jobs parsed, %d %s", "Baseline saved" if baseline else "Checked",
                 len(jobs), added, "saved silently" if baseline else "new jobs")

    def pending(self, now):
        return self.db.execute(
            "SELECT * FROM jobs WHERE sent = 0 AND next_attempt <= ? ORDER BY rowid", (now,)
        ).fetchall()

    def status(self):
        return {"initialized": bool(self.get("initialized")),
                "known_jobs": self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "pending_notifications": self.db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE sent = 0").fetchone()[0],
                "last_success_unix": self.get("last_success") or None,
                "fetch_error": self.get("fetch_error") or None,
                "notification_error": self.get("notification_error") or None}


def fetch(config, store=None):
    headers = {"User-Agent": "noti-internship-watcher/1.0", "Accept": "text/plain"}
    if store and store.get("initialized"):
        if store.get("etag"):
            headers["If-None-Match"] = store.get("etag")
        if store.get("modified"):
            headers["If-Modified-Since"] = store.get("modified")
    try:
        with urlopen(Request(config["source_url"], headers=headers), timeout=30) as response:
            body = response.read(10_000_001)
            if len(body) > 10_000_000:
                raise ValueError("README exceeds 10 MB")
            return body.decode("utf-8"), response.headers.get("ETag", ""), response.headers.get("Last-Modified", "")
    except HTTPError as error:
        error.close()
        if error.code == 304:
            return None
        raise


def clip(text, limit):
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit - 3].decode("utf-8", errors="ignore") + "…"


def notification(job):
    lines = [clip(job.role, 700), clip(job.location, 1000), clip(job.category, 200)]
    if job.restrictions:
        lines.append(job.restrictions)
    if job.age:
        lines.append(f"Listing age when detected: {job.age}")
    # Links are also included as a click action and an explicit Apply button.
    return {"title": clip(f"New internship: {job.company}", 200),
            "message": clip("\n".join(lines), 2500), "click": job.url,
            "actions": [{"action": "view", "label": "Apply", "url": job.url}],
            "tags": ["briefcase"], "priority": 3}


def digest_notification(jobs, digest_url=None):
    if len(jobs) == 1:
        return notification(jobs[0])
    lines = []
    for job in jobs:
        line = clip(f"{job.company} — {job.role} | {job.location}", 240)
        if len("\n".join(lines + [line]).encode("utf-8")) > 3000:
            break
        lines.append(line)
    if len(lines) < len(jobs):
        lines.append(f"…and {len(jobs) - len(lines)} more.")
    lines.append("Tap View jobs for all details and application links." if digest_url else
                 "Full details are in latest-digest.md; tap Browse listings to open GitHub.")
    url = digest_url or LISTINGS_URL
    return {"title": f"{len(jobs)} new internships", "message": "\n".join(lines),
            "click": url, "actions": [{"action": "view", "label": "View jobs" if digest_url else
                                       "Browse listings", "url": url}],
            "tags": ["briefcase"], "priority": 3}


def digest_report(jobs):
    def escape(text):
        return re.sub(r"([\\`*_{}\[\]<>#+.!|])", r"\\\1", clean(text))

    lines = [f"## {len(jobs)} new internships", ""]
    for job in jobs:
        url = job.url.replace("(", "%28").replace(")", "%29")
        lines.extend([f"### {escape(job.company)} — {escape(job.role)}", "",
                      f"Location: {escape(job.location)}  ",
                      f"Category: {escape(job.category)}  "])
        if job.restrictions:
            lines.append(f"Eligibility: {escape(job.restrictions)}  ")
        if job.age:
            lines.append(f"Listing age: {escape(job.age)}  ")
        lines.extend([f"[Apply]({url})", ""])
    return "\n".join(lines) + "\n"


def save_digest_report(config, jobs):
    report = digest_report(jobs)
    if config.get("digest_summary_path"):
        with open(config["digest_summary_path"], "a", encoding="utf-8") as file:
            file.write(report)
    if config.get("database"):
        path = Path(config["database"]).parent / "latest-digest.md"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(report, encoding="utf-8")
        temporary.replace(path)


def publish(config, payload):
    headers = {"Content-Type": "application/json", "User-Agent": "noti-internship-watcher/1.0"}
    if config.get("ntfy_token"):
        headers["Authorization"] = "Bearer " + config["ntfy_token"]
    body = json.dumps({**payload, "topic": config["ntfy_topic"]}, ensure_ascii=False).encode("utf-8")
    request = Request(config["ntfy_server"].rstrip("/") + "/", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read(65536))
            if not isinstance(result, dict) or result.get("event") != "message" or not result.get("id"):
                raise ValueError("Notification server did not acknowledge a message")
    except HTTPError as error:
        error.close()
        raise


def error_message(error):
    # Do not log full request URLs or server bodies (topic/token may be present).
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return "Network request failed"
    return str(error)


def retry_delay(error, attempts, now):
    delay = min(3600, 60 * 2 ** min(attempts, 6))
    if isinstance(error, HTTPError):
        value = error.headers.get("Retry-After", "") if error.headers else ""
        if value:
            try:
                delay = max(delay, float(value))
            except ValueError:
                try:
                    delay = max(delay, parsedate_to_datetime(value).timestamp() - now)
                except (ValueError, TypeError, OverflowError):
                    pass
    return delay


NETWORK_ERRORS = (OSError, ValueError, HTTPException)


def deliver(config, store, stop=None, max_seconds=None):
    now = time.time()
    # Apply the new filter to old queues too, including restored Actions state and 304s.
    with store.db:
        for row in store.pending(float("inf")):
            job = eligible_job(Job(**json.loads(row["payload"])))
            if job is None:
                store.db.execute("UPDATE jobs SET sent = 1 WHERE key = ?", (row["key"],))
            else:
                store.db.execute("UPDATE jobs SET payload = ? WHERE key = ?",
                                 (json.dumps(asdict(job), ensure_ascii=False), row["key"]))
    if float(store.get("delivery_resume", "0")) > now:
        return
    rows = store.pending(now)
    if not rows or (stop and stop.is_set()) or (max_seconds is not None and max_seconds <= 0):
        return
    jobs = [Job(**json.loads(row["payload"])) for row in rows]
    try:
        save_digest_report(config, jobs)
        publish(config, digest_notification(jobs, config.get("digest_url")))
    except NETWORK_ERRORS as error:
        resume = time.time() + retry_delay(error, max(row["attempts"] for row in rows), time.time())
        with store.db:
            store.db.executemany("UPDATE jobs SET attempts = attempts + 1, next_attempt = ? WHERE key = ?",
                                 [(resume, row["key"]) for row in rows])
            store.put("delivery_resume", resume)
            store.put("notification_error", error_message(error))
        LOG.error("Digest failed (%s); %d jobs queued for retry", error_message(error), len(rows))
        return
    with store.db:
        store.db.executemany("UPDATE jobs SET sent = 1 WHERE key = ?", [(row["key"],) for row in rows])
        store.put("delivery_resume", "0")
        store.put("notification_error", "")
    LOG.info("One notification accepted for %d new jobs", len(jobs))


def check(config, store):
    if float(store.get("fetch_resume", "0")) > time.time():
        LOG.info("Waiting before retrying GitHub")
        return False
    try:
        result = fetch(config, store)
        if result is not None:
            body, etag, modified = result
            store.ingest(parse_jobs(body), etag, modified)
        else:
            LOG.info("Listings unchanged")
        with store.db:
            store.put("last_success", time.time())
            store.put("fetch_error", "")
            store.put("fetch_attempts", "0")
            store.put("fetch_resume", "0")
        return True
    except NETWORK_ERRORS as error:
        with store.db:
            store.put("fetch_error", error_message(error))
            attempts = int(store.get("fetch_attempts", "0"))
            store.put("fetch_attempts", attempts + 1)
            store.put("fetch_resume", time.time() + retry_delay(error, attempts, time.time()))
        LOG.error("GitHub check failed (%s); keeping history", error_message(error))
        return False


@contextmanager
def locked_store(path):
    import fcntl  # Linux/macOS; service deployment targets Linux.
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another watcher is already using this database") from None
        store = Store(path)
        try:
            yield store
        finally:
            store.db.close()


def load_config(path):
    config = json.loads(path.read_text())
    for field in ("source_url", "ntfy_server"):
        if not web_url(config[field]) or urlsplit(config[field]).scheme != "https":
            raise ValueError(f"{field} must be an HTTPS URL")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config["ntfy_topic"]):
        raise ValueError("ntfy_topic must contain 1–64 letters, digits, underscores, or hyphens")
    interval = config["poll_seconds"]
    if type(interval) is not int or interval < 30:
        raise ValueError("poll_seconds must be an integer of at least 30")
    config["database"] = path.parent / config["database"]
    config["ntfy_token"] = os.environ.get("NTFY_TOKEN", config.get("ntfy_token", ""))
    return config


def init_config(path):
    config = {"source_url": SOURCE, "poll_seconds": 300, "database": "data/noti.sqlite3",
              "ntfy_server": "https://ntfy.sh", "ntfy_topic": "internships-" + secrets.token_hex(16),
              "ntfy_token": ""}
    # Exclusive creation prevents accidentally replacing a subscribed topic.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    print(f"Created {path}\nIn the ntfy phone app, subscribe to:\n{config['ntfy_server']}/{config['ntfy_topic']}")


def service_unit(config_path):
    def quote(value):
        # Escape systemd specifiers and ExecStart environment interpolation.
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
    print("[Unit]\nDescription=Summer 2027 internship notifications\nAfter=network-online.target\n"
          "\n[Service]\nType=simple\n"
          f"ExecStart={quote(sys.executable)} {quote(Path(__file__).resolve())} --config {quote(config_path)} run\n"
          "Restart=on-failure\nRestartSec=30\nUMask=0077\nNoNewPrivileges=true\n"
          "\n[Install]\nWantedBy=default.target")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config with a random ntfy topic; does not send anything")
    run = sub.add_parser("run", help="Watch continuously (default: every five minutes)")
    run.add_argument("--once", action="store_true", help="Check once and deliver due notifications")
    preview = sub.add_parser("preview", help="Parse listings without changing state or sending anything")
    preview.add_argument("--file", type=Path, help="Use a local README instead of fetching GitHub")
    sub.add_parser("test-notification", help="Send one test notification to the configured topic")
    sub.add_parser("status", help="Show saved state, pending count, and most recent error")
    sub.add_parser("service", help="Print a Linux systemd user service for this installation")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    path = args.config.expanduser().resolve()
    try:
        if args.command == "init":
            init_config(path)
            return 0
        if args.command == "service":
            service_unit(path)
            return 0
        if args.command == "preview" and args.file:
            print(json.dumps([asdict(j) for j in parse_jobs(args.file.read_text())], ensure_ascii=False, indent=2))
            return 0
        config = load_config(path)
        if args.command == "preview":
            print(json.dumps([asdict(j) for j in parse_jobs(fetch(config)[0])], ensure_ascii=False, indent=2))
            return 0
        if args.command == "test-notification":
            publish(config, {"title": "Internship watcher is connected", "message":
                    f"New Summer 2027 listings will be checked every {config['poll_seconds'] // 60} minutes.",
                    "tags": ["white_check_mark"]})
            print("Test accepted by ntfy. Check your phone to confirm delivery.")
            return 0
        if args.command == "status":
            if not config["database"].exists():
                print("Not started. Run: python3 noti.py run")
                return 0
            store = Store(config["database"])
            try:
                print(json.dumps(store.status(), indent=2))
            finally:
                store.db.close()
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        with locked_store(config["database"]) as store:
            if store.get("source_url") not in {"", config["source_url"]}:
                raise ValueError("Source URL changed; use a separate database for a different source")
            with store.db:
                store.put("source_url", config["source_url"])
            LOG.info("Watching every %d seconds", config["poll_seconds"])
            next_check = 0
            while not stop.is_set():
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + config["poll_seconds"]
                    ok = check(config, store)
                deliver(config, store, stop)
                if args.once:
                    return 0 if ok and not store.status()["pending_notifications"] else 1
                stop.wait(min(15, max(0, next_check - time.monotonic())))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, HTTPException) as error:
        LOG.error("%s", error_message(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
