# Schedule App

A web version of `Schedule_Maker_4000.py` + `Formatter_Pro.py`. Employees paint
their week in the browser — both the hours they *can* work and the hours they'd
*rather* work — and you generate the schedule from an admin dashboard running
the same OR-Tools CP-SAT solver as your original script.

## What changed in this version

**The `2` from your spreadsheet is back.** Availability is stored as `0`
(can't work), `1` (can work), `2` (would prefer). The old version silently
flattened everything to a yes/no, so preferences were being thrown away.

**A fairness section.** The solver no longer just maximizes total preferred
hours granted — that hands one person 96% of their picks and another 17%.
See *How fairness works* below.

**A week check.** The admin dashboard now tells you what's actually blocking a
week ("Tue 7AM–9AM: only 3 people are available, but 4 are needed") instead of
a generic "no valid schedule exists."

## The employee form

Days across the top, hours down the side, drag to paint — like WhenIsGood.
Three modes:

| Mode | Colour | Meaning |
|---|---|---|
| Can work | blue | available, no strong feeling |
| Would prefer | gold + diagonal hatch | available *and* wants this hour |
| Erase | white | not available |

Preferences are a subset of availability by construction: level 2 counts as
available everywhere downstream, so gold always sits inside blue and nobody can
mark a preference for an hour they can't work.

Blue and gold were picked over the usual green/red because they stay
distinguishable under every common form of colour blindness, and the hatch
pattern means the two tiers survive a black-and-white printout.

The grid also tells people things they used to find out the hard way: how many
hours they've marked against their weekly minimum, and which days have no
stretch long enough to hold a minimum-length shift.

## How fairness works

Each person gets one score, and the solver maximizes the **lowest** score on the
roster.

**Preference satisfaction, normalized.** Counting raw satisfied hours favours
whoever marked the most preferences. Instead each person is scored against their
own ask:

```
pref_score = 1000 × (preferred hours received) / (preferred hours they could realistically get)
```

The denominator is capped by their max weekly hours and the longest week they
could legally work, so someone who marks every hour as preferred isn't scored
against a target they were never going to reach. Someone who marks 5 preferred
hours and gets 4 scores higher than someone who marks 40 and gets 20.

**Burden, weighted by scarcity.** This is the part you asked for. For every
hour, compare how many people asked for it against how many are needed:

```
burden = 10 × max(0, staff_needed − people_who_prefer_it) / staff_needed
```

An hour four people are needed for that nobody asked for scores 10. An hour that
four or more people want scores 0. Thursday 7–9AM with no takers is expensive;
Wednesday at 2PM that everyone wants is free.

**The combined score.**

```
deal = pref_score − burdenWeight × (sum of burden for every hour you worked)
```

Because the objective lifts the lowest `deal` on the roster, covering the shift
nobody wants drops you to the bottom, and the only lever the solver has to raise
the floor again is giving you more of your preferred hours elsewhere in the
week. That is the compensation loop you described, expressed as a single number.

Measured on a 20-person test roster, against the naive "maximize total preferred
hours" objective:

| | Worst person's share | Best person's share | Spread | Total preferred hours granted |
|---|---|---|---|---|
| Fairness floor on | 63% | 88% | 25 pts | 214 |
| Fairness floor off | 17% | 96% | 79 pts | 218 |

Four preferred hours across the whole roster buys a 54-point reduction in
spread. That comparison describes the historical weighted solver. Weekday priorities are now fixed: fairness first, preferred hours second, fewer cap overruns third, and boundary distribution last. Only `burdenWeight` remains an editable part of the fairness metric.

The Generate tab shows the per-person breakdown, and it's written to a
**Fairness** sheet in the Excel export so you have something to point at when
someone asks why they got the Thursday open.

## The week check

The **Weekly hours remaining** panel appears in both Week check and Employees.
It subtracts every roster employee's saved minimum weekly hours from weekday
staff-hours required by the current scheduling settings. For example, 200 required
minus 215 allotted displays **−15**, rather than clamping to zero. It includes
employees without submissions and is independent of the viewed folder and the
Generate selection; the existing Week check diagnostics still use that selection.

Editing minimum hours shows an **Unsaved preview** in both panels. Save each row
to commit it; failed saves retain the draft. Invalid minimum inputs suppress the
preview and show the saved balance. A matching total is a planning aid, not proof
that availability, lead coverage, and shift rules permit a schedule. Day-hours
lead coverage (`requireLeadDuringOpen`, on by default) applies before
`lateHourStart`; late-hours coverage (`requireLeadDuringLate`, off by default)
is controlled separately. Either switch can be enabled on its own. Only staffed
hours require coverage, and Settings labels and help follow the configured hours.
The counter
does not change solver rules, saved schedules, or weekend generation.

The admin-only `GET /api/staffing-plan` endpoint returns `requiredHours`,
`allottedHours`, `remainingHours`, and the full `employees` roster. Demand uses
the solver's existing hourly staffing helpers, including closing times. No
database migration is required.

Three heatmaps and two lists, all computed by arithmetic before the solver runs:

- **Coverage** — people free vs. people needed, per hour. Red means provably
  impossible, amber means exactly enough with zero slack.
- **Demand** — how many people asked for each hour.
- **Burden** — what covering each hour costs in fairness terms.

The blocker list catches things the old pre-check missed:

- An hour where fewer people are available than the staffing requirement, merged
  into ranges so you get "Tue 7AM–9AM" rather than three separate lines.
- A staffed hour with no lead available, when its day-hours or late-hours lead switch requires one.
- **Headcount arithmetic from your shift caps.** With your defaults, the week
  has 20 opening slots and 8 closing slots, and each person is capped at 2
  opening-plus-closing shifts, so the roster needs at least 14 people. Below
  that the week is impossible no matter what anyone submits, and nothing in the
  old version told you so.
- Total staff-hours needed vs. what everyone's availability and hour caps can
  actually supply.
- A person whose weekly minimum can't be met — now accounting for contiguity and
  minimum shift length, so four scattered single hours no longer count as four
  workable hours.

And if the solver still fails after the pre-check passes, it re-solves with the
staffing requirements allowed to bend and reports the smallest set of rules that
would have to give: *"Tue 7AM–9AM: short 1 person."*

## Running it locally

```bash
cd flask-app
pip install -r requirements.txt
ADMIN_PASSWORD=pick-a-real-password python3 app.py
```

Employee form at `http://localhost:5000`, dashboard at
`http://localhost:5000/admin.html`.

`python3 seed.py` fills the database with 20 fake employees if you want
something to click through. `python3 verify.py` regenerates schedules on random
rosters and asserts every hard constraint holds — run it after changing rules in
`solver.py`.

## Hosting it for free

Everything below is $0 with no credit card. The Flask app serves the frontend
itself, so there's no separate frontend host to set up.

**Render (app) + Neon (database).**

1. Push this folder to a GitHub repo.
2. Create a free Neon project, copy the connection string.
3. On Render, create a **Web Service** from the repo:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --timeout 120`
   - Environment: `ADMIN_PASSWORD` = your password, `DATABASE_URL` = the Neon string.

`models.py` reads `DATABASE_URL` and switches from SQLite to Postgres with no
other changes; leave it unset and you get SQLite on local disk.

Two settings matter here. `--workers 1` is required because admin login tokens
live in process memory — with more workers you'd get randomly logged out.
`--timeout 120` must stay above your solver time limit, or gunicorn kills the
request mid-solve; the default of 30 seconds would cut off a 30-second solve.

The catches, both fine for something you use in bursts a few times a
semester: a free Render web service sleeps after 15 idle minutes and takes about a minute to wake, runs on 512 MB of RAM, and can't attach a persistent disk — which is exactly why the database lives on Neon rather than
on the app's filesystem. Render's own free Postgres expires 30 days after creation, so don't use that one. Neon suspends an idle database after 5 minutes and wakes it in milliseconds on the next query, with no
expiry date and none of the 7-day inactivity pause Supabase's free tier applies.

**PythonAnywhere**, if you'd rather upload files in a browser than use Git.
Free accounts get 512 MiB of disk which is persistent, so SQLite works with no external database, and the CPU-second quota doesn't apply to web app
requests. Watch two things: OR-Tools is a large install to fit in 512 MiB
(check with `du -sh ~/.local` after installing), and a free web app expires after a month, so log in and hit reload before a
scheduling session if it's been a while.

Since the app sleeps when idle, expect the first page load of a session to be
slow, and expect solves to take longer on a fractional CPU than on your laptop.
Raise the solver time limit in Settings if you see the "ran out of time before
balancing preferences" note.

## How the solve runs

Weekday generation uses four integer optimization stages under one total deadline.
A later objective cannot reduce an earlier score. If fairness proof uses the time
budget, the best feasible result is returned with an explicit unproved-optimality
note; no refinement can trade fairness away. Later timeouts retain the last valid
incumbent.

The optional preferred-boundary feature requires separate employee opening and
closing consent plus supervisor enablement. See the
[consent implementation, migration, tests, and review guide](PREFERRED_BOUNDARY_CONSENT.md).

## Other fixes in this version

- `init_db()` now runs at import. It used to sit under `if __name__ ==
  "__main__"`, so any real deployment (gunicorn, PythonAnywhere) never created
  the tables and died on the first request.
- Slot assignment can no longer silently drop people. Raising "staff needed per
  hour" above the number of slot names used to schedule someone the printable
  grid then discarded.
- The lead slot is configurable instead of hardcoded to `"DLA"`, which used to
  crash slot assignment if you renamed it in Settings.
- Settings are validated. `minShiftLength > maxShiftLength` and friends are
  rejected with a reason instead of producing a mysterious infeasible week.
- Names are matched case-insensitively, so "avery", "Avery" and "AVERY" are one
  person instead of three separate people all entering the solver.
- Availability payloads are validated against the configured days and hours.
- Login is rate-limited to 10 attempts per 10 minutes and uses a constant-time
  comparison.
- Database connections are opened and closed per request.

## Honest limitations

- **Single shared admin password**, not per-person accounts. Fine for one or two
  supervisors.
- **The employee form still trusts the name typed in.** Autocomplete against the
  roster reduces accidents, and you can turn off self-registration in Settings
  so only people you've added can submit, but there's no real identity check.
  Per-employee links would be the next step.
- **Not hardened for the public internet.** Reasonable for a small team; if it
  ever holds anything sensitive, it deserves a security review first.
- **Preferences are "want" only.** There's no "I'll do it if you need me but I'd
  rather not" tier. That would be a level between 0 and 1 and a negative term in
  the objective — straightforward to add if it turns out to matter.

## Files

```
flask-app/
  app.py              Flask routes / API
  models.py           Peewee models + config defaults and validation
  solver.py           CP-SAT model, fairness scoring, diagnostics
  verify.py           Constraint regression check
  seed.py             Demo data
  requirements.txt
  public/
    index.html        Employee availability form
    admin.html        Admin dashboard
    css/style.css
    js/employee.js    Painting grid
    js/admin.js       Dashboard, diagnostics, Excel export
```


## Semester folders and durable records

Availability, comments, folders, server sessions, and generated snapshots are
stored in the backend database. There is no availability expiration. A successful
submission follows a committed database transaction. Employee access remains
name-based; folders and stable IDs do not authenticate employees.

On first startup, an atomic, idempotent migration copies legacy availability into
`Imported availability`, retaining the old availability table, employees, and
settings. PostgreSQL uses a transaction advisory lock to serialize concurrent
migration and folder/submission writes. No database reset is required.

Keep the existing Render `DATABASE_URL` pointing to persistent PostgreSQL/Neon.
Do not substitute SQLite on Render's ephemeral filesystem. For local SQLite,
set `DATABASE_PATH` to a persistent disk path. `DB_PATH` is also supported.
A redeploy or process restart must reuse the same database.

Before deploying, verify the destination and backup readiness privately:

- PostgreSQL: take a consistent custom-format `pg_dump` using a suitably matched
  PostgreSQL client and secure libpq credentials (never put credentials in chat,
  source, or logs). Keep the dump outside the service filesystem. Inspect it with
  `pg_restore --list` and restore into a separate disposable database to validate
  it. Confirm Neon restore-history coverage in the actual project. Do not assume
  a backup exists because Neon is configured.
- SQLite: run `python backup_db.py /persistent/schedule.db /backups/schedule.db`.
  The destination must be new. The helper includes committed WAL data and checks
  integrity. Test restoration to a separate path; stop application writers before
  switching `DATABASE_PATH` to a restored copy.
- A production restore is a separate recovery decision. Never drop, reset, or
  overwrite the live database merely to make deployment checks pass.

Generated snapshots retain their original configuration, employees, availability,
assignments, and fairness results. Reopening and exporting a snapshot uses those
saved settings. Collection includes Saturday/Sunday; generation and exports use
Monday-Friday only.

### Regression checks

The supervisor availability editor and viewer use fixed collection windows:
Monday–Saturday 7 AM–10 PM and Sunday 7 AM–5 PM. These remain editable even
when weekday operating hours or day-specific closing times are narrower.
Saving retains valid collection entries, comments, and employee-recorded consent;
supervisors cannot grant or reconfirm that consent. Weekday staffing, weekend
shift definitions, employee-facing restrictions, snapshots, and exports keep
their existing behavior. Run `node test_supervisor_availability_browser.cjs`
for disposable desktop/375px editing, persistence, and keyboard checks.

```sh
python -m unittest -v test_app
npm install --no-save jsdom@30.1.0 exceljs@4.4.0
node test_frontend.cjs
```

Node dependencies are for development only. The Excel regression runs ExcelJS's
browser bundle in the page's JavaScript realm, serializes real XLSX bytes, and
reopens them with the Node reader. Injecting the Node workbook constructor into
JSDOM is invalid because ExcelJS checks arrays using `instanceof Array`.
The disposable PostgreSQL check and its isolation requirements are documented in
`test_postgres.py`; never point that test at a live database.



### Permanent folder deletion

`Delete folder permanently` is separate from Archive / restore. The supervisor
reviews the exact name and counts, types that name, then confirms deletion.
Availability (including comments/timestamps), weekday snapshots, weekend
snapshots, and the folder are removed by folder ID in one write transaction.
Employees, minimum/maximum hours, lead flags, global settings, legacy storage,
and every other folder are preserved. Deleting the active folder stops submissions
and increments the submission revision; it never activates another folder.

The preview version hashes folder identity/state and all dependent record
contents under the existing write lock. Changed submissions, snapshots, names,
or submission status require a new preview and freshly typed confirmation.
Weekday generation releases the lock during solver work and rechecks folder
identity inside the snapshot-write transaction. Weekend generation rechecks at
completion; weekend saves require the generated folder ID and creation version.
Legacy saved weekend snapshots remain viewable; generate a fresh preview to save
again. No schema migration is required.

The folder view revision invalidates pending requests after deletion, clearing
availability/comments, selection, diagnostics, both previews/lists, and export
state. Another folder may be selected for viewing, without activating it. The
last-folder state keeps folder creation and roster-wide planning available.

Additional disposable browser checks:

```sh
npm install --no-save jsdom@30.1.0 exceljs@4.4.0 playwright@1.58.2
npx playwright install --with-deps chromium
node test_deletion_browser.cjs
```

The browser test creates temporary SQLite fixtures, overwrites DATABASE_URL with
an empty value, starts only a local Flask server, and checks confirmation,
cancellation, repeated-submit protection, preservation, final-folder cleanup,
and desktop/375px layouts. The PR workflow also runs the shared deletion tests
against a newly created database on a dedicated local PostgreSQL service. It
uses no production database credentials.

For manual review, use a disposable local database (never production Neon):

1. Run the regression/browser checks above, then create a fresh local database
   with DATABASE_URL unset and DATABASE_PATH pointing to a new temporary path.
2. Add a folder, activate it, and submit two fictional employees with comments.
   Generate and save a weekday schedule and a Friday-evening–Sunday weekend
   schedule. Create a second folder containing its own submissions/snapshots.
3. Open both schedule types, pin a submission, and open permanent deletion.
   Check the exact name, all three counts, preservation statement, and active
   submission warning. Cancel and verify the data remains. Type an incorrect
   name, then the exact name; only the latter enables Delete permanently.
4. Change a submission or add a snapshot in a second tab after previewing. Confirm
   deletion and verify the conflict refreshes counts and requires typing again.
5. Delete the folder. Confirm previews, comments, selection, diagnostics, export
   controls, and both saved lists no longer expose its data. The other folder and
   employee targets/settings remain; no new submission folder becomes active.
6. Submit a previously opened employee form and confirm rejection. Delete the
   final folder and check the empty state, creation controls, Employees, and
   Weekly hours remaining. Repeat at a 375px viewport and inspect the dialog.
## Automatic 18-month retention

`cleanup_retention.py` performs one bounded cleanup pass independently of Flask.
It does **not** run during app startup, import solver modules, initialize storage,
create tables, or migrate the database. Separately, the app runs the same bounded
cleanup on normal API requests by default after merge/deploy, as described below.

The retention unit is a database record, not an uploaded file. Employee and
supervisor availability saves reset `submitted_at`; retention uses that **latest
submission**, not first insertion. Consent-only edits do not extend retention.
Legacy `Availability` records use their latest submission too. Weekday and weekend
snapshots use their own `created_at`. Archived and active content use the same
policy. Folders use their own creation date and are removed only after **all**
their availability, weekday snapshots and weekend snapshots have been removed.
An old folder with any newer child retains its identity, name and active status.
Empty newer folders stay. Employees (including lead flags and hour targets),
global settings, authentication sessions and the submission-state singleton stay.

One UTC time is captured per pass. Subtract 18 **calendar months**, retaining the
time of day and microseconds, clamping to the last valid day of the destination
month. For example, August 31, 2026 becomes February 28, 2025; August 31, 2025
becomes February 29, 2024. Only timestamps **strictly before** the cutoff expire;
equality stays. A run at October 3, 2026 12:00 UTC uses April 3, 2025 12:00 UTC.
Existing naive timestamps mean UTC, regardless of the host's timezone. SQLite
normalizes offset-bearing legacy timestamps explicitly, without rounding away
microseconds. PostgreSQL must retain the existing `timestamp without time zone`
schema; the command rejects changed age-column types. Invalid/nonfinite timestamps
are excluded, reported and protect their folders pending investigation.

Deleting the active folder clears its active reference and advances the form
revision in the same transaction, without activating a replacement. Fresh
submissions remain allowed to surviving active folders. The singleton stays so a
restart cannot reimport removed legacy records. Generation rechecks input row
identities, submission dates and availability/comments after solving. Removed or
resubmitted inputs reject the result with 409. Weekend previews carry an opaque
input fingerprint and must still match on save. Consent-only changes preserve the
existing pre-solve frozen agreement/settings behavior; solver rules are unchanged.

Younger snapshots are independent records and may contain copies of older source
information. This policy does not promise redaction of every historical copy.
Deletion does not erase exports on users' devices, information already in browser
memory, backups or Neon restore history. Open admin views refresh their lists and
clear cached previews/exports when a read finds removed content, or generation
rejects changed inputs. An overview refresh discards missing selections/pins.
There is no push notification: content already displayed remains until a refresh
or subsequent request detects the change. A stale browser may resubmit availability
to a surviving active folder, resetting its latest-submission age.

## Command and deployment controls

Production requires a nonempty PostgreSQL `DATABASE_URL`, plus
`RETENTION_DATABASE_HOST` (the exact hostname from that URL) and
`RETENTION_DATABASE_NAME` (its database name, normally `neondb`). Copy these from
the verified web-service connection privately; do not print the secret URL. The
job fails if the identity differs, storage/schema is missing, a lock times out, or
the transaction fails. It never falls back to SQLite in production mode.

```sh
# Read-only preview; RETENTION_ENABLED is not needed.
python cleanup_retention.py
# Explicit destructive pass, only after rollout review and enabling the setting.
RETENTION_ENABLED=true python cleanup_retention.py --apply --max-rows 1000
# Disposable SQLite only; never use the web service's local database.
DATABASE_PATH=/tmp/disposable-retention.db python cleanup_retention.py --local-test
```

SQLite test storage must already have been initialized by the app/test harness.
`--apply` also requires `RETENTION_ENABLED=true` in local-test mode. Do not pass
connection strings on the command line. The connection closes on success/failure;
SIGTERM or interruption rolls back an open transaction.

Each apply pass uses the existing SQLite `BEGIN IMMEDIATE` / PostgreSQL advisory
transaction lock `9032401`, coordinating with app writes and manual deletion.
Eligibility is selected under that lock; a preview is never reused as a deletion
list. A pass is one atomic transaction, capped at 1,000 total record deletions by
default (`--max-rows` accepts 1–10,000). Availability and snapshots/legacy rows
are removed before eligible empty folders. The lock wait defaults to 5 seconds
(`--lock-timeout-ms` accepts 1–60,000); PostgreSQL statements also have a 60-second
timeout. No automatic retry occurs: a failed transaction rolls back completely,
exits nonzero, and can be safely rerun. Successful bounded passes resume on later
runs. Logs report UTC run time/cutoff, candidates, committed counts, invalid dates
and remaining backlog, without comments, employee names, snapshots or tokens.
Do not treat `backlogRemaining=true` as a fully cleared backlog. Review invalid
timestamp counts even when there is no eligible backlog. Manual folder deletion's
exact-name and preview-version checks remain required; cleanup invalidates a
preview when it changes its contents.

No timestamp migration/index is added: the inspected database has only one folder
and four submissions. Existing folder foreign-key indexes support child existence
checks. Evaluate `(submitted_at, id)` / `(created_at, id)` indexes using query plans
before growing the workload or increasing the batch limit; apply any index change
through the app's serialized migration process, never through this job.

## Free automatic cleanup when the app is used

Normal `/api/` requests run a bounded retention pass before reading or changing
app data. Opening the employee or admin page makes these API requests, so using
the website triggers cleanup. Static files and `/healthz` do not trigger it.
No Render cron, GitHub schedule, extra service, new secret or repository activity
is required. The existing app database connection is used, including Neon's
pooled connection; cleanup settings are transaction-local and restored afterward.

Cleanup is enabled by default after this change is manually merged and deployed.
Set `REQUEST_RETENTION_ENABLED=false` on the web service to disable it temporarily.
There is no automatic cleanup while the website is unused. After four months of
inactivity, the next API request checks ages against that request's current UTC
cutoff. Only records **strictly older than 18 calendar months** are eligible:
exactly 18 months old and all newer records remain. Availability age still starts
at the latest submission; consent-only edits do not extend it. Old folders remain
while they contain newer or invalid-dated children. Shared infrastructure remains.

One server process attempts cleanup at most once per 24 hours after a successful
pass with no backlog. Each pass commits at most 1,000 deletions. If a backlog
remains, later API requests can trigger another pass after 60 seconds. A server
restart resets this in-memory interval; multiple server processes may each run a
pass. All passes share the app's database write lock, recheck eligibility under
that lock and safely resume the backlog, so extra attempts cannot delete younger
records. The deployed app currently has one worker.

The triggering request waits for the bounded pass; PostgreSQL lock waits are
limited to 200 milliseconds and individual statements to five seconds. If cleanup
fails or the database is busy, it rolls back, logs only the exception class,
and allows the normal request to continue. Another visit can retry after five
minutes. Normal request database failures still use the app's existing error
handling. Review Render application logs for `Request retention` results or
warnings, including deleted counts, cutoff, invalid timestamps and backlog.

Verify recovery coverage before rollout and check the merged web deploy. The
standalone cleanup command remains available for a private read-only preview or
an explicit bounded maintenance pass. Do not also enable a separate scheduled
apply workflow. Code rollback or disabling cleanup stops future deletion; it
does not restore already deleted records. Records can remain older than the
cutoff during inactivity, failures or while a large backlog drains.

Tests: `python -m unittest -v test_app test_boundary` covers retention on disposable
SQLite along with existing app/deletion/solver-boundary regressions.
`TEST_POSTGRES_PORT=... python test_postgres.py` runs the same retention/deletion
regressions inside a uniquely created local PostgreSQL database. Never point the
test harness at Neon. `node test_frontend.cjs`, `node test_deletion_browser.cjs`
and `node test_retention_browser.cjs` cover frontend state, existing manual
deletion and background removal recovery (including 375px). The PR workflow also
runs `node test_request_retention_browser.cjs`, which verifies cleanup on the first
real website visit at desktop and 375px sizes, plus existing
boundary/supervisor/permission browser and solver suites.
