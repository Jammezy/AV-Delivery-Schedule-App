# Workflows and scheduling behavior

A web app for collecting employee availability and creating weekday and weekend
delivery schedules. Employees enter a shared collection code and paint the hours
they can work or would prefer. Supervisors manage collection, rosters, settings,
and saved schedules in a password-protected dashboard.

The Flask backend serves the HTML/CSS/JavaScript frontend and stores records with
Peewee. Weekday scheduling uses Google's OR-Tools CP-SAT solver; weekend scheduling
uses a separate fixed-shift rotation generator. Local development can use SQLite;
the hosted app uses Render with PostgreSQL on Neon.

- [Employee form](https://av-delivery-schedule-app.onrender.com/)
- [Supervisor dashboard](https://av-delivery-schedule-app.onrender.com/admin.html)
- [Setup, configuration, maintenance, and tests](../flask-app/README.md)

## Collection and supervisor workflow

1. **Create or select a scheduling folder.** Each folder has its own roster,
   employee lead/hour attributes, scheduling settings, collection codes,
   availability, and saved schedules. New folders start with an empty roster and
   default settings. Selecting a folder changes the dashboard context; it does
   not change where existing collection codes save responses.
2. **Create a code in Codes and copy its invitation.** Each new code allows
   30 successful responses shared by everyone using it. Supervisors can add
   responses to that code or create another. The separate cumulative folder
   limit defaults to 100 for folders without a saved limit; existing saved limits
   are preserved. Raising a code's allowance does not raise the folder limit.
3. **Employees submit availability.** No employee account is needed. A valid code
   unlocks one submission, which saves automatically into that code's folder.
   Each ordinary submission creates a separate employee entry, including repeated
   names with numbered suffixes. Another sheet requires entering a code again.
4. **Review current availability and history.** Submissions opens with current
   availability. Supervisors can inspect preserved responses in the searchable
   response-history dialog, edit current availability and comments, and manage
   additional-shift permissions separately. A supervisor-created edit link lets
   an employee correct the same linked employee record and adds a correction to
   history; typing the same name into a new submission does not perform an edit.
5. **Check and generate schedules.** Set folder-specific hours, staffing,
   shift rules, and employee lead/minimum/maximum hours. Select submitted
   employees for weekday diagnostics and generation. Successful weekday results
   are saved automatically. Review and export them to Excel, or use Weekend for
   dated fixed assignments and rotation, then explicitly save its preview.

Codes may accept submissions into several folders at once. Revoking a code or
archiving its folder stops submissions. Archiving retains records until the
retention policy applies; permanent folder deletion uses a separate confirmation
with the folder's exact name and current record counts.

See [collection codes and edit links](design/COLLECTION_CODES.md) and
[folder isolation](design/FOLDER_ISOLATION.md) for implementation details.
The application guide describes current behavior where older review documents
contain rollout notes.

## Availability and weekday scheduling

Availability uses three levels: **0** = unavailable, **1** = can work,
**2** = would prefer. Preferred hours also count as available. The form collects
weekdays and weekends; the CP-SAT schedule and its export cover Monday–Friday.

The weekday model enforces availability, hourly staffing, contiguous daily shifts,
minimum/maximum shift length, employee weekly hour bounds, opening/closing caps,
optional clopening restrictions, and independently controlled day/late lead
coverage. Closing-hour settings determine which hours need staffing.

Its objectives run in this fixed order:

1. Maximize the lowest employee deal score.
2. Maximize total preferred hours assigned without reducing that fairness score.
3. Minimize opening/closing cap overruns allowed by qualifying permissions.
4. Spread opening/closing duties across employees.

All stages share one time budget. Later stages run only after the earlier
objective is proven optimal. A feasible schedule is retained if a later stage
times out, with a note describing which optimality guarantees were achieved.
The legacy `wFairness`, `wPreference`, and `wSpread` keys do not change this order.

### Fairness scoring

For each employee, the preference denominator is the minimum of preferred staffed
hours marked, their maximum weekly hours, and
`maxShiftLength × number of weekdays`. This is a scoring ceiling, not an exact
availability-aware calculation of the longest feasible week.

```text
preferenceScore = floor(1000 × preferred hours assigned / preference ceiling)
hourBurden = round(10 × max(0, staff needed − employees preferring the hour) / staff needed)
dealScore = preferenceScore − burdenWeight × total assigned burden
```

Unstaffed hours have no burden. An employee with a zero preference ceiling uses
a preference score of 1000 and is compared on burden. Raising the lowest deal
score encourages compensation for covering less popular hours within the hard
constraints; it does not guarantee an equal schedule or a particular percentage
of preferred hours. The dashboard and weekday Excel export show per-person
fairness results.

### Additional preferred opening and closing shifts

The optional `allowPreferredBoundaryExtras` setting defaults to off. When enabled,
recorded opening and closing permissions can allow qualifying fully preferred
boundary blocks to exceed their normal individual and combined caps. Weekly
hours, availability, shift lengths, and other hard constraints still apply.

Employees choose opening and closing permissions separately; supervisors can
explicitly save them through the separate permission controls. Shift-length
changes keep permission recorded while recalculating qualifying blocks. Changed
caps or staffed boundaries require reconfirmation. Extra shifts are optional
and are never guaranteed. See the [boundary scheduling guide](../flask-app/README.md#additional-shift-permissions).

## Weekend schedules

The Weekend tab schedules a date range using recurring Friday evening, Saturday,
and Sunday shifts, excluded dates, fixed assignments, and a rotating employee
pool. An employee must be available for the full shift and can work at most one
shift in a given weekend. This generator uses its own rotation logic rather than
the weekday fairness objectives or weekly hour/lead constraints. Unfilled shifts
are reported, and previews can be saved and reopened separately. The formatted
weekend preview and Excel download include dated assignments, shifts grouped by
employee, and a separate voluntary signup section for excluded weekend dates.
Excluded shifts skip fixed assignments and rotation and do not count as assigned
hours; the Excel signup sheet leaves employee cells blank for supervisors.

## Persistence, retention, and hosting

Availability, response history, folders, settings, server-side admin sessions,
and schedule snapshots are stored in the database. Saved weekday snapshots keep
the original inputs and results; reopening them does not regenerate with today's
settings.

**Scheduling records are not kept indefinitely.** Normal API requests trigger
bounded cleanup of eligible records strictly older than **18 calendar months**
in UTC. Current availability ages from its latest save; response history and
schedule snapshots retain their own timestamps. Older folders remain while they
contain retained children. No cleanup runs while the website is unused, and
exports, backups, and provider restore history are separate copies.

Render's Free service sleeps after 15 idle minutes and wakes on a visit, usually
taking about a minute. Neon compute also wakes automatically on a database query.
Sleep does not replace the application's retention policy. Usage limits, account
state, and provider policies still apply. See
[Render's free-service documentation](https://render.com/docs/free) and
[Neon's scale-to-zero documentation](https://neon.com/docs/introduction/scale-to-zero).

For startup, use a unique `ADMIN_PASSWORD` and configure
`COLLECTION_ENCRYPTION_KEY` and `COLLECTION_VERIFIER_KEY` for collection codes.
Hosted storage needs a persistent `DATABASE_URL`; Render's local filesystem
cannot preserve SQLite records across restarts or sleep. See the
[application guide](../flask-app/README.md) for complete setup and backup instructions.
