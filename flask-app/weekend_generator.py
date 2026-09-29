import datetime
from collections import defaultdict

# Fixed recurring shifts
SHIFTS = [
    {"key": "friday_evening", "day_offset": 0, "start": 19, "end": 22},
    {"key": "saturday_morning", "day_offset": 1, "start": 7, "end": 12},
    {"key": "saturday_afternoon", "day_offset": 1, "start": 12, "end": 17},
    {"key": "saturday_evening", "day_offset": 1, "start": 17, "end": 22},
    {"key": "sunday_morning", "day_offset": 2, "start": 7, "end": 12},
    {"key": "sunday_afternoon", "day_offset": 2, "start": 12, "end": 17},
]

def _is_available(parsed_avail, name, shift):
    """
    Check if employee 'name' is fully available for 'shift'.
    parsed_avail is dict[name] -> set of available interval keys (e.g., "Sat_07")
    """
    avail = parsed_avail.get(name, set())
    day = "Fri" if shift["day_offset"] == 0 else "Sat" if shift["day_offset"] == 1 else "Sun"
    for h in range(shift["start"], shift["end"]):
        if f"{day}_{h:02d}" not in avail:
            return False
    return True

def generate_weekend_schedule(config, availability_dict, roster):
    """
    config:
      start_date: str 'YYYY-MM-DD'
      end_date: str 'YYYY-MM-DD'
      excluded_dates: list of str 'YYYY-MM-DD'
      fixed_assignments: dict mapping shift_key -> employee_id
      rotating_employees: list of employee_ids in original order
      shift_starting_person: bool (default True)

    availability_dict: dict mapping employee_name -> dict {"Sat_07": 1, ...}
    roster: list of dicts with {"id": int, "name": str}
    """
    start_date = datetime.datetime.strptime(config["start_date"], "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(config["end_date"], "%Y-%m-%d").date()
    holidays = set()
    for excl in config.get("excluded_dates", []):
        if isinstance(excl, dict): # Handle dicts just in case
            holidays.add(datetime.datetime.strptime(excl["date"], "%Y-%m-%d").date())
        else:
            holidays.add(datetime.datetime.strptime(excl, "%Y-%m-%d").date())

    fixed_assignments = config.get("fixed_assignments", {})
    rotating_pool = config.get("rotating_employees", [])
    shifting_mode = config.get("shift_starting_person", True)

    emp_by_id = {e["id"]: e for e in roster}
    emp_by_name = {e["name"]: e for e in roster}

    # 1. Parse availability
    parsed_avail = {}
    for name, intervals_data in availability_dict.items():
        avail = set()
        for k, v in intervals_data.items():
            if v > 0:
                avail.add(k)
        parsed_avail[name] = avail

    # 2. Construct dated shifts in chronological order, excluding holidays
    dated_shifts = []
    current = start_date
    while current <= end_date:
        if current.weekday() == 4: # Friday
            friday = current
            for s in SHIFTS:
                s_date = friday + datetime.timedelta(days=s["day_offset"])
                if start_date <= s_date <= end_date and s_date not in holidays:
                    dated_shifts.append({
                        "friday": friday.isoformat(),
                        "date": s_date.isoformat(),
                        "shift": s,
                        "origin": None,
                        "assigned": None,
                    })
        elif current == start_date and current.weekday() in [5, 6]: # partial first weekend
            friday = current - datetime.timedelta(days=(current.weekday() - 4))
            for s in SHIFTS:
                s_date = friday + datetime.timedelta(days=s["day_offset"])
                if start_date <= s_date <= end_date and s_date not in holidays:
                    dated_shifts.append({
                        "friday": friday.isoformat(),
                        "date": s_date.isoformat(),
                        "shift": s,
                        "origin": None,
                        "assigned": None,
                    })
        current += datetime.timedelta(days=1)

    # 3. Validate and place fixed assignments
    occupancy = defaultdict(set) # friday_str -> set(emp_id)

    fixed_employees = set()
    for shift_key, emp_id in fixed_assignments.items():
        if emp_id in emp_by_id:
            fixed_employees.add(emp_id)

    for ds in dated_shifts:
        k = ds["shift"]["key"]
        emp_id = fixed_assignments.get(k)
        if emp_id and emp_id in emp_by_id:
            name = emp_by_id[emp_id]["name"]

            # Check availability
            if not _is_available(parsed_avail, name, ds["shift"]):
                ds["unfilled_reason"] = f"Fixed employee {name} is unavailable."
                continue

            # Check double assignment in weekend
            if emp_id in occupancy[ds["friday"]]:
                ds["unfilled_reason"] = f"Fixed employee {name} is already working this weekend."
                continue

            ds["assigned"] = emp_id
            ds["origin"] = "fixed"
            occupancy[ds["friday"]].add(emp_id)

    # 4. Form effective rotating order
    open_shifts = [ds for ds in dated_shifts if not ds.get("assigned")]

    effective_pool = []
    for emp_id in rotating_pool:
        if emp_id in fixed_employees or emp_id not in emp_by_id:
            continue

        name = emp_by_id[emp_id]["name"]
        can_cover_any = False
        for os in open_shifts:
            if _is_available(parsed_avail, name, os["shift"]):
                can_cover_any = True
                break

        if can_cover_any:
            effective_pool.append(emp_id)

    # 5. Initialize counts
    actual_rotating = defaultdict(int)

    # Group open shifts by weekend
    weekend_open = defaultdict(list)
    for ds in open_shifts:
        weekend_open[ds["friday"]].append(ds)

    # 6. Assign open shifts as a batch per weekend
    for friday in sorted(weekend_open.keys()):
        shifts = weekend_open[friday]

        best_assignment = None
        best_count = -1

        def search(shift_idx, current_assign, current_occ, current_counts):
            nonlocal best_assignment, best_count
            if shift_idx == len(shifts):
                filled = len(current_assign)
                if filled > best_count:
                    best_count = filled
                    best_assignment = current_assign.copy()
                return

            shift_to_fill = shifts[shift_idx]

            # Find eligible candidates
            candidates = []
            for emp_id in effective_pool:
                if emp_id not in current_occ:
                    name = emp_by_id[emp_id]["name"]
                    if _is_available(parsed_avail, name, shift_to_fill["shift"]):
                        r = current_counts[emp_id]
                        orig_idx = effective_pool.index(emp_id)
                        if shifting_mode:
                            rank = (orig_idx - r) % len(effective_pool)
                        else:
                            rank = orig_idx
                        candidates.append((r, rank, emp_id))

            candidates.sort() # sort by count (r), then rank

            # Try filling
            for r, rank, emp_id in candidates:
                current_assign[shift_idx] = emp_id
                current_occ.add(emp_id)
                current_counts[emp_id] += 1

                search(shift_idx + 1, current_assign, current_occ, current_counts)

                current_counts[emp_id] -= 1
                current_occ.remove(emp_id)
                del current_assign[shift_idx]

            # Try leaving unfilled
            search(shift_idx + 1, current_assign, current_occ, current_counts)

        search(0, {}, occupancy[friday].copy(), actual_rotating.copy())

        # Apply best assignment
        if best_assignment:
            for shift_idx, emp_id in best_assignment.items():
                shifts[shift_idx]["assigned"] = emp_id
                shifts[shift_idx]["origin"] = "rotating"
                occupancy[friday].add(emp_id)
                actual_rotating[emp_id] += 1

        # Handle unfillable reasons
        for ds in shifts:
            if not ds.get("assigned"):
                ds["unfilled_reason"] = "No eligible rotating employee available."

    return {
        "assignments": dated_shifts,
        "effective_pool": effective_pool,
        "rotating_counts": dict(actual_rotating)
    }
