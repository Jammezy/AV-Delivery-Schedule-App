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
spread. The four weights (`wFairness`, `wPreference`, `wSpread`, `burdenWeight`)
are all editable in Settings if you want to retune that trade.

The Generate tab shows the per-person breakdown, and it's written to a
**Fairness** sheet in the Excel export so you have something to point at when
someone asks why they got the Thursday open.

## The week check

Three heatmaps and two lists, all computed by arithmetic before the solver runs:

- **Coverage** — people free vs. people needed, per hour. Red means provably
  impossible, amber means exactly enough with zero slack.
- **Demand** — how many people asked for each hour.
- **Burden** — what covering each hour costs in fairness terms.

The blocker list catches things the old pre-check missed:

- An hour where fewer people are available than the staffing requirement, merged
  into ranges so you get "Tue 7AM–9AM" rather than three separate lines.
- An open hour with no lead available, when a lead is required.
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

Two phases. Phase one looks for any legal schedule with no objective, which is
fast. Phase two feeds that solution back as a hint and spends the rest of the
time budget improving fairness.

This is about correctness, not just speed. With a single objective-driven solve,
a week that's perfectly schedulable but slow to optimize returns UNKNOWN at the
time limit — and the old code reported that to you as "no valid schedule
exists." Now an unfinished optimization degrades to a valid-but-less-balanced
schedule with a note, instead of a false impossibility.

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
- Login is rate-limited to 8 attempts per 15 minutes and uses a constant-time
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
