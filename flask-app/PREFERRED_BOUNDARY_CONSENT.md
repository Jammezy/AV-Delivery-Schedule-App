# Preferred boundary consent and fairness-first scheduling

Employees can separately accept additional fully preferred openings and closings.
The supervisor must also enable **Allow additional fully preferred opening/closing
shifts**. Both employee choices and the supervisor switch default to false.
Consent permits an assignment; it does not request or guarantee one. Weekend
generation is unchanged.

## Agreement and persistence

`FolderAvailability` stores `allow_extra_openings`, `allow_extra_closings`, and
`consent_context` separately from the hour grid, keyed by employee and folder IDs.
The employee reload and authenticated supervisor submission response include both
recorded choices and effective status. The selected-employee viewer explains
disabled settings, missing qualifying blocks, and reconfirmation requirements.
Public submission context contains definitions, not other employees' consent.
Employee identity remains the application's existing name-based identification;
this feature does not introduce employee authentication.

`boundary.py` owns the day-specific staffing calendar and candidate calculation.
Opening qualification requires every hour of the configured minimum shift from
the staffed opening to be preferred. Closing qualification requires the whole
staffed evening block to be preferred, plus available preceding hours when the
minimum shift is longer. A too-short staffed day cannot create a shortened
minimum. Friday closing before the evening start creates no closing candidate.
Invalid or missing levels do not qualify in the consent helper.

The server hashes canonical caps, minimum shift length, and day-specific blocks.
Changing these requires reconfirmation while retaining the recorded choices.
Staffing count changes that leave the blocks unchanged, solver settings, and
feature enablement do not invalidate the agreement. The server recomputes the
context inside the same write transaction as the existing folder revision check,
grid, comment, choices, and timestamp. Stale submissions receive HTTP 409 without
partial writes. The form preserves the draft and requires explicit reconfirmation.
Legacy payloads without choices save false. Admin edits preserve choices and reject
consent fields. Changes to availability immediately change qualifying candidates.

Generation freezes the selected folder's consent, config, and submissions under
the existing lock; solver work occurs outside the transaction. The saved snapshot
uses those frozen values even if settings/submissions change during solving.
Saved result explanations and the Excel **Boundary consent** sheet use their
captured counts, caps, qualifying assigned credits, and consent context.

## Hard constraints and objective

With normal caps M, E, B, assigned openings O, closings C, and qualifying **assigned**
credits QO and QC, the hard constraints are:

```
O - QO <= M
C - QC <= E
O + C - QO - QC <= B
```

Each credit is the corresponding day's assignment variable, included only when
the feature, current-context consent for that type, and complete preferred block
all qualify. Disabled or missing consent gives zero credit. Individual and
combined constraints overlap; a credit is not consumed from a separate pool.
All availability, full-evening coverage, continuity, minimum/maximum shift lengths,
weekly hours, lead requirements, and closing-to-opening restrictions still apply.
The implementation also fixes the old `maxHours=0` fallback to 40 in weekday model
and fairness calculations. Pre-checks use a safe candidate-credit upper bound;
candidates are not assumed to coexist in a feasible schedule. The relaxation
diagnostic retains consent qualification and boundary constraints.

Optimization now uses four ordered integer objectives:

1. Maximize the existing minimum employee deal score.
2. Maximize total assigned preferred hours without reducing that score.
3. Minimize the sum of positive opening, closing, and combined cap overruns.
4. Maximize the number of employees sharing opening and closing duties.

The preserved deal score is `floor(1000 * assignedPreferred / ceiling) -
burdenWeight * burdenPoints`. The ceiling is the minimum of preferred staffed
hours marked, employee weekly maximum, and maximum shift length times days.
With no preferred ceiling the preference component is 1000. Burden uses the
existing per-hour scarcity weights (0–10), including **every** assigned boundary
hour and every extra; there is no consent bonus. This preserves the actual
existing metric, rather than introducing a new fairness definition.

The model computes score inputs once. Each proven optimum is locked by an integer
bound before the next objective. One monotonic deadline covers the phases and
any infeasibility explanation. Each phase retains its feasible incumbent. An
unproved fairness phase receives the remaining search budget, returns FEASIBLE
with `fairnessOptimal=false`, and skips refinement. Later timeouts return the
latest incumbent that obeys all earlier locks. OPTIMAL requires all four proofs.
Legacy `wFairness`, `wPreference`, and `wSpread` values remain readable for old
configs but no longer control weekday trade-offs; their obsolete controls are
removed. `burdenWeight` still defines the fairness metric.

## Migration and rollback

Startup adds the three columns if absent using Peewee's SQLite/PostgreSQL
migrators inside the existing serialized migration transaction. Existing rows
receive false/false/null. Repeated startup is safe; no table reset or production
data operation is required. Before a future deployment, follow the existing backup
and restore verification procedure. This feature branch does not deploy or change
Render/Neon settings.

To disable the feature, turn off its supervisor switch. For code rollback, retain
the additive columns and roll back application code; the older model ignores
them. Do not drop columns or reset data. Old code does not maintain this consent
context, so invalidate/reconfirm recorded consent before subsequently re-enabling
extras after a period running old code.

## Reproducible checks

All database tests below isolate their data. Never use production credentials.

```
python -m unittest -v test_app test_boundary
node test_frontend.cjs
python verify.py
python test_postgres.py
node test_deletion_browser.cjs
node test_boundary_browser.cjs
```

Install `requirements.txt` and the existing test dependencies
`jsdom@30.1.0 exceljs@4.4.0 playwright@1.58.2`; install Playwright Chromium.
`test_postgres.py` requires `TEST_POSTGRES_PORT` for a dedicated local server with
the `recovery_test` role and creates/drops only a unique disposable database.
Browser checks overwrite `DATABASE_URL`, create temporary SQLite databases,
and use `PYTHON` when set. They verify desktop (1280px) and narrow (375px)
layouts and write screenshots to `test-browser-output/`.

The tests cover independent consent/revocation, legacy records, strict booleans,
atomic conflicts, folder/employee isolation, admin preservation, additive migration,
snapshot freezing during a concurrent change, complete/partial blocks, minimum
lengths 2/3/4, zero caps, combined-only overruns, assigned-only credits, and hourly
limits. Exhaustive tiny-model enumeration compares the entire ordered objective
tuple; timeout injection checks each phase and the shared decreasing budget.
Browser tests exercise keyboard consent, save/reload, feature off/on generation,
changed-limit reconfirmation, revocation, viewer status, and saved snapshot history.
Frontend tests include stale refreshes, retained conflict drafts, duplicate sends,
and reopening actual XLSX bytes to check captured consent values.

For independent review, run the disposable browser fixture and inspect the two
employees: Alex prefers three complete openings and opts into openings only;
Blair prefers three complete evenings and opts into closings only. Normal caps
are M=2, E=1, B=2, with a two-hour minimum. Compare generation with the switch off
and on. Extras need not appear. If they do, inspect their corresponding assigned
credits. Change a cap, observe reconfirmation, revoke Alex's choice, and open the
earlier saved result: its explanation must retain its original agreement.
