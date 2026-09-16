"""Regression check for the scheduling rules.

Builds random rosters, generates schedules, and asserts that every hard
constraint actually holds in the output: staffing counts, lead coverage,
contiguity, shift lengths, availability, weekly hour bounds, opening and
closing caps, clopening, and that nobody scheduled by the solver goes
missing from the printable slot grid.

Run it after changing anything in solver.py:

    python3 verify.py
"""
import random, time, solver
from models import DEFAULT_CONFIG
cfg = dict(DEFAULT_CONFIG)
DAYS = cfg["days"]; HOURS = list(range(cfg["hourStart"], cfg["hourEnd"]+1))

def make_roster(n, seed=1):
    rng = random.Random(seed)
    emps, avail = [], {}
    for i in range(n):
        nm = "Emp%02d" % i
        emps.append({"name": nm, "isLead": i < 6, "minHours": rng.choice([0,0,6,9]),
                     "maxHours": rng.choice([20,25,30])})
        row = {}
        for d in DAYS:
            start = rng.choice([7,7,7,8,9,11,13]); end = rng.choice([13,15,17,19,21,21,21])
            if end-start < 3: end = min(21, start+5)
            for h in range(start, end+1): row["%s_%02d"%(d,h)] = 1
            if rng.random() < 0.7:
                ps = rng.randint(start, max(start, end-3))
                for h in range(ps, min(ps+rng.randint(3,6), end+1)): row["%s_%02d"%(d,h)] = 2
        avail[nm] = row
    return emps, avail

def verify(res, emps, avail, cfg):
    work = res["work"]; errs=[]
    for day in DAYS:
        for h in HOURS:
            req = solver.required_staff(day,h,cfg); got=len(work[day][h])
            if got!=req: errs.append("staffing %s %d %d!=%d"%(day,h,got,req))
            if req and h < cfg["lateHourStart"]:
                if not [e for e in emps if e["isLead"] and e["name"] in work[day][h]]:
                    errs.append("no lead %s %d"%(day,h))
    for e in emps:
        wk=0
        for day in DAYS:
            hrs=[h for h in HOURS if e["name"] in work[day][h]]; wk+=len(hrs)
            if hrs:
                if len(hrs)!=max(hrs)-min(hrs)+1: errs.append("gap %s %s"%(e["name"],day))
                if not cfg["minShiftLength"]<=len(hrs)<=cfg["maxShiftLength"]:
                    errs.append("len %s %s=%d"%(e["name"],day,len(hrs)))
            for h in hrs:
                if solver.level(avail,e["name"],day,h)==0: errs.append("unavail %s %s %d"%(e["name"],day,h))
        if not e["minHours"]<=wk<=e["maxHours"]: errs.append("weekly %s=%d"%(e["name"],wk))
        m=sum(1 for d in DAYS if e["name"] in work[d][cfg["hourStart"]])
        ev=sum(1 for d in DAYS if e["name"] in work[d][cfg["lateHourStart"]])
        if m>cfg["maxMorningShifts"]: errs.append("morn %s=%d"%(e["name"],m))
        if ev>cfg["maxEveningShifts"]: errs.append("eve %s=%d"%(e["name"],ev))
        if m+ev>cfg["maxMorningPlusEvening"]: errs.append("comb %s=%d"%(e["name"],m+ev))
    if cfg["blockClopening"]:
        for e in emps:
            for k in range(len(DAYS)-1):
                d,nx=DAYS[k],DAYS[k+1]
                lo=max([h for h in HOURS if solver.required_staff(d,h,cfg)>0], default=None)
                if lo is None: continue
                if e["name"] in work[d][lo]:
                    for mh in [h for h in HOURS[:3] if solver.required_staff(nx,h,cfg)>0]:
                        if e["name"] in work[nx][mh]: errs.append("clopen %s %s"%(e["name"],d))
    for day in DAYS:
        for h in HOURS:
            placed={v for v in res["schedule"][day][h].values() if v}
            if placed!=set(work[day][h]): errs.append("slotdrop %s %d"%(day,h))
    return errs

for n in (16, 22, 30):
    emps, avail = make_roster(n, seed=7)
    t=time.time(); res = solver.generate_schedule(emps, avail, cfg, seed=42); el=time.time()-t
    print("n=%2d  status=%-9s optimized=%-5s wall=%.1fs floor=%d" %
          (n, res["status"], res.get("optimized"), el, res.get("fairnessFloor",0)))
    errs = verify(res, emps, avail, cfg)
    print("     violations:", len(errs), errs[:3])
    f=res["fairness"]
    pcts=[r["preferredPct"] for r in f if r["preferredPct"] is not None]
    print("     pref%% min=%s median=%s max=%s | worst deal=%s best=%s" %
          (min(pcts), sorted(pcts)[len(pcts)//2], max(pcts), f[0]["dealScore"], f[-1]["dealScore"]))
    assert not errs
print("OK")
