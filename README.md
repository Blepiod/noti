# Internship notifications

A Python app that checks [Simplify's Summer 2027 internship list](https://github.com/SimplifyJobs/Summer2027-Internships)
every **five minutes** and pushes each newly discovered job to your phone through
[ntfy](https://ntfy.sh). Requires **Python 3.10+ on Linux or macOS**, with no packages to install.

Notifications contain company, role, locations, category, listing age, and any
listed sponsorship/citizenship/degree restrictions. Tap the notification or its
**Apply** action to open the application. All five categories are included.

For free cloud hosting, use [GitHub Actions](#run-on-github-actions-free). Your
computer can be off once that workflow is configured. Local hosting is optional.

## Run on GitHub Actions (free)

The included `.github/workflows/watch.yml` runs a short check every five minutes.
Use a **public repository** with the standard runner for free execution. Private
repositories have a limited monthly allowance; this schedule can exceed it.
See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

1. **Create an empty public repository** in your GitHub account, for example `noti`.
   Upload the project code to its default branch (`main`). Include:
   - `.github/workflows/watch.yml` (the leading-dot directory matters)
   - `noti.py`, `actions_runner.py`, `README.md`, `.gitignore`, and `tests/`

   **Do not upload `config.json`, `data/`, or `__pycache__/`.** The existing
   `.gitignore` excludes them when using Git. For a new local Git checkout, the
   commands below publish only the named project files. Replace `YOUR_USERNAME`:

   ```sh
   git init -b main
   git add .github noti.py actions_runner.py README.md .gitignore tests
   git commit -m "Add internship notification watcher"
   git remote add origin https://github.com/YOUR_USERNAME/noti.git
   git push -u origin main
   ```

   If this directory is already a Git checkout, use its existing branch and
   remote instead of repeating `git init` / `git remote add`.

2. **Connect ntfy.** In the phone app, subscribe to your existing topic on
   `https://ntfy.sh` and allow notifications. Its name is the `ntfy_topic` value in
   your local `config.json`. If you haven't created one yet, run `python3 noti.py init`.

3. **Add repository secrets.** On GitHub, open **Settings → Secrets and variables
   → Actions → New repository secret**:

   | Secret | Value |
   | --- | --- |
   | `NTFY_TOPIC` | The topic name from `config.json`, without `https://ntfy.sh/` |
   | `NTFY_TOKEN` | Optional; only if your ntfy account/topic requires a token |

   No personal GitHub access token is required. The workflow uses GitHub's built-in
   token with `contents: write` to save its own state. If your organization restricts
   workflow permissions, allow this job to write the `noti-state` branch.
   [GitHub's secret setup instructions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

4. **Start and test.** Open **Actions → Internship alerts → Run workflow**. Select
   the default branch, check **Send a test notification to confirm phone setup**,
   and run it. Confirm the test on your phone. The first successful check creates
   the `noti-state` branch and silently records existing jobs. Subsequent runs send
   new listings automatically. The run summary shows the known-job and queued-message counts.

5. **Stop the local watcher**, if you were running it. For the installed Linux
   service: `systemctl --user disable --now noti.service`. For a terminal process:
   Ctrl+C. Cloud and local histories are separate, so running both can double alerts.

The cloud starts with a fresh baseline; your existing local database is not uploaded.
The workflow uses hosted `ntfy.sh` and a five-minute schedule independently of your
local `config.json`. Change the workflow's cron expression to change cloud frequency.

### Saved state and failures

`noti-state` contains **public job details, delivery flags, and retry metadata** in
`state.json`. On a public repo, this history is public too. It contains no ntfy
topic/token or local configuration. Do not delete the branch: losing it resets the
baseline. No Actions caches or expiring artifacts are used as the source of history.

Each run restores state into temporary SQLite, checks GitHub, pushes discovered
jobs to the state branch **before sending**, then pushes delivery results. Unchanged
state creates no commit. Runs are serialized; conflicting pushes fail instead of
overwriting another run. Protected-branch rules must allow the bot to create and
update `noti-state` directly. Keep your normal protections on `main`.

Failures preserve queued jobs for a later scheduled run, provided GitHub accepts
the state push. A crash or rejected final push after sending can cause duplicate
notifications on retry. Delivery batches have a two-minute budget; remaining items
stay queued. In cloud mode retries happen on subsequent workflow runs, rather than
the local service's 15-second queue loop. A failed check or a nonempty queue makes
the run red so you can inspect it. A missing secret or unreadable/corrupt saved
state stops the run; it is not treated as a new baseline.

The workflow does not run on pull requests or ordinary pushes. The manual test
option sends one test plus performs the normal check. Inspect failures under
**Actions → Internship alerts → the run → Check listings and persist notifications**.
For a push-permission failure, check **Settings → Actions → General → Workflow
permissions**, organization policy, and branch rules. To stop cloud checks, open
the workflow's menu and choose **Disable workflow**.

**Scheduling limits:** GitHub can delay or drop scheduled runs under load. The
cron is offset from the top of the hour (`:02`, `:07`, …) but cannot guarantee
five-minute delivery. Public-repository schedules are disabled after 60 days with
no repository activity; check the Actions page periodically and re-enable if
needed. [GitHub scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Connect your phone

1. Install **ntfy** from the [App Store or Google Play](https://docs.ntfy.sh/subscribe/phone/)
   and allow notifications.
2. In this directory, run:

   ```sh
   python3 noti.py init
   ```

   This creates `config.json` with a randomly generated topic and prints its URL.
   If `config.json` already exists, use its `ntfy_topic`; `init` refuses to replace it.
3. In the phone app, subscribe to that topic on the default server, `https://ntfy.sh`.
4. Send a test, then confirm it appears on your phone:

   ```sh
   python3 noti.py test-notification
   ```

5. Start watching:

   ```sh
   python3 noti.py run
   ```

The **first successful check saves existing jobs silently**. Subsequent checks
notify only about new listings. Keep this process and its computer running with
internet access. Ctrl+C stops it cleanly. To start automatically, use the service below.

The free hosted ntfy service needs no account. Its topics are public: anyone who
knows your topic can read or publish to it. The generated topic has 128 random bits,
but it is not authenticated private access. Keep `config.json` private; it and the
database are ignored by Git. An optional `ntfy_token` (or `NTFY_TOKEN` environment
variable) supports a protected topic on a server/account configured for access control.
Using hosted ntfy sends the job details through that service. See [ntfy's documentation](https://docs.ntfy.sh/).

## Run automatically on Linux

After connecting your phone, generate and install a systemd **user** service.
Run these from this project directory; the generated unit uses absolute paths.

```sh
mkdir -p ~/.config/systemd/user
python3 noti.py service > ~/.config/systemd/user/noti.service
systemctl --user daemon-reload
systemctl --user enable --now noti.service
```

To keep the user service running after logout and start it at boot, enable lingering
(your system may ask for administrator authorization):

```sh
loginctl enable-linger "$USER"
```

The computer still needs to stay powered on and awake. A server or Raspberry Pi
works well. On macOS, the foreground command works; the generated service is Linux-only.

```sh
systemctl --user status noti.service
journalctl --user -u noti.service -f
systemctl --user stop noti.service
```

Don't also run a foreground watcher against the same database. A process lock
prevents concurrent watchers from sending duplicates. Moving the project or Python
executable requires regenerating the unit and reloading systemd.

## Commands and configuration

```sh
python3 noti.py status
python3 noti.py preview                 # Live parsed JSON; no state changes or notifications
python3 noti.py preview --file README-from-github.md
python3 noti.py run --once              # One check, then send due notifications
python3 -m unittest discover -s tests -v
```

`--config /path/to/config.json` goes **before** the command. Configuration fields:

| Field | Default / purpose |
| --- | --- |
| `poll_seconds` | `300` (five minutes) |
| `source_url` | Raw README on the upstream `dev` branch |
| `database` | `data/noti.sqlite3`, relative to the config file |
| `ntfy_server` | `https://ntfy.sh` |
| `ntfy_topic` | Random topic created by `init` |
| `ntfy_token` | Empty; optional access token, overridable with `NTFY_TOKEN` |

Restart the process after changing configuration. Keep the SQLite database when
upgrading or moving hosts: it holds both history and queued notifications. A new
database starts with a fresh silent baseline. `status` reports the last successful
check, queued notifications, and current fetch/delivery errors. `run --once` exits
nonzero if the fetch failed or notifications remain queued.

## How detection and delivery work

- HTTP conditional requests use saved ETag/Last-Modified values. A `304` response
  skips downloading and parsing the README.
- The parser handles HTML job tables, repeated-company arrows, image application
  buttons, and expandable multi-location lists. It rejects missing/changed headers,
  malformed rows, empty results, and missing table/end markers without advancing state.
- Identity comes from the Simplify job URL when present, otherwise the application
  URL. Known tracking parameters are removed while job identifiers are preserved.
  Edits, age changes, sorting, and a known job disappearing/reappearing don't re-alert.
  A posting with a completely new identity is treated as new.
- SQLite commits newly seen jobs and pending messages together. Existing jobs on
  the first run and jobs explicitly marked closed are not notified. Closed entries
  with no application link are skipped.
- Notifications become sent only after ntfy acknowledges them. Failures retry with
  backoff, respect `Retry-After`, and survive restarts. The queue is checked at most
  every 15 seconds independently of the five-minute GitHub check. Messages are
  spaced out to reduce bursts; provider quotas can delay a large batch.
- Delivery is **at least once**: a timeout after ntfy accepted a message, or a crash
  before recording its acknowledgment, can cause a duplicate on retry. An ntfy
  acknowledgment confirms server acceptance, not display on your phone.
- Only the current main README is watched, not off-season or archived files. Jobs
  added and removed entirely between checks can be missed. Alerts reflect when
  the repository lists a job, not necessarily when the employer posted it.

Normally a new listing is detected within five minutes, plus notification delivery
time. Host downtime, provider limits, phone connectivity, and notification settings
can delay alerts. Pending jobs remain queued during outages.

## Why polling instead of events?

An upstream `push` webhook would let GitHub trigger the parser directly, but
[creating repository webhooks requires owner/admin access](https://docs.github.com/en/webhooks/using-webhooks/creating-webhooks).
That would need Simplify's maintainers to cooperate. Forking the repository does
not subscribe the fork to upstream pushes. The public Events API also requires
polling and [can lag by up to six hours](https://docs.github.com/en/rest/activity/events).
Five-minute conditional requests are the practical option with read-only access.
