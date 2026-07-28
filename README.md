# slotwatch

Discord alerts when a watched booking slot becomes available on a WordPress-backed
recreational-session booking site.

> **Which site?** Deliberately not recorded here. Every target detail — domain, endpoint,
> tab identifiers, venue labels — lives in a runtime **site profile** that is never
> committed. See [Configuration](#configuration).

## What it actually watches for

Written for a case where the wanted sessions were *nearly always already full* — at the
time of writing, 15 of 16 matching slots were sold out — so "new slots dropped" was the
wrong thing to wait for. The signal that actually fires is a **cancellation freeing a
spot**.

| Trigger | Meaning |
|---|---|
| `reopened` | A known slot went from full → available. **The primary signal.** |
| `new_slot` | An unseen slot appeared already bookable — usually a new date entering the rolling window. |
| `health` | The scraper stopped making sense of the page. Exists so silence is never mistaken for "nothing available yet". |

Separately, a **deploy check** runs on every code or config change and posts a Discord
summary of what the bot can currently see. Same reasoning as `health`: a broken secret, an
invalid site profile and drifted markup all look exactly like "nothing has opened up yet",
so each push proves the whole path — tests, real fetch, parse, and Discord delivery.

```
🧪 Deploy check passed
Read 24 slot(s) from <tab> — 7 bookable, 17 full.
Matching your rules right now: 1 bookable slot(s).
Parser anomalies: none.
Active rules: Court 1, late block · Any Intermediate opening
Polling: Mon-Thu 09:00-17:00 every 3 min · Fri 09:00-17:00 every 3 min (2 min 12:00-15:00)
```

Run it yourself with `--test-ping`. It never reads or writes state, and exits non-zero on
a fetch failure or parser anomaly so drift turns the build red too.

Deliberately *not* alerts: a slot selling out, a slot ageing off the rolling window, the
spaces-left count ticking down, or **a session that has already finished**.

That last one needs saying, because the page keeps same-day rows listed — the live
capture carries six rows dated its own capture day. A cancellation can therefore free a
spot in a block that is already over, and a ping you cannot act on is how you learn to
ignore the ones you can. Suppression is measured against the session's **end**, not its
start: a spot freed thirty minutes into a three-hour block is still worth taking.

It fails open in every direction. No local clock, an unreadable date, an unreadable time
— each of those alerts anyway, because staying quiet is the failure this project exists
to prevent. The bot goes silent only when it is certain the session is done. An
unreadable time is also flagged as a parser anomaly, so drift that disables expiry
surfaces through `health` rather than quietly widening what gets announced.

## The daily heartbeat

Two more messages a day, at the edges of the polling window. Neither mentions you — a
phone buzzing twice daily for routine news gets the channel muted, and a muted channel is
how the real alert gets missed.

```
🟢 Polling opened — Mon 27 Jul
Watching for the rest of the day — 160 polls scheduled.
Schedule: Mon-Thu 09:00-17:00 every 3 min · Fri 09:00-17:00 every 3 min (2 min 12:00-15:00)
```

The closing one is the one that earns its place. It reports polls **made** against polls
**scheduled**:

```
🌙 Polling closed — Tue 28 Jul
158 of 160 scheduled polls (99%) — 2 short.
Fetch failures: 2 of those attempts could not read the page.
Alerts sent today: 1.
```

Without that ratio, a silent day is ambiguous — "nothing opened up" and "the watcher was
barely running" look identical, and the second is the failure this whole project is built
around. So a day that lands under `coverage_floor_percent` (default 75) is the one daily
message that *does* mention you:

```
⚠️ Polling closed short — Mon 27 Jul
55 of 160 scheduled polls (34%) — 105 short.
⚠️ Most of the day went unwatched. Scheduled jobs were dropped or started late, so an
opening could have come and gone unseen. A day this short is not a quiet day.
```

Both edges are detected from the clock during an ordinary poll rather than scheduled
separately, since a separate schedule is one more thing that can silently stop. If a job
hits `--max-runtime` before the window shuts, the closing report goes out late — on the
next day's first poll — rather than not at all. Set `daily_summary: false` to switch the
pair off.

## Configuration

Two files, with a firm split:

| File | Committed? | Contains |
|---|---|---|
| `rules.yaml` | yes | What to get pinged about, and when to poll |
| `site.yaml` | **no** (gitignored) | Which site, and how to address it |

```bash
cp site.example.yaml site.yaml   # then fill in the real values
```

In CI, the same YAML is supplied through the `SITE_PROFILE` secret instead of a file, so
no workflow step ever writes target details into the tree.

`rules.yaml` intentionally omits venue names: a single tab is polled, so a `gym` filter
would be redundant anyway, and naming venues in a committed file would defeat the point.

## Setup

1. **Discord webhook**: channel → Edit Channel → Integrations → Webhooks → New Webhook →
   Copy URL.
2. **Repo secrets** (Settings → Secrets and variables → Actions):
   - `SITE_PROFILE` — the full contents of your `site.yaml`.
   - `DISCORD_WEBHOOK_URL` — the webhook URL.
   - `DISCORD_MENTION` — your Discord user ID as `<@123456789>`, so alerts buzz your
     phone instead of sitting unread. (Developer Mode → right-click yourself → Copy User ID.)
3. **Make the repo public.** Actions minutes are unlimited on public repos, which is what
   pays for the self-pacing poller. Secrets stay secret on public repos, and nothing
   identifying lives in the tree. To stay private instead, see the note in
   [poll.yml](.github/workflows/poll.yml) — drop `--loop` for a `*/5` cron and accept
   10–20 minute latency.
4. **Edit [rules.yaml](rules.yaml)** to change what pings you. GitHub's web/mobile editor
   is fine for this.

## Local use

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Windows
pytest -q        # 175 tests, fully offline, no site profile needed
pytest -m live   # opt-in canary; hits the real site once, skips without a profile
```

```bash
# See what it would say, without touching Discord
python -m slotwatch.main --once --dry-run --ignore-window

# Replay a fixture instead of fetching
python -m slotwatch.main --once --dry-run --ignore-window \
  --fixture tests/fixtures/primary_reopened.html

# Deploy check: what can the bot see right now?
python -m slotwatch.main --test-ping --dry-run

# What CI runs
python -m slotwatch.main --loop --max-runtime 115

# Reconcile two runs that both wrote state (what CI's persist step calls on a
# rejected push). Needs neither a site profile nor a webhook.
python -m slotwatch.main --state state/seen.json --merge-state other.json
```

`--dry-run` still writes state (so seeding works) but never posts and never marks events
as notified, so you can re-run it freely.

Exit codes: `0` success · `2` no webhook configured · `3` no site profile found.

## Polling schedule

**Weekdays only**, 3-minute cadence inside 09:00–17:00 America/New_York, tightening to
2 minutes on Fridays 12:00–15:00 — the target's refund rules require weekend cancellations
by mid-afternoon Friday, so freed weekend spots cluster just before that cutoff, which is
what makes a Mon–Fri schedule defensible.

The trade it accepts: a spot freed on Saturday or Sunday morning goes unnoticed until
Monday, by which point a weekend session has already happened. Add `sat`/`sun` to the
windows in `rules.yaml` if you'd rather cover that — sessions that have already finished
are suppressed on their own merits, so weekend polling will not start announcing blocks
that are already over.

That is ~160 requests/day, roughly 6.4 MB — about what a dozen human page views cost,
since one full page load pulls ~500 KB of assets. A request every 3 minutes is slower
than a person casually browsing.

Two scheduling facts worth knowing:

- **Cron does not set the cadence.** Actions schedules drift and get dropped under load,
  so each job paces *itself* (`--loop`) and cron merely starts it. Drift observed in
  practice has been far worse than the 5–15 minutes usually quoted — over an hour on some
  starts — which is exactly why the daily report counts polls rather than assuming them.
- **The window is enforced in Python, not cron.** Cron is UTC-only; a fixed UTC window
  would slide an hour every November. `config.interval_at` uses real `zoneinfo` rules and
  is unit-tested across a DST boundary, as is `config.expected_polls` — the day's target
  is 8 local hours in both DST states, not a fixed number of UTC hours.

### Why CI merges state in Python

Both scheduled workflows commit `state/seen.json` back to the branch, and two runs can
end up writing it at once. Git cannot reconcile that file: runs that each create it
conflict `add/add`, a line-wise merge of a JSON object means nothing, and taking one side
wholesale drops either a sighting (inventing a "new slot" later) or a notified-marker
(re-pinging a slot you were already told about). `state.merge` computes the union
instead — newer sightings win, notified-markers are unioned with the later timestamp —
and CI calls it through `--merge-state`, then commits the result as an ordinary
fast-forward.

The overlap that made this necessary is worth naming: `actions/checkout` defaults to
`github.sha`, which a *scheduled* run pins when the run is **created**. A run queued
behind the `concurrency: poll` lock starts with a SHA that is minutes or hours stale, so
the default checkout silently misses state a previous run already pushed. Both workflows
check out `github.ref_name` instead.

## How the data is read

The obvious page URL cannot see the wanted tab. It only ever server-renders **tab 1** of
a program group, which in this case is a different, permanently empty venue — scraping it
would have looked like "nothing available, ever". Probing the page's filter parameter
across its whole range found no value that renders the wanted tab directly.

Every other tab loads only through the same `POST` the site's own front-end fires on each
tab click, which is what [fetch.py](src/slotwatch/fetch.py) reproduces.

Each row yields a stable numeric id from its radio value (`16194#24#16.0` → `16194`),
used as the dedup key — page text is never hashed. The middle `#` field is ignored: it
was constant on one capture and varied unrelatedly on another.

**On `robots.txt`:** the target disallows its whole WordPress admin directory, where this
endpoint lives. It omits the `Allow: /wp-admin/admin-ajax.php` line WordPress core has
emitted since 4.0, and its other rules match a pre-4.0 template — it reads as legacy
boilerplate rather than a decision about this endpoint, which is designed to be publicly
callable and is hit by every ordinary visitor. No compliant alternative reaches the
wanted tab. The bot stays deliberately gentle: one request per poll, an honest
`User-Agent` (verified served fine — no browser impersonation), jittered intervals, and
backoff on failure. Judge that trade for yourself before running it.

## Architecture

Pure core, I/O only at the edges.

| Module | Role |
|---|---|
| [site.py](src/slotwatch/site.py) | Runtime site profile. The only place target details exist |
| [fetch.py](src/slotwatch/fetch.py) | Only module touching the target site |
| [notify.py](src/slotwatch/notify.py) | Only module touching Discord; scrubs the webhook URL from every error |
| [parse.py](src/slotwatch/parse.py) | HTML → `ParseResult`. Pure |
| [diff.py](src/slotwatch/diff.py) | `(state, observation)` → events. Pure |
| [rules.py](src/slotwatch/rules.py) | Slot ↔ `WatchRule` matching. Pure |
| [format.py](src/slotwatch/format.py) | Events → one batched Discord embed. Pure |
| [config.py](src/slotwatch/config.py) | `rules.yaml` + DST-safe window gating. Pure |
| [state.py](src/slotwatch/state.py) | Durable memory across runs, the daily tally, and the concurrent-write merge |
| [main.py](src/slotwatch/main.py) | Wiring, CLI, self-pacing loop |

Rules are *data*, not code, which is what lets a later phase add slash commands (Discord
HTTP Interactions on a serverless worker, rewriting `rules.yaml` via the GitHub API)
without the poller changing.

## Why this is tested the way it is

The behaviour that matters is a **transition between two page states over time**. You
cannot summon that against the live site — you would wait days for a real cancellation.
Fixture pairs are not a convenience here, they are the only way to exercise the feature at
all.

Three fixtures are real captures with proper nouns and contact details anonymised; their
structure is byte-faithful to the live page. The transition variants are derived by
[make_variants.py](tests/fixtures/make_variants.py), and CI verifies they still regenerate
identically.

Behaviours worth knowing are locked in by tests:

- **Cold start seeds silently** — otherwise run #1 pings every currently-listed row.
- **Empty state is zero slots, not an error** — conflating them is the one bug that would
  make the bot fail silently forever.
- **Degraded markup never returns a silently-empty list** — it returns the good rows plus
  anomalies, and a malformed row is skipped rather than mis-assigned.
- **A failed Discord post does not advance state**, so the alert is retried instead of
  forgotten.
- **Batching** — six simultaneous openings are one message, not six buzzes.
- **Cooldown** — a slot flapping full ↔ open cannot ping every 3 minutes.
- **A merge never drops a notified-marker**, since a lost marker re-pings a slot you
  were already told about — the property that rules out "just take one side".
- **Expiry fails open** — no clock, no date or no readable time each still alert. The
  fail-open cases are the load-bearing half of that feature, not the happy path.
- **A finished session is still recorded in state.** Dropping it from the observation
  would look like ageing off the rolling window, and its return would then read as a
  brand-new slot.
- **An undelivered daily report stays pending**, and is never overwritten by the next
  day opening on top of it.

## Known limits

- Fixtures freeze the markup as captured; only the `health` trigger and `pytest -m live`
  cover future site changes.
- Messages cannot deep-link the tab (tab switching is JS-driven), so each alert names the
  tab to click.
- The rolling window means dates silently disappear off the back; expected, never alerted.
- Obscuring the target reduces casual discoverability — it is not anonymity. Anyone who
  runs the bot, reads the secret, or inspects outbound traffic learns the target.
- The daily report can only be sent by a job that runs. A day on which Actions dropped
  *every* scheduled start produces no report at all until the next run picks it up —
  the weekly [heartbeat](.github/workflows/heartbeat.yml) commit is the backstop for
  that, not the daily ping.
