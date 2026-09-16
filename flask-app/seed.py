"""Populate a demo database so the app can be exercised locally."""
import random, json
from models import init_db, Employee, Availability, db
init_db()
db.connect(reuse_if_open=True)
Employee.delete().execute(); Availability.delete().execute()
rng = random.Random(7)
DAYS = ["Mon","Tue","Wed","Thu","Fri"]
for i in range(20):
    nm = ["Avery","Bree","Caleb","Dina","Eli","Faith","Gus","Hana","Ian","Jae",
          "Kira","Leo","Mila","Noah","Omar","Pia","Quinn","Rosa","Sam","Tess"][i]
    Employee.create(name=nm, is_lead=i < 6, min_hours=rng.choice([0,0,6,9]),
                    max_hours=rng.choice([20,25,30]))
    row = {}
    for d in DAYS:
        s = rng.choice([7,7,7,8,9,11,13]); e = rng.choice([13,15,17,19,21,21,21])
        if e-s < 3: e = min(21, s+5)
        for h in range(s, e+1): row["%s_%02d" % (d,h)] = 1
        if rng.random() < 0.7:
            ps = rng.randint(s, max(s, e-3))
            for h in range(ps, min(ps+rng.randint(3,6), e+1)): row["%s_%02d"%(d,h)] = 2
    Availability.create(employee_name=nm, data_json=json.dumps(row))
db.close()
print("seeded 20 employees")
