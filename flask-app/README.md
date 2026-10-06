# AV Delivery Schedule App — application guide

This directory contains the Flask backend, browser frontend, database models,
weekday CP-SAT solver, weekend rotation generator, and regression checks.
The [repository README](../README.md) provides the project overview.

## Contents

- [Local setup](#local-setup)
- [Render and Neon deployment](#render-and-neon-deployment)
- [Collecting availability](#collecting-availability)
- [Folders, rosters, and settings](#folders-rosters-and-settings)
- [Current availability and response history](#current-availability-and-response-history)
- [Additional-shift permissions](#additional-shift-permissions)
- [Weekday diagnostics and scheduling](#weekday-diagnostics-and-scheduling)
- [Weekend scheduling](#weekend-scheduling)
- [Persistence and migrations](#persistence-and-migrations)
- [Retention and deletion](#retention-and-deletion)
- [Backups and recovery](#backups-and-recovery)
- [Tests](#tests)
- [Access and security behavior](#access-and-security-behavior)
- [Files](#files)

## Local setup

Python 3.12 and Node 22 match the repository's CI environment. Node is needed
only for frontend tests, not to run the Flask application.

From the repository root:

```sh
cd flask-app
python -m venv .venv
```

Activate the environment:

```sh
# macOS / Linux
. .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install the backend dependencies:

```sh
python -m pip install -r requirements.txt
```

Configure the environment in the same terminal before starting the app:

| Variable | Purpose |
| --- | --- |
| `ADMIN_PASSWORD` | Required unique supervisor password. Empty values and `admin123` prevent startup. |
| `COLLECTION_ENCRYPTION_KEY` | Fernet key for encrypting stored collection codes. Required for collection endpoints. |
| `COLLECTION_VERIFIER_KEY` | Separate 32-byte key encoded as 64 hexadecimal characters, for code/CSRF verification and rate identities. |
| `DATABASE_URL` | PostgreSQL connection URL. If unset or empty, the app uses SQLite. Keep credentials server-side. |
| `DATABASE_PATH` | SQLite path; defaults to `schedule.db` in the working directory. `DB_PATH` is a fallback alias. Ignored when `DATABASE_URL` is set. |
| `PORT` | Local Flask port; defaults to 5000. |
| `REQUEST_RETENTION_ENABLED` | Defaults to the exact string `true`. Set to `false` to stop cleanup on normal API requests. |

Generate two independent collection keys after installing the requirements:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the first output as `COLLECTION_ENCRYPTION_KEY` and the second as
`COLLECTION_VERIFIER_KEY`. Keep them private and stable across restarts.
Set a unique `ADMIN_PASSWORD` as well. For example, PowerShell uses
`$env:ADMIN_PASSWORD = 'your-unique-local-password'`; POSIX shells use
`export ADMIN_PASSWORD='your-unique-local-password'`. Set the other variables
in the same way. Use a disposable local database for experimentation; clear any
inherited production `DATABASE_URL` first. The app reads process environment
variables directly and does not automatically load a `.env` file.

```sh
python app.py
```

Open the employee form at [localhost:5000](http://localhost:5000/) and the
dashboard at [localhost:5000/admin.html](http://localhost:5000/admin.html).
Sign in with `ADMIN_PASSWORD`, choose a folder, and create a collection code to
exercise employee submissions. Initial storage includes an
`Imported availability` folder, even when there are no legacy responses.

Without valid collection keys, collection endpoints return 503 rather than
accepting unprotected submissions. Other supervisor features can still load.
Changing the encryption key without migrating stored codes prevents their
display; changing the verifier key invalidates existing code verification and
CSRF tokens. Back up the keys securely and plan key rotation as a migration.

### Demo data

`seed.py` creates 20 synthetic weekday employees only when the employee table is
empty. It uses the legacy active-folder reference, normally the initial
`Imported availability` folder; the current Codes workflow does not offer an
activation button. Run it only in a fresh disposable local database with
`DATABASE_URL` unset and `DATABASE_PATH` pointing to that database:

```sh
python seed.py
```

If no legacy active folder exists, seeding refuses to run. Ordinary collection
and scheduling do not need this script or the legacy active-folder reference.

## Render and Neon deployment

The current hosted service was checked on **October 6, 2026**:

| Setting | Current value |
| --- | --- |
| Repository / deployed branch | `Jammezy/AV-Delivery-Schedule-App` / `master` |
| Render service | `AV-Delivery-Schedule-App`, Free web service, Virginia |
| Root directory | `flask-app` |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --workers 1 --timeout 120` |
| Health-check path | `/healthz` |
| Deployment trigger | Commits to `master` |
| Database project | Neon's `Schedule Generator`, PostgreSQL 18, AWS US East 2 |
| Database default branch | `production` |

The Render environment has `ADMIN_PASSWORD`, both collection-key variables,
and `DATABASE_URL` configured. Secret values are intentionally omitted.
The deployed commit matched the source reviewed for this guide.

To configure another deployment:

1. Create a Neon database and select the intended persistent production branch.
2. Connect a Render Web Service to this repository and use the root directory,
   build/start commands, and health-check path above.
3. Set a unique `ADMIN_PASSWORD`, stable `COLLECTION_ENCRYPTION_KEY`,
   stable `COLLECTION_VERIFIER_KEY`, and the server-only Neon `DATABASE_URL`
   with TLS enabled.
4. Back up existing data before upgrading. App import runs serialized additive
   initialization/migrations; keep the same database across redeploys.
5. Verify code creation, a synthetic submission in a test folder, and both
   scheduling workflows after deployment.

The one-worker start command matches the current small-service deployment.
Admin session hashes now live in the database and survive process restarts;
the old requirement for one worker solely to keep login tokens in memory no
longer applies. Login attempt tracking and retention timing still live per
process. Keep the Gunicorn timeout comfortably above `solverTimeLimit`, allowing
for database and response work; changing the solver limit may also require
changing the server timeout. `solverWorkers` controls OR-Tools search threads,
not Gunicorn web workers.

`/healthz` returns a simple `{"ok": true}` response. It opens the request database
connection, but it does not query every table or validate collection keys and
does not trigger retention cleanup. It is not a complete end-to-end collection
check.

### Returning after inactivity

Render's Free web service sleeps after 15 minutes without inbound traffic.
An ordinary visit wakes it, normally taking about one minute. Its local
filesystem is ephemeral, so keep application data in Neon rather than SQLite.
See [Render's Free service behavior](https://render.com/docs/free).

Neon Free compute scales to zero after five idle minutes and reactivates
automatically on a query. Stored data remains while compute sleeps; a long-idle
first connection can add latency. See
[Neon's scale-to-zero documentation](https://neon.com/docs/introduction/scale-to-zero).

A gap of two to four months does not itself require manually restarting either
service, provided the accounts, production branch, credentials, and usage limits
remain valid. A keep-alive service is not required. Before a collection period,
check the site and existing data, review provider notices, and keep backups and
dependencies current. Free hosting does not promise indefinite maintenance-free
operation. Application records remain subject to the 18-month policy below.

## Collecting availability

In **Codes**, choose the scheduling folder, optionally add a label and expiry,
then select **Create code** and **Copy invitation**. Share the invitation through
your usual communication channel; the app does not send it automatically.

A valid shared code unlocks the employee form without creating an account.
Employees enter a name, paint weekday/weekend availability, optionally record
additional-shift choices, and add a comment of up to 99 characters. The form asks
them not to include sensitive personal information.

| Paint mode | Stored value | Display |
| --- | --- | --- |
| Unavailable / erase | `0` or omitted | White |
| Can work | `1` | Blue |
| Would prefer | `2` | Gold with diagonal hatch |

Level 2 is also available. Ordinary forms use the code folder's collection
configuration, with Saturday and Sunday included and Sunday closing at 5 PM.
Supervisor and employee edit-link forms use the wider fixed correction windows
described below.

Submitting successfully saves the response, employee entry, current availability,
and counters in one transaction. Ordinary new sheets create separate entries;
case-insensitive repeated names receive suffixes such as `Alex (2)`. A typed
name or a shared code does not prove identity or merge submissions.

An unlocked form allows one successful submission. The page then disables
submission and offers **Enter a code for another submission**. Identical retries
reuse the saved receipt while the receipt/session remains valid.

### Response allowances and stopping collection

- Each new code starts with **30 successful responses total**, shared across its
  users. **Add responses** increases that code's total limit without resetting
  usage. Limits can increase to 10,000; revoked/deleted codes cannot be increased.
- The **Folder submission limit** is a separate cumulative allowance across all
  codes in that folder. It defaults to **100** when no limit has been saved.
  Existing saved limits, including older values of 300, are preserved.
  Supervisors can save a folder cap of 30–10,000.
- New sheets and edit-link corrections consume one response from both allowances.
  Invalid forms and identical retries do not consume another response.
  Creating another code does not raise the folder cap; allowances do not reset
  weekly, monthly, after restart, or when records are deleted.
- Several folders can collect at the same time. A code always targets its assigned
  folder, regardless of the supervisor's currently viewed folder.
- **Revoke**, **Delete code**, expiry, or archiving the folder stops new
  submissions, including from previously unlocked forms. Deleting a code hides
  it by default but preserves history/usage; **Show deleted codes** reveals it.
- At most 100 code records may exist per folder, including soft-deleted codes.

The form refreshes the server's code destination and allowance context without
discarding draft answers. It does not display the old explanatory
response-limit notice. Server enforcement remains in place.

See [COLLECTION_CODES.md](../docs/design/COLLECTION_CODES.md) for the feature's API, storage,
security, and regression details; its pre-merge notes describe the historical
rollout rather than a still-pending feature.

## Folders, rosters, and settings

The selected folder scopes **Employees**, **Settings**, **Week check**,
**Submissions**, **Codes**, weekday/weekend generation, and saved schedules.
A new folder starts with default scheduling settings and an empty roster.
Changing its settings, lead status, or employee hour limits does not edit another
folder. Matching names do not automatically link people across folders.

Supervisors can add employees before they submit, or explicitly import unassigned
legacy identities from Employees. Removing a membership excludes that employee
from current scheduling and revokes their edit grants in that folder while
preserving response history, saved schedules, the employee identity, and other
memberships.

### Weekday settings

These keys come from `models.py`; saved folder values may differ from defaults.

| Settings | Default / behavior |
| --- | --- |
| `days` | Monday–Friday for weekday generation; `availabilityDays` additionally includes Saturday/Sunday. |
| `hourStart`, `hourEnd` | 7 and 21. `hourEnd` starts the final one-hour block, so 21 means 9–10 PM. |
| `reqStaffOpen`, `reqStaffLate`, `lateHourStart` | 4 before 7 PM, 2 from 7 PM onward. |
| `fridayCloseHour`, `dayCloseHours` | Friday closes at 19 (7 PM) by default; per-day closing overrides take precedence. Closing hours are exclusive boundaries. |
| `minShiftLength`, `maxShiftLength` | 3 and 6 hours. |
| Employee `minHours`, `maxHours`, `isLead` | Folder-specific roster attributes; new ordinary entries default to 0–40 hours and non-lead. |
| `maxMorningShifts`, `maxEveningShifts`, `maxMorningPlusEvening` | 2 openings, 1 closing, 2 combined per employee per week. |
| `allowPreferredBoundaryExtras` | Off; optional qualifying preferred-boundary exceptions. |
| `blockClopening` | On; blocks working a day's last staffed hour followed by an opening-window hour the next weekday. |
| `requireLeadDuringOpen` | On; requires a lead in staffed hours before `lateHourStart`. |
| `requireLeadDuringLate` | Off; independently requires a lead in staffed hours from `lateHourStart`. |
| `slotNames`, `leadSlotName` | `DLA, A4, A1, A2`, with `DLA` as the lead slot. |
| `burdenWeight` | 3; penalty multiplier within the fairness score. |
| `solverTimeLimit`, `solverWorkers` | 30 seconds total solver budget and 8 search threads. |
| `wFairness`, `wPreference`, `wSpread` | Legacy readable keys; do not change weekday objective priority. |

Day and late lead switches can be enabled independently. Closed or unstaffed
hours are exempt. Settings labels/help follow the configured operating windows.
Invalid combinations such as a minimum shift longer than the maximum are rejected.

`allowSelfRegister` remains a legacy configuration key; it is not an identity
check or a roster-only gate for the current shared-code submission flow.
Possession of a valid code authorizes a new, separate entry.

See [FOLDER_ISOLATION.md](../docs/design/FOLDER_ISOLATION.md) for membership storage, scoped API
routes, and the one-time migration.

## Current availability and response history

**Submissions** opens with **Current availability**. Hover/focus to preview a
person, or click to pin them. The weekly availability grid appears before the
additional-shift permission controls. Excluding an employee from weekday
generation does not delete their saved submission.

The supervisor **Edit** dialog can save current availability and comments across
**Monday–Saturday 7 AM–10 PM** and **Sunday 7 AM–5 PM**, including Friday evening.
These correction windows remain editable when weekday scheduling hours are
narrower. Saving availability preserves recorded permission choices and does not
grant/reconfirm them; use the separate permission controls for that action.

**View response history (N)** opens a dialog with all saved responses in the
selected folder, newest first in pages of 20. Search by submitted name or code
label, then select a response to inspect its original grid, comment, choices,
and correction relationship. The multiple-response notice appears only for
affected names. Switching folders clears the old selection and history details.

### Employee edit links

Pin current availability and select **Create edit link**, or use a supported
response-history entry. Links expire after 24 hours and are redeemed once into a
temporary employee session. They use stable employee/folder IDs and load the
employee's **current saved availability when opened**, including successful
supervisor changes; they do not reload an old response just because names match.

An edit fixes the submitted name, updates the same current employee record, and
adds a correction response linked to the previous response. The original response
remains in history. Corrections use the original code/folder allowances and still
require an accepting code and unarchived folder. Missing current records produce
an error instead of recreating them.

Creating a replacement link or selecting **Revoke edit links** revokes that
employee's existing links and redeemed sessions in the folder. Concurrent saves
use a content/version check: a stale supervisor or employee edit receives a
conflict and must reload before saving. Context refresh alone does not upgrade
a stale draft's version.

## Additional-shift permissions

When `allowPreferredBoundaryExtras` is enabled, opening and closing permissions
can independently allow assignments above normal opening/closing and combined
caps. Only qualifying preferred boundary blocks earn this exception. Availability,
weekly hour bounds, shift lengths, lead coverage, and clopening rules remain hard.

Employees see the additional-shift choice section when their qualifying preferred
openings or closings exceed that type's individual cap. Existing recorded choices
can still be cleared or reconfirmed even when this section is hidden. Exceeding
only the combined cap does not by itself show the choice section.

Supervisors can explicitly set/reconfirm either choice using the pinned person's
**Save additional-shift permissions** controls. Employees can subsequently change
their own choices. Saving a permission alone does not alter availability/comments
or extend the availability retention timestamp.

Permission stays recorded when shift lengths change, while qualifying days are
recalculated against the current minimum shift length. Changes to caps or actual
staffed boundaries make it stale and require explicit reconfirmation. Disabling
the feature prevents exceptions without erasing recorded choices. Additional
shifts remain optional, and higher-priority fairness cannot be traded away to
assign them.

[PREFERRED_BOUNDARY_CONSENT.md](../docs/design/PREFERRED_BOUNDARY_CONSENT.md) explains the original
solver design and review. This section and the current `boundary.py` /
`test_boundary.py` describe the later persistent-permission behavior.

## Weekday diagnostics and scheduling

### Weekly hours remaining

Week check and Employees show:

```text
remaining hours = weekday staff-hours required − sum of folder roster minimum hours
```

This uses **every active roster member in the selected folder**, including people
without submissions, independently of the Generate selection. Negative values
remain negative. Editing minimum hours shows an **Unsaved preview**; save each row
to commit it. Invalid drafts suppress the preview, and a failed save retains the
draft. A zero balance is a planning aid, not proof that a schedule is feasible.

The folder-scoped endpoint is
`GET /api/folders/<folder_id>/staffing-plan`; it returns required, allotted,
and remaining hours plus the folder roster. Demand includes configured closing
times. This panel does not change solver rules or weekend generation.

### Week check and solver behavior

Diagnostics use the selected employees with saved availability in the current
folder. Coverage, demand, and burden heatmaps show shortages, zero-slack hours,
and scarce preferred coverage. Blockers include insufficient available staff,
missing required leads, opening/closing cap capacity, aggregate staff-hours,
and individual weekly minima that cannot fit legal contiguous shifts.

The weekday solver schedules Monday–Friday with exact hourly staffing, at most
one contiguous shift per employee per day, shift-length bounds, weekly hour
bounds, an all-or-nothing staffed late block, and the configured boundary,
lead, and clopening rules.

Four optimization stages share one `solverTimeLimit` deadline:

1. Maximize the worst employee deal score.
2. Maximize preferred hours assigned.
3. Minimize qualifying normal-cap overruns.
4. Maximize distribution of opening/closing duties.

A later stage starts only after the preceding optimum is proven and cannot
worsen that score. A timeout returns the last valid incumbent as `FEASIBLE`
with an explicit optimality note. `OPTIMAL` means all four stages completed with
proof. No incumbent produces `UNKNOWN`; proven infeasibility can produce a
relaxed diagnostic explanation only if time remains. Passing arithmetic
pre-checks does not guarantee feasibility.

### Fairness metric and export

The preference ceiling is
`min(preferred staffed hours marked, employee maxHours, maxShiftLength × weekdays)`.
It is an upper bound used for normalization, not the exact longest feasible
week computed from that employee's availability and all constraints.

```text
preferenceScore = floor(1000 × preferred hours assigned / preference ceiling)
hourBurden = round(10 × max(0, required staff − employees preferring the hour) / required staff)
dealScore = preferenceScore − burdenWeight × sum of assigned hourBurden
```

Unstaffed hours have zero burden. A zero preference ceiling uses a score of 1000,
then subtracts burden. These integer scores encourage compensation for less
popular assignments while raising the floor; they do not promise identical hours
or preference satisfaction.

Successful weekday generation **automatically saves** a database snapshot of
inputs, settings, selected employees, permissions, assignments, and results.
Reopening/exporting it uses those saved values. Excel export is generated in
the browser with ExcelJS and includes a Fairness sheet. There is no separate
publishing or employee schedule-notification workflow.

## Weekend scheduling

Weekend uses `weekend_generator.py`, separate from the weekday CP-SAT solver.
Choose a date range, excluded dates, recurring fixed assignments, and an ordered
rotating pool. The recurring shifts are:

| Day | Shift |
| --- | --- |
| Friday | 7–10 PM |
| Saturday | 7 AM–noon; noon–5 PM; 5–10 PM |
| Sunday | 7 AM–noon; noon–5 PM |

The generator checks full-shift availability and allows at most one assignment
per employee per weekend. Fixed employees are excluded from the rotating pool.
For open shifts it searches assignments per weekend to maximize filled shifts,
using accumulated rotation counts and pool order to order candidates. It reports
unfilled shifts and rotating counts; it does not apply the weekday deal-score
objectives, lead-coverage switches, or weekly hour limits, and does not jointly
optimize weekday plus weekend workloads.

Weekend generation creates a preview. Its **Save schedule** button explicitly stores
it; saved weekends can be reopened and deleted. Excel export currently covers
weekday schedules. The preview
must still match its folder and source availability when saved, otherwise
generate a fresh one. The folder's assignment controls reload when switching
folders.

## Persistence and migrations

Database storage includes current availability, response history, collection
codes/allowances, folders, memberships/settings, server sessions, and schedule
snapshots. A successful save follows a committed transaction. Database failures
are reported as errors rather than successful saves.

`init_db()` runs when the app is imported, including by Gunicorn. Initialization
and additive migrations are serialized with SQLite `BEGIN IMMEDIATE` or a
PostgreSQL transaction advisory lock. They preserve legacy tables and existing
snapshots rather than resetting the database.

The initial migration imports legacy availability into `Imported availability`.
The one-time `folder-isolation-v1` migration copies legacy settings into existing
folders and establishes memberships from availability/accepted responses.
Employees without a known association remain unassigned. Later restarts preserve
folder-specific edits and increased collection allowances. Collection startup
also imports old pending intake records into separate saved entries; rejected
records remain excluded.

Global `Employee` IDs remain stable historical references. Live scheduling
attributes are stored in `FolderEmployee`, and settings in `FolderConfig`.
Legacy `Config` and the active-folder singleton remain for compatibility, but
the current UI's collection destination comes from the code or edit grant.

Keep the same persistent database on every redeploy. Back up before schema/code
upgrades and validate recovery separately; do not drop tables or switch to a new
empty database to work around a deployment error.

## Retention and deletion

### Automatic 18-calendar-month retention

Normal `/api/` requests run bounded cleanup by default, before reading/changing
app data. Opening the employee/dashboard page makes those requests.
Static files and `/healthz` do not trigger cleanup. No cron service, GitHub
schedule, or keep-alive is required; the committed workflow is for tests.

Only records **strictly before** the UTC cutoff of 18 calendar months expire.
The cutoff preserves time of day/microseconds and clamps invalid destination
days to the end of that month. Equality stays. Active and archived folders
follow the same policy.

| Record | Timestamp used / eligibility |
| --- | --- |
| Current and legacy availability | Latest `submitted_at`; employee corrections and supervisor availability saves reset it. Permission-only saves do not. |
| Intake response history | Each response's own `submitted_at`; corrections add a new record and preserve the original's timestamp. |
| Weekday/weekend snapshots | Each snapshot's `created_at`. |
| Collection codes | `created_at`, and only after no retained intake response references the code. |
| Folders | `created_at`, and only after all retained availability, snapshots, intake responses, and codes are gone. |

An old folder with younger or invalid-dated children remains. Invalid/nonfinite
timestamps are retained and reported. Employee identities, legacy settings,
admin sessions, and the submission-state singleton are not aged out by this
policy; deleting a folder removes its dependent memberships, configuration,
collection settings, and grants/sessions.

One pass commits at most 1,000 record deletions in a single transaction.
Each server process waits 24 hours after a successful pass with no backlog,
60 seconds if backlog remains, or five minutes after a failed pass.
Restarting resets this in-memory timing; the database lock and eligibility
recheck keep concurrent passes safe. Request cleanup uses a 200 ms PostgreSQL
lock timeout and five-second statement timeout. Failures roll back, log a
sanitized warning, and allow the ordinary request to continue.

No cleanup runs while the site is unused. Old records can remain during
inactivity, failures, or backlog draining. On the next eligible API request,
cleanup uses that request's current cutoff. Set
`REQUEST_RETENTION_ENABLED=false` to stop future request cleanup; this does not
restore deleted records. `RETENTION_ENABLED` controls only standalone
`--apply`, not request cleanup.

Snapshots can contain historical copies of older source data. This policy does
not redact every copy, exports on user devices, backups, or Neon restore
history. Open pages detect removals on refresh/subsequent requests and clear
invalid selections/previews; there is no push notification.

### Standalone maintenance command

`cleanup_retention.py` defaults to a read-only preview. It does not initialize
storage or run migrations. Production use requires an existing PostgreSQL
`DATABASE_URL` and exact target guards:

- `RETENTION_DATABASE_HOST`: the URL's hostname.
- `RETENTION_DATABASE_NAME`: the URL's database name.
- `RETENTION_ENABLED=true`: required only when explicitly applying deletions.

With the URL and guards already set privately:

```sh
python cleanup_retention.py
```

After reviewing the preview and recovery readiness, an explicit pass is:

```sh
# Requires RETENTION_ENABLED=true in this terminal.
python cleanup_retention.py --apply --max-rows 1000
```

For disposable, already initialized SQLite storage, unset `DATABASE_URL`, set
`DATABASE_PATH`, and use `--local-test`. Applying still requires
`RETENTION_ENABLED=true`. The command checks schema, never falls back to local
SQLite in production mode, and uses the same database write lock as app writes.
`--max-rows` accepts 1–10,000; `--lock-timeout-ms` accepts 1–60,000 and defaults
to 5,000. PostgreSQL statements default to a 60-second timeout.

A failure rolls back the whole pass and exits nonzero. Successful logs report
counts, cutoff, invalid dates, and remaining backlog without response contents
or tokens. `backlogRemaining=true` means another pass is needed. Do not enable
a separate scheduled apply job alongside request cleanup.

### Archive and permanent deletion

Archiving closes collection but retains records until retention applies.
**Delete folder permanently** is separate: review the exact name/counts, type
that name, then confirm. Deletion removes the folder's availability/comments,
weekday/weekend schedules, intake responses, codes, collection settings,
memberships, scheduling settings, and dependent grants/sessions transactionally.
Employee identities, legacy storage, and other folders remain.

If dependent records or folder state changed after preview, confirmation is
rejected and must be refreshed. Generation rechecks folder/input state after
solving; stale weekend previews also fail when saved. Deleted-folder responses
cannot redirect into another folder. Creation remains available when the last
folder is removed.

## Backups and recovery

Persistent storage is not proof that a usable backup exists. Keep verified
backups outside Render's ephemeral filesystem, and keep collection keys securely
with the recovery materials.

- **PostgreSQL/Neon:** use a suitably matched PostgreSQL client to take a
  consistent custom-format `pg_dump` through a direct connection. Keep credentials
  out of source, chat, command-line URLs, and logs. Inspect with
  `pg_restore --list` and validate restoration into a separate disposable database.
  Check the actual project's restore-history window; it is distinct from the
  application's 18-month retention.
- **SQLite:** `python backup_db.py SOURCE_PATH NEW_BACKUP_PATH` uses SQLite's
  backup API, includes committed WAL data, and checks integrity. The destination
  must be new. Restore/test a separate copy; stop app writers before switching the
  configured database path.

A production restore is a separate recovery action. Code rollback or disabling
retention does not undelete records. Old releases may not understand folder
isolation or current collection protections; preserve added tables and review
compatibility before rolling back.

## Tests

Run checks from `flask-app`. They use synthetic data and disposable databases.
The PostgreSQL harness creates/drops uniquely named databases on a dedicated
**local** PostgreSQL server; never point it at production Neon.

Backend and solver checks:

```sh
python -m unittest -v test_app test_boundary test_collection_codes test_folder_isolation
python verify.py
```

`test_app` includes retention and permanent-deletion regression classes.
`verify.py` exercises random weekday rosters and verifies hard constraints.

Frontend dependencies match the CI workflow:

```sh
npm install --no-save jsdom@30.1.0 exceljs@4.4.0 playwright@1.58.2
npx playwright install chromium
node test_frontend.cjs
```

On Linux CI, the workflow uses
`npx playwright install --with-deps chromium` to install system dependencies too.
Excel tests run ExcelJS's browser bundle in the page realm, serialize XLSX bytes,
and reopen them with the Node reader.

Browser checks:

```sh
node test_collection_browser.cjs
node test_response_history_browser.cjs
node test_folder_isolation_browser.cjs
node test_deletion_browser.cjs
node test_retention_browser.cjs
node test_request_retention_browser.cjs
node test_boundary_browser.cjs
node test_supervisor_availability_browser.cjs
node test_boundary_permissions_browser.cjs
```

These cover collection/corrections, folder changes, dialogs, save conflicts,
permission persistence, removal recovery, and desktop/375px layouts.

For dedicated local PostgreSQL integration, set `TEST_POSTGRES_PORT` to the
disposable server and run:

```sh
python test_postgres.py
python test_collection_postgres.py
```

See those files for the remaining test-server defaults.
[Scheduling regression checks](../.github/workflows/regression-tests.yml)
runs on pull requests targeting `master` and manual dispatch using Python 3.12,
Node 22, and a local PostgreSQL 16 service. It runs the backend, solver,
frontend, PostgreSQL, and browser checks above without production credentials.

## Access and security behavior

Supervisors share one password; there are no per-supervisor accounts.
Admin bearer-token hashes and eight-hour expiries are persisted in the database,
and logout deletes the server session. Login uses constant-time password
comparison and process-local attempt limiting.

Employees do not authenticate by name. Shared-code possession grants one
submission, not access to another person's availability. Anonymous pages do not
list the roster or load responses by typed name. Edit links are specific to a
linked employee and folder; their token travels in a URL fragment removed before
redemption.

Collection cookies are HttpOnly, SameSite=Strict, two-hour sessions, and Secure
on Render/HTTPS. Writes require a session-bound CSRF header. Codes are encrypted
for supervisor display and independently HMAC-verified; edit/session tokens are
hashed. Quotas, revocation, version checks, and submission writes are checked on
the server under the shared transaction lock.

The app has bounded persistent collection rate buckets, a 64 KiB request-body
limit, API `no-store` responses, and referrer/framing/MIME-sniffing protections.
Forwarded IP headers are not trusted, so employees behind Render's proxy can
share an observed address and encounter rate limits. These controls do not prove
employee identity or constitute a complete security audit. A valid code holder
can consume its available responses. Retention is not a legal-compliance
certification.

## Files

| File / directory | Responsibility |
| --- | --- |
| `app.py` | Flask routes, supervisor authentication, availability editing, generation, snapshots, health check. |
| `models.py` | Peewee models, database selection, defaults/validation, locked initialization and migrations. |
| `collection_codes.py` | Code/session/CSRF flow, allowances, edit links, intake history, automatic saving. |
| `boundary.py` | Boundary definitions, permission context, qualifying preferred blocks. |
| `solver.py` | Weekday CP-SAT constraints, diagnostics, ordered optimization, fairness and slot assignment. |
| `weekend_generator.py` | Dated recurring weekend shifts and rotation. |
| `retention.py`, `request_retention.py` | Record-age eligibility and bounded cleanup on API use. |
| `cleanup_retention.py` | Guarded standalone preview/apply command. |
| `backup_db.py`, `seed.py`, `verify.py` | SQLite backup, disposable demo data, solver verification. |
| `public/index.html`, `public/admin.html`, `public/css/style.css` | Employee and supervisor pages/styles. |
| `public/js/employee.js` | Code gate, painting, employee permission choices, submit/edit flow. |
| `public/js/admin.js`, `folders.js`, `collection-codes.js` | Folder-scoped dashboard, scheduling/export, current availability, permissions, history, code management. |
| `test_*.py`, `test_*.cjs` | Backend, database, solver, and frontend/browser regressions. |
| `requirements.txt` | Pinned Python runtime dependencies. |
