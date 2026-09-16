# ============================================================
# solver.py
#
# CP-SAT scheduling model, ported from Schedule_Maker_4000.py,
# extended with:
#   - three availability levels (0 unavailable / 1 available / 2 preferred)
#   - scarcity-weighted "burden" scoring for shifts nobody wants
#   - a fairness floor that normalizes across people who mark many
#     preferred hours vs. people who mark only a few
#   - diagnostics that name the binding constraint instead of
#     saying "infeasible"
# ============================================================

import math
from ortools.sat.python import cp_model

UNAVAILABLE = 0
AVAILABLE = 1
PREFERRED = 2

# Preference satisfaction is tracked in per-mille (0-1000) so CP-SAT
# can compare a person with 40 preferred hours against one with 5.
PREF_SCALE = 1000

# How undesirable a single hour can be, before burdenWeight is applied.
BURDEN_MAX = 10


# ------------------------------------------------------------------
# config / availability helpers
# ------------------------------------------------------------------

def hours_of(cfg):
    return list(range(int(cfg["hourStart"]), int(cfg["hourEnd"]) + 1))


def close_hour(day, cfg):
    """Hour this day stops being staffed, or None if it runs to hourEnd."""
    per_day = cfg.get("dayCloseHours") or {}
    if day in per_day and per_day[day] not in (None, ""):
        return int(per_day[day])
    if day == "Fri" and cfg.get("fridayCloseHour") not in (None, ""):
        return int(cfg["fridayCloseHour"])
    return None


def required_staff(day, h, cfg):
    ch = close_hour(day, cfg)
    if ch is not None and h >= ch:
        return 0
    return int(cfg["reqStaffOpen"]) if h < int(cfg["lateHourStart"]) else int(cfg["reqStaffLate"])


def level(availability, name, day, hour):
    """0 = can't work, 1 = can work, 2 = wants to work."""
    row = availability.get(name) or {}
    v = row.get("%s_%02d" % (day, hour), 0)
    if v is True:
        return AVAILABLE
    if v is False or v is None:
        return UNAVAILABLE
    try:
        v = int(v)
    except (TypeError, ValueError):
        return UNAVAILABLE
    if v >= 2:
        return PREFERRED
    return AVAILABLE if v == 1 else UNAVAILABLE


def slot_names_for(cfg):
    """Never return fewer slots than the largest staffing requirement,
    otherwise assign_slots() would silently drop scheduled people."""
    needed = max(int(cfg["reqStaffOpen"]), int(cfg["reqStaffLate"]), 1)
    names = [str(s) for s in (cfg.get("slotNames") or []) if str(s).strip()]
    names = names[:needed]
    while len(names) < needed:
        names.append("Slot %d" % (len(names) + 1))
    return names


def lead_slot_for(cfg, slot_names):
    want = cfg.get("leadSlotName")
    if want and want in slot_names:
        return want
    return slot_names[0]


def hour_label(h):
    return "%d%s" % (h % 12 or 12, "AM" if h < 12 else "PM")


def span_label(start, end):
    """end is inclusive as an hour-block start, so display end+1."""
    return "%s-%s" % (hour_label(start), hour_label(end + 1))


def _merge_runs(hours):
    """[7,8,9,14] -> [(7,9),(14,14)]"""
    out = []
    for h in sorted(hours):
        if out and h == out[-1][1] + 1:
            out[-1][1] = h
        else:
            out.append([h, h])
    return [(a, b) for a, b in out]


def _runs(flags):
    """Lengths of consecutive True runs."""
    lengths, cur = [], 0
    for f in flags:
        if f:
            cur += 1
        else:
            if cur:
                lengths.append(cur)
            cur = 0
    if cur:
        lengths.append(cur)
    return lengths


def max_workable_hours(employee, availability, cfg):
    """Upper bound on hours this person could legally work in a week.

    Accounts for the one-contiguous-shift-per-day rule and
    minShiftLength, which the old pre-check ignored.
    """
    name = employee["name"]
    hours = hours_of(cfg)
    min_shift = int(cfg["minShiftLength"])
    max_shift = int(cfg["maxShiftLength"])
    per_day = {}
    total = 0
    for d in cfg["days"]:
        flags = [
            level(availability, name, d, h) >= AVAILABLE and required_staff(d, h, cfg) > 0
            for h in hours
        ]
        usable = [r for r in _runs(flags) if r >= min_shift]
        best = min(max(usable), max_shift) if usable else 0
        per_day[d] = best
        total += best
    return min(total, int(employee.get("maxHours", 40))), per_day


# ------------------------------------------------------------------
# burden weights: how undesirable is each hour?
# ------------------------------------------------------------------

def burden_weights(employees, availability, cfg):
    """0 = plenty of people want this hour, BURDEN_MAX = nobody does.

    This is what makes taking the Thursday 7AM shift "cost" something,
    which the fairness floor then compensates for elsewhere.
    """
    weights = {}
    for d in cfg["days"]:
        for h in hours_of(cfg):
            req = required_staff(d, h, cfg)
            if req <= 0:
                weights[(d, h)] = 0
                continue
            wanted = sum(
                1 for e in employees
                if level(availability, e["name"], d, h) == PREFERRED
            )
            shortfall = max(0, req - wanted)
            weights[(d, h)] = int(round(BURDEN_MAX * shortfall / req))
    return weights


# ------------------------------------------------------------------
# diagnostics
# ------------------------------------------------------------------

def analyze(employees, availability, cfg):
    """Everything the admin needs to understand why a week is tight.

    Returns blockers (provably impossible), warnings (tight but maybe
    solvable), a per-hour coverage grid, and the preference demand map.
    """
    days = list(cfg["days"])
    hours = hours_of(cfg)
    n = len(employees)
    blockers = []
    warnings = []

    def blocker(code, message, **extra):
        blockers.append(dict(code=code, message=message, **extra))

    def warn(code, message, **extra):
        warnings.append(dict(code=code, message=message, **extra))

    # ---- config sanity -------------------------------------------------
    if int(cfg["hourEnd"]) < int(cfg["hourStart"]):
        blocker("config", "Last operating hour is earlier than opening hour.")
    if int(cfg["minShiftLength"]) > int(cfg["maxShiftLength"]):
        blocker("config", "Minimum shift length is longer than maximum shift length.")
    if not days:
        blocker("config", "No days are configured.")
    if int(cfg["reqStaffOpen"]) > len(slot_names_for(cfg)):
        warn("config", "More staff are required per hour than you have slot names; "
                       "extra columns will be auto-named.")
    if not n:
        blocker("roster", "Nobody has submitted availability yet.")
        return {
            "blockers": blockers, "warnings": warnings, "coverage": [],
            "demand": {}, "totals": {}, "people": [],
        }

    leads = [e for e in employees if e.get("isLead")]
    if cfg.get("requireLeadDuringOpen") and not leads:
        blocker("lead", "A lead is required during every open hour, but nobody on the "
                        "roster is marked as a lead. Mark at least one person on the "
                        "Employees tab.")

    # ---- per-hour coverage --------------------------------------------
    coverage = []
    demand = {d: {} for d in days}
    short_hours = {}       # day -> [hours where available < required]
    tight_hours = {}       # day -> [hours where available == required]
    no_lead_hours = {}     # day -> [open hours with no lead available]
    unwanted_hours = {}    # day -> [hours nobody marked preferred]

    for d in days:
        for h in hours:
            req = required_staff(d, h, cfg)
            can, can_lead, want = [], [], []
            for e in employees:
                lv = level(availability, e["name"], d, h)
                if lv >= AVAILABLE:
                    can.append(e["name"])
                    if e.get("isLead"):
                        can_lead.append(e["name"])
                if lv == PREFERRED:
                    want.append(e["name"])
            demand[d][h] = len(want)
            coverage.append({
                "day": d, "hour": h, "label": span_label(h, h),
                "required": req, "available": len(can),
                "availableLeads": len(can_lead), "preferred": len(want),
                "slack": len(can) - req if req else None,
                "closed": req == 0,
            })
            if req == 0:
                continue
            if len(can) < req:
                short_hours.setdefault(d, []).append((h, req - len(can), len(can)))
            elif len(can) == req:
                tight_hours.setdefault(d, []).append(h)
            if (cfg.get("requireLeadDuringOpen") and h < int(cfg["lateHourStart"])
                    and not can_lead):
                no_lead_hours.setdefault(d, []).append(h)
            if not want:
                unwanted_hours.setdefault(d, []).append(h)

    for d, entries in short_hours.items():
        by_hour = {h: (gap, have) for h, gap, have in entries}
        for a, b in _merge_runs(by_hour.keys()):
            worst = max(by_hour[h][0] for h in range(a, b + 1))
            have = min(by_hour[h][1] for h in range(a, b + 1))
            need = required_staff(d, a, cfg)
            blocker(
                "coverage",
                "%s %s: only %d %s available, but %d are needed. Short by %d."
                % (d, span_label(a, b), have, "person is" if have == 1 else "people are",
                   need, worst),
                day=d, startHour=a, endHour=b, have=have, need=need,
            )

    for d, hs in no_lead_hours.items():
        for a, b in _merge_runs(hs):
            blocker(
                "lead",
                "%s %s: no lead is available, and a lead is required during open hours."
                % (d, span_label(a, b)),
                day=d, startHour=a, endHour=b,
            )

    for d, hs in tight_hours.items():
        for a, b in _merge_runs(hs):
            blocker_free = required_staff(d, a, cfg)
            warn(
                "tight",
                "%s %s: exactly %d available for %d slots. Everyone free then must "
                "work it, so shift-length and hour caps have no room to maneuver."
                % (d, span_label(a, b), blocker_free, blocker_free),
                day=d, startHour=a, endHour=b,
            )

    for d, hs in unwanted_hours.items():
        for a, b in _merge_runs(hs):
            warn(
                "unwanted",
                "%s %s: nobody marked this as preferred. Whoever covers it earns "
                "fairness credit elsewhere." % (d, span_label(a, b)),
                day=d, startHour=a, endHour=b,
            )

    # ---- headcount arithmetic from the morning/evening caps ------------
    open_hour = int(cfg["hourStart"])
    late_hour = int(cfg["lateHourStart"])
    morning_slots = sum(required_staff(d, open_hour, cfg) for d in days)
    evening_slots = sum(required_staff(d, late_hour, cfg) for d in days)

    def cap_check(label, slots, cap, code):
        cap = int(cap)
        if cap <= 0 or slots <= 0:
            return
        if slots > n * cap:
            blocker(
                code,
                "%s: the week has %d %s to fill, but each person is capped at %d, so "
                "you need at least %d people on the roster. You have %d."
                % (label, slots, "slot" if slots == 1 else "slots", cap,
                   math.ceil(slots / cap), n),
                slots=slots, cap=cap, needPeople=math.ceil(slots / cap), havePeople=n,
            )

    cap_check("Opening shifts", morning_slots, cfg["maxMorningShifts"], "morningCap")
    cap_check("Closing shifts", evening_slots, cfg["maxEveningShifts"], "eveningCap")
    cap_check("Opening + closing shifts combined", morning_slots + evening_slots,
              cfg["maxMorningPlusEvening"], "combinedCap")

    # ---- total hours ---------------------------------------------------
    required_hours = sum(required_staff(d, h, cfg) for d in days for h in hours)
    people = []
    capacity = 0
    for e in employees:
        ceiling, per_day = max_workable_hours(e, availability, cfg)
        capacity += ceiling
        submitted = e["name"] in availability
        marked = sum(
            1 for d in days for h in hours
            if level(availability, e["name"], d, h) >= AVAILABLE
        )
        preferred = sum(
            1 for d in days for h in hours
            if level(availability, e["name"], d, h) == PREFERRED
        )
        people.append({
            "name": e["name"], "isLead": bool(e.get("isLead")),
            "minHours": int(e.get("minHours") or 0),
            "maxHours": int(e.get("maxHours") or 40),
            "submitted": submitted, "markedHours": marked,
            "preferredHours": preferred, "workableCeiling": ceiling,
            "perDayCeiling": per_day,
        })
        if not submitted:
            warn("missing", "%s hasn't submitted availability yet." % e["name"],
                 name=e["name"])
            continue
        if ceiling < int(e.get("minHours") or 0):
            blocker(
                "minHours",
                "%s has a %d hour weekly minimum, but their availability only supports "
                "%d hours once %d-hour minimum shifts and one-shift-per-day are applied."
                % (e["name"], int(e["minHours"]), ceiling, int(cfg["minShiftLength"])),
                name=e["name"], minHours=int(e["minHours"]), ceiling=ceiling,
            )
        elif marked and ceiling == 0:
            warn(
                "fragmented",
                "%s marked %d hours, but none form a block of %d consecutive hours, so "
                "they can't be scheduled at all."
                % (e["name"], marked, int(cfg["minShiftLength"])),
                name=e["name"],
            )

    if required_hours > capacity:
        blocker(
            "capacity",
            "The week needs %d staff-hours, but everyone's availability and hour caps "
            "together only supply %d. You're %d hours short."
            % (required_hours, capacity, required_hours - capacity),
            requiredHours=required_hours, capacityHours=capacity,
        )

    min_hours_total = sum(int(e.get("minHours") or 0) for e in employees)
    if min_hours_total > required_hours:
        blocker(
            "minHoursTotal",
            "Weekly minimums add up to %d hours, but the schedule only has %d hours of "
            "work in it. Lower some minimums or extend your hours."
            % (min_hours_total, required_hours),
            minTotal=min_hours_total, requiredHours=required_hours,
        )

    return {
        "blockers": blockers,
        "warnings": warnings,
        "coverage": coverage,
        "demand": demand,
        "people": people,
        "totals": {
            "people": n,
            "leads": len(leads),
            "requiredHours": required_hours,
            "capacityHours": capacity,
            "morningSlots": morning_slots,
            "eveningSlots": evening_slots,
            "minHoursTotal": min_hours_total,
        },
    }


# ------------------------------------------------------------------
# model
# ------------------------------------------------------------------

def _build(employees, availability, cfg, relax=False):
    days = list(cfg["days"])
    hours = hours_of(cfg)
    n = len(employees)
    model = cp_model.CpModel()

    work = {}
    for i in range(n):
        for d in days:
            for h in hours:
                work[(i, d, h)] = model.NewBoolVar("w_%d_%s_%d" % (i, d, h))

    shortfall = {}

    # --- staffing + lead coverage
    for d in days:
        for h in hours:
            req = required_staff(d, h, cfg)
            if req == 0:
                for i in range(n):
                    model.Add(work[(i, d, h)] == 0)
                continue
            staffed = sum(work[(i, d, h)] for i in range(n))
            if relax:
                s = model.NewIntVar(0, req, "short_%s_%d" % (d, h))
                shortfall[(d, h)] = s
                model.Add(staffed + s == req)
            else:
                model.Add(staffed == req)
            if h < int(cfg["lateHourStart"]) and cfg.get("requireLeadDuringOpen"):
                lead_vars = [work[(i, d, h)] for i, e in enumerate(employees) if e.get("isLead")]
                if lead_vars:
                    model.Add(sum(lead_vars) >= 1)

    # --- one contiguous shift per day
    for i in range(n):
        for d in days:
            starts = []
            for idx, h in enumerate(hours):
                is_start = model.NewBoolVar("s_%d_%s_%d" % (i, d, h))
                starts.append(is_start)
                if idx == 0:
                    model.Add(is_start == work[(i, d, h)])
                else:
                    model.Add(is_start >= work[(i, d, h)] - work[(i, d, hours[idx - 1])])
            model.Add(sum(starts) <= 1)

    # --- shift length bounds
    for i in range(n):
        for d in days:
            daily = sum(work[(i, d, h)] for h in hours)
            active = model.NewBoolVar("a_%d_%s" % (i, d))
            model.Add(daily > 0).OnlyEnforceIf(active)
            model.Add(daily == 0).OnlyEnforceIf(active.Not())
            model.Add(daily >= int(cfg["minShiftLength"])).OnlyEnforceIf(active)
            model.Add(daily <= int(cfg["maxShiftLength"])).OnlyEnforceIf(active)

    # --- evening block is all-or-nothing
    late = [h for h in hours if h >= int(cfg["lateHourStart"])]
    for i in range(n):
        for d in days:
            open_late = [h for h in late if required_staff(d, h, cfg) > 0]
            for a, b in zip(open_late, open_late[1:]):
                model.Add(work[(i, d, a)] == work[(i, d, b)])

    # --- no closing then opening the next morning
    if cfg.get("blockClopening"):
        for i in range(n):
            for k in range(len(days) - 1):
                d, nxt = days[k], days[k + 1]
                open_hours = [h for h in hours if required_staff(d, h, cfg) > 0]
                if not open_hours:
                    continue
                last = max(open_hours)
                for mh in [h for h in hours[:3] if required_staff(nxt, h, cfg) > 0]:
                    model.Add(work[(i, d, last)] + work[(i, nxt, mh)] <= 1)

    # --- availability, weekly hours, opening/closing caps
    min_slack = {}
    pref_vars = {}
    for i, e in enumerate(employees):
        name = e["name"]
        prefs = []
        for d in days:
            for h in hours:
                lv = level(availability, name, d, h)
                if lv == UNAVAILABLE:
                    model.Add(work[(i, d, h)] == 0)
                elif lv == PREFERRED and required_staff(d, h, cfg) > 0:
                    prefs.append(work[(i, d, h)])
        pref_vars[i] = prefs

        weekly = sum(work[(i, d, h)] for d in days for h in hours)
        lo = int(e.get("minHours") or 0)
        if relax and lo > 0:
            slack = model.NewIntVar(0, lo, "minslack_%d" % i)
            min_slack[i] = slack
            model.Add(weekly + slack >= lo)
        else:
            model.Add(weekly >= lo)
        model.Add(weekly <= int(e.get("maxHours") or 40))

        open_hour = int(cfg["hourStart"])
        late_hour = int(cfg["lateHourStart"])
        mornings = [work[(i, d, open_hour)] for d in days
                    if required_staff(d, open_hour, cfg) > 0]
        evenings = [work[(i, d, late_hour)] for d in days
                    if required_staff(d, late_hour, cfg) > 0]
        if mornings:
            model.Add(sum(mornings) <= int(cfg["maxMorningShifts"]))
        if evenings:
            model.Add(sum(evenings) <= int(cfg["maxEveningShifts"]))
        if mornings or evenings:
            model.Add(sum(mornings) + sum(evenings) <= int(cfg["maxMorningPlusEvening"]))

    return {
        "model": model, "work": work, "days": days, "hours": hours,
        "prefVars": pref_vars, "shortfall": shortfall, "minSlack": min_slack,
    }


def _add_fairness(built, employees, availability, cfg):
    """The fairness section.

    Two numbers combine into one score per person:

      pref_score  how much of what you asked for you got, as a share of what
                  you could realistically have gotten. Dividing by your own
                  ceiling is what puts someone who marked 5 preferred hours on
                  equal footing with someone who marked 40 - both are measured
                  against their own ask, not against each other's.

      burden      hours you worked that nobody wanted, each weighted by how far
                  preference demand fell short of the staffing requirement for
                  that hour. An hour four people are needed for and nobody
                  asked for is worth the full BURDEN_MAX; an hour everyone
                  wants is worth nothing.

      deal = pref_score - burdenWeight * burden

    The objective lifts the *lowest* deal score on the roster. So covering the
    Thursday 7AM nobody wants drops you to the bottom, and the only way the
    solver can raise the floor again is to hand you more of your preferred
    hours somewhere else in the week.

    Everything here is written as one-sided bounds rather than equalities.
    Since we're maximizing, the solver pushes each score up to its true value
    on its own, and the looser encoding searches noticeably faster.
    """
    model = built["model"]
    work = built["work"]
    days, hours = built["days"], built["hours"]
    n = len(employees)

    weights = burden_weights(employees, availability, cfg)
    burden_weight = int(cfg.get("burdenWeight", 3))
    max_week = int(cfg["maxShiftLength"]) * len(days)
    floor_lo = -burden_weight * BURDEN_MAX * max_week

    worst = model.NewIntVar(floor_lo, PREF_SCALE, "fairness_floor")
    satisfied_exprs = []
    meta = []

    for i, e in enumerate(employees):
        prefs = built["prefVars"][i]
        marked = len(prefs)
        ceiling = min(marked, int(e.get("maxHours") or 40), max_week)
        satisfied = sum(prefs) if prefs else 0
        satisfied_exprs.append(satisfied)

        burden = sum(
            weights[(d, h)] * work[(i, d, h)]
            for d in days for h in hours if weights.get((d, h))
        )

        if ceiling > 0:
            pref_score = model.NewIntVar(0, PREF_SCALE, "pref_%d" % i)
            # pref_score <= PREF_SCALE * satisfied / ceiling, kept linear
            model.Add(pref_score * ceiling <= PREF_SCALE * satisfied)
            model.Add(worst <= pref_score - burden_weight * burden)
        else:
            # Marked no preferences, so there's nothing to satisfy. Score them
            # on burden alone; otherwise they'd pin the floor at zero and the
            # solver would stop trying to help anyone.
            model.Add(worst <= PREF_SCALE - burden_weight * burden)
        meta.append({"marked": marked, "ceiling": ceiling})

    # share opening/closing duty across as many people as possible
    spread = []
    open_hour, late_hour = int(cfg["hourStart"]), int(cfg["lateHourStart"])
    for i in range(n):
        for tag, hour in (("m", open_hour), ("e", late_hour)):
            src = [work[(i, d, hour)] for d in days if required_staff(d, hour, cfg) > 0]
            if not src:
                continue
            flag = model.NewBoolVar("%s_any_%d" % (tag, i))
            model.Add(flag <= sum(src))
            spread.append(flag)

    built["meta"] = meta
    built["worst"] = worst
    built["weights"] = weights
    built["objective"] = (
        int(cfg.get("wFairness", 100)) * worst
        + int(cfg.get("wPreference", 2)) * sum(satisfied_exprs)
        + (int(cfg.get("wSpread", 20)) * sum(spread) if spread else 0)
    )
    return built


def fairness_report(work_out, employees, availability, cfg, weights):
    """Computed from the finished schedule rather than read back off solver
    variables, so the numbers are identical whether the optimizer finished or
    timed out and we fell back to the first feasible schedule."""
    days, hours = list(cfg["days"]), hours_of(cfg)
    burden_weight = int(cfg.get("burdenWeight", 3))
    max_week = int(cfg["maxShiftLength"]) * len(days)

    rows = []
    for e in employees:
        name = e["name"]
        worked = [(d, h) for d in days for h in hours if name in work_out[d][h]]
        marked = sum(
            1 for d in days for h in hours
            if level(availability, name, d, h) == PREFERRED and required_staff(d, h, cfg) > 0
        )
        ceiling = min(marked, int(e.get("maxHours") or 40), max_week)
        got = sum(1 for d, h in worked if level(availability, name, d, h) == PREFERRED)
        burden = sum(weights.get((d, h), 0) for d, h in worked)
        pref_score = int(PREF_SCALE * got / ceiling) if ceiling else PREF_SCALE
        rows.append({
            "name": name,
            "hours": len(worked),
            "preferredMarked": marked,
            "preferredCeiling": ceiling,
            "preferredSatisfied": got,
            "preferredPct": round(100 * got / ceiling) if ceiling else None,
            "burdenPoints": burden,
            "unwantedHours": sum(1 for d, h in worked if weights.get((d, h), 0) > 0),
            "dealScore": pref_score - burden_weight * burden,
        })
    rows.sort(key=lambda r: r["dealScore"])
    return rows


def _solve(model, cfg, seed=None, minimize=None, maximize=None, budget=None):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(
        budget if budget is not None else cfg.get("solverTimeLimit", 30)
    )
    solver.parameters.num_search_workers = int(cfg.get("solverWorkers", 8))
    if seed is not None:
        solver.parameters.random_seed = int(seed)
    if minimize is not None:
        model.Minimize(minimize)
    if maximize is not None:
        model.Maximize(maximize)
    return solver, solver.Solve(model)


# ------------------------------------------------------------------
# public entry point
# ------------------------------------------------------------------

def _extract(solver, built, employees):
    days, hours, work = built["days"], built["hours"], built["work"]
    out = {d: {h: [] for h in hours} for d in days}
    weekly = {e["name"]: 0 for e in employees}
    for d in days:
        for h in hours:
            for i, e in enumerate(employees):
                if solver.Value(work[(i, d, h)]):
                    out[d][h].append(e["name"])
                    weekly[e["name"]] += 1
    return out, weekly


def generate_schedule(employees, availability, cfg, seed=None):
    """Solved in two phases.

    Phase 1 looks for any legal schedule with no objective at all, which is
    fast. Phase 2 hands that solution back as a hint and spends the rest of the
    budget improving fairness.

    Splitting it this way matters for correctness, not just speed: with a
    single objective-driven solve, a week that is perfectly schedulable but
    slow to optimize returns UNKNOWN at the time limit, and the old code
    reported that to you as 'no valid schedule exists.' Now an unfinished
    optimization degrades to a valid-but-less-fair schedule instead of a
    false impossibility.
    """
    diagnostics = analyze(employees, availability, cfg)
    if diagnostics["blockers"]:
        return {"status": "IMPOSSIBLE", "diagnostics": diagnostics}

    budget = float(cfg.get("solverTimeLimit", 30))
    built = _build(employees, availability, cfg)

    phase1_budget = max(5.0, budget * 0.35)
    s1, st1 = _solve(built["model"], cfg, seed=seed, budget=phase1_budget)
    if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "INFEASIBLE",
            "diagnostics": diagnostics,
            "relaxation": explain_infeasible(employees, availability, cfg),
        }

    work_out, weekly = _extract(s1, built, employees)
    optimized = False
    elapsed = s1.WallTime()

    _add_fairness(built, employees, availability, cfg)
    for key, var in built["work"].items():
        built["model"].AddHint(var, s1.Value(var))

    remaining = max(2.0, budget - elapsed)
    s2, st2 = _solve(built["model"], cfg, seed=seed,
                     maximize=built["objective"], budget=remaining)
    if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        work_out, weekly = _extract(s2, built, employees)
        optimized = True
        elapsed += s2.WallTime()

    weights = built["weights"]
    fairness = fairness_report(work_out, employees, availability, cfg, weights)
    days, hours = built["days"], built["hours"]

    if not optimized:
        note = ("Found a valid schedule, but ran out of time before balancing "
                "preferences. Raise the solver time limit in Settings for a "
                "fairer split.")
    elif st2 == cp_model.OPTIMAL:
        note = None
    else:
        note = ("Valid schedule, balanced as far as the time limit allowed. "
                "Raising the solver time limit may improve the fairness floor.")

    return {
        "status": "OPTIMAL" if (optimized and st2 == cp_model.OPTIMAL) else "FEASIBLE",
        "optimized": optimized,
        "note": note,
        "work": work_out,
        "schedule": assign_slots(work_out, employees, cfg),
        "weeklyHours": weekly,
        "fairness": fairness,
        "fairnessFloor": min((r["dealScore"] for r in fairness), default=0),
        "burdenMap": {d: {h: weights.get((d, h), 0) for h in hours} for d in days},
        "diagnostics": diagnostics,
        "solveSeconds": round(elapsed, 2),
    }


def explain_infeasible(employees, availability, cfg):
    """Re-solve allowing understaffing and missed minimums, then report
    exactly where the model had to cheat. This turns 'no valid schedule
    exists' into 'you're one person short Tuesday 7-9AM'."""
    built = _build(employees, availability, cfg, relax=True)
    model = built["model"]
    penalty = (
        100 * sum(built["shortfall"].values())
        + sum(built["minSlack"].values())
    )
    solver, status = _solve(model, cfg, minimize=penalty)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "solved": False,
            "message": "Even with staffing requirements relaxed, no schedule fits. "
                       "The shift-length, hour-cap, or clopening rules are conflicting "
                       "with what people submitted.",
            "gaps": [], "missedMinimums": [],
        }

    per_day = {}
    for (d, h), var in built["shortfall"].items():
        v = solver.Value(var)
        if v:
            per_day.setdefault(d, {})[h] = v

    gaps = []
    for d, by_hour in per_day.items():
        for a, b in _merge_runs(by_hour.keys()):
            gaps.append({
                "day": d, "startHour": a, "endHour": b,
                "short": max(by_hour[h] for h in range(a, b + 1)),
                "message": "%s %s: short %d %s."
                           % (d, span_label(a, b),
                              max(by_hour[h] for h in range(a, b + 1)),
                              "person" if max(by_hour[h] for h in range(a, b + 1)) == 1
                              else "people"),
            })

    missed = []
    for i, var in built["minSlack"].items():
        v = solver.Value(var)
        if v:
            missed.append({
                "name": employees[i]["name"], "short": v,
                "message": "%s falls %d hours short of their %d hour minimum."
                           % (employees[i]["name"], v, int(employees[i]["minHours"])),
            })

    return {
        "solved": True,
        "gaps": sorted(gaps, key=lambda g: (cfg["days"].index(g["day"]), g["startHour"])),
        "missedMinimums": missed,
        "message": "Here's the smallest set of rules that would have to bend:",
    }


# ------------------------------------------------------------------
# slot assignment (Formatter_Pro continuity logic)
# ------------------------------------------------------------------

def assign_slots(work, employees, cfg):
    slot_names = slot_names_for(cfg)
    lead_slot = lead_slot_for(cfg, slot_names)
    lead_map = {e["name"]: bool(e.get("isLead")) for e in employees}
    hours = hours_of(cfg)

    schedule = {}
    for day in cfg["days"]:
        schedule[day] = {}
        prev_map = {}
        for h in hours:
            on_shift = [{"name": nm, "isLead": lead_map.get(nm, False)}
                        for nm in work[day][h]]
            slots = {s: "" for s in slot_names}
            assigned = set()

            # keep people in the same column hour to hour
            for p in on_shift:
                slot = prev_map.get(p["name"])
                if not slot or slot not in slots:
                    continue
                if slot == lead_slot and not p["isLead"]:
                    continue
                if slots[slot] == "":
                    slots[slot] = p["name"]
                    assigned.add(p["name"])

            if slots[lead_slot] == "":
                free_leads = [p for p in on_shift
                              if p["isLead"] and p["name"] not in assigned]
                if free_leads:
                    slots[lead_slot] = free_leads[0]["name"]
                    assigned.add(free_leads[0]["name"])
                else:
                    for s in slot_names:
                        if s != lead_slot and slots[s] and lead_map.get(slots[s]):
                            slots[lead_slot] = slots[s]
                            slots[s] = ""
                            break

            leftover = [p for p in on_shift if p["name"] not in assigned]
            leftover.sort(key=lambda p: p["isLead"], reverse=True)
            empty = [s for s in slot_names if slots[s] == ""]
            for p, s in zip(leftover, empty):
                slots[s] = p["name"]
                assigned.add(p["name"])

            schedule[day][h] = slots
            prev_map = {nm: s for s, nm in slots.items() if nm}

    return schedule
