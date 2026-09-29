# AV Delivery App

The AV Delivery App is a scheduling tool designed to generate fair and valid weekly shift schedules for employees. It provides a web-based frontend where employees can submit their availability and preferences, and an administrative dashboard where supervisors can generate, view, and manage schedules.

Under the hood, the app employs an operations research approach using the OR-Tools CP-SAT solver to navigate complex staffing requirements and employee preferences.

## Application Logic and Flow

1. **Configuration:** The administrator configures the constraints for the week (e.g., store hours, required staff per hour, minimum/maximum shift lengths, and maximum shifts per person).
2. **Employee Submissions:** Employees visit the frontend to "paint" their availability onto a weekly grid. They can mark hours as:
   - **Unavailable** (cannot work)
   - **Available** (can work)
   - **Preferred** (wants to work)
3. **Pre-check and Diagnostics:** Before generating a schedule, the app calculates coverage against the constraints. It detects impossibilities (e.g., "Tuesday 7 AM needs 4 people but only 3 are available") and highlights them to the admin so they can adjust requirements if necessary.
4. **Schedule Generation:** The admin requests a schedule. The backend invokes the solver to search for a valid configuration of shifts.
5. **Review and Publish:** The generated schedule is displayed in the dashboard, complete with fairness scores and diagnostics. It can be saved, exported to Excel, or regenerated with tweaked rules.

## Schedule Generator Logic

The core of the schedule generation is powered by the `Schedule_Maker_4000.py` logic, now integrated into the `solver.py` module using Google's **OR-Tools CP-SAT solver**.

The solver runs in a **two-phase approach**:
1. **Feasibility Phase:** The solver first searches rapidly for *any* valid schedule that strictly obeys all the hard constraints (availability, staffing requirements, shift length limits). This ensures that if a valid schedule exists, the system will find it quickly without getting bogged down by optimization.
2. **Optimization Phase (Fairness):** Once a feasible schedule is found, it is fed back to the solver as a starting point. The solver uses the remaining time budget to maximize the "fairness" objective, swapping shifts to balance the quality of the schedules given to each employee. If the solver runs out of time during this phase, it still returns the best valid schedule it found, rather than failing.

If the solver cannot find a feasible schedule (even after the pre-check passes), it runs an "infeasible explanation" routine. It relaxes the hard constraints (allowing understaffing or missed minimum hours) and reports the smallest set of rules that had to bend, producing a concrete error like *"Tue 7AM–9AM: short 1 person"* instead of a generic failure.

## Fairness Scoring

The objective function of the solver isn't just to maximize the total number of preferred hours granted. A naive maximization would give one person 96% of their preferred hours while another gets only 17%. Instead, the solver maximizes the **lowest deal score** on the roster, effectively raising the floor so everyone gets a fair schedule.

An employee's **deal score** is calculated based on two factors:

1. **Preference Satisfaction (Normalized):**
   Employees are scored against their own realistic ceiling.
   ```
   pref_score = 1000 × (preferred hours received) / (realistic ceiling of preferred hours)
   ```
   The ceiling is capped by their maximum weekly hours and the longest legal week they could work. This normalizes the score: someone who asks for 5 preferred hours and gets 4 is scored better than someone who asks for 40 and gets 20.

2. **Shift Burden (Weighted by Scarcity):**
   Working hours that nobody wants is considered a "burden". Every open hour is weighted by how scarce volunteers are.
   ```
   burden = 10 × max(0, staff_needed - people_who_prefer_it) / staff_needed
   ```
   An hour that 4 people are needed for but 0 people want has a maximum burden weight. An hour that everyone wants has a burden weight of 0.

**Combined Deal Score:**
```
deal_score = pref_score - (burden_weight × total_burden_points_worked)
```
Because the solver maximizes the lowest deal score, if an employee covers an unwanted Thursday 7 AM shift, their deal score drops. To raise the lowest score back up, the solver is forced to compensate that employee by granting them more of their preferred hours elsewhere in the week.

## Changeable Constraints

The schedule generator is highly configurable. Admins can tweak the following rules to adapt to different weeks or staffing levels:

* **Operating Hours:** `hourStart` and `hourEnd` define the open window for the schedule.
* **Staffing Levels:** `reqStaffOpen` (standard hours) and `reqStaffLate` (late hours starting at `lateHourStart`).
* **Shift Lengths:** `minShiftLength` (e.g., minimum 3 hours) and `maxShiftLength` (e.g., maximum 6 hours).
* **Weekly Hours per Employee:** Individual `minHours` and `maxHours` (set per employee in the roster).
* **Shift Caps:**
  * `maxMorningShifts`: Max opening shifts per person per week.
  * `maxEveningShifts`: Max closing shifts per person per week.
  * `maxMorningPlusEvening`: Max combined opening/closing shifts per week.
* **Clopening:** `blockClopening` prevents scheduling an employee for a closing shift followed by an opening shift the next morning.
* **Lead Coverage:** `requireLeadDuringOpen` ensures at least one designated "Lead" employee is scheduled during all standard operating hours.
* **Solver Weights:** The trade-off between raising the fairness floor (`wFairness`), granting raw preferred hours (`wPreference`), and sharing opening/closing duties (`wSpread`) can be adjusted.

## Running the App

For detailed setup instructions for running the Flask backend and the employee frontend, please see the `flask-app/README.md` file.