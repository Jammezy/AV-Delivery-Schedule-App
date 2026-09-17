# ============================================================
# models.py - persistent storage
#
# Defaults to SQLite on local disk. Set DATABASE_URL to point at a
# hosted Postgres instead (see README) and nothing else changes -
# that's what lets the app run on a free host with an ephemeral
# filesystem.
# ============================================================

import os
import json
import datetime

from peewee import (
    SqliteDatabase, Model, CharField, BooleanField, IntegerField,
    TextField, DateTimeField,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL:
    from playhouse.db_url import connect as _connect
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    db = _connect(DATABASE_URL)
else:
    db = SqliteDatabase(
        os.environ.get("DB_PATH", "schedule.db"),
        pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000},
    )


class BaseModel(Model):
    class Meta:
        database = db


class Employee(BaseModel):
    name = CharField(unique=True)
    is_lead = BooleanField(default=False)
    min_hours = IntegerField(default=0)
    max_hours = IntegerField(default=40)


class Availability(BaseModel):
    employee_name = CharField(unique=True)
    data_json = TextField()  # {"Mon_07": 1, "Mon_08": 2, ...}
    submitted_at = DateTimeField(default=lambda: datetime.datetime.now(datetime.timezone.utc))

    def get_data(self):
        """Normalize to 0/1/2. Rows written by the old boolean version
        of the app read back as 1, so nothing needs migrating."""
        try:
            raw = json.loads(self.data_json)
        except (TypeError, ValueError):
            return {}
        out = {}
        for key, value in (raw or {}).items():
            out[key] = normalize_level(value)
        return {k: v for k, v in out.items() if v}


class Config(BaseModel):
    data_json = TextField()


def normalize_level(value):
    if value is True:
        return 1
    if value is False or value is None:
        return 0
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    if value >= 2:
        return 2
    return 1 if value == 1 else 0


DEFAULT_CONFIG = {
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "hourStart": 7,
    "hourEnd": 21,          # 21 is the 9-10PM block, so this closes at 10PM
    "reqStaffOpen": 4,
    "reqStaffLate": 2,
    "lateHourStart": 19,
    "minShiftLength": 3,
    "maxShiftLength": 6,
    "maxMorningShifts": 2,
    "maxEveningShifts": 1,
    "maxMorningPlusEvening": 2,
    "fridayCloseHour": 19,
    "dayCloseHours": {},     # e.g. {"Fri": 19} - overrides fridayCloseHour
    "requireLeadDuringOpen": True,
    "blockClopening": True,
    "slotNames": ["DLA", "A4", "A1", "A2"],
    "leadSlotName": "DLA",
    "allowSelfRegister": True,
    # solver
    "solverTimeLimit": 30,
    "solverWorkers": 8,
    # objective weights - see solver._add_fairness
    "wFairness": 100,        # lift whoever got the worst deal
    "wPreference": 2,        # total preferred hours granted
    "wSpread": 20,           # share opening/closing duty around
    "burdenWeight": 3,       # how much an unwanted hour counts against you
}

INT_FIELDS = [
    "hourStart", "hourEnd", "reqStaffOpen", "reqStaffLate", "lateHourStart",
    "minShiftLength", "maxShiftLength", "maxMorningShifts", "maxEveningShifts",
    "maxMorningPlusEvening", "fridayCloseHour", "solverTimeLimit",
    "solverWorkers", "wFairness", "wPreference", "wSpread", "burdenWeight",
]
BOOL_FIELDS = ["requireLeadDuringOpen", "blockClopening", "allowSelfRegister"]


def validate_config(cfg):
    """Catches the settings combinations that used to produce a silent
    empty model or a KeyError deep in slot assignment."""
    errors = []
    if cfg["hourEnd"] < cfg["hourStart"]:
        errors.append("Last operating hour must be at or after the opening hour.")
    if not 0 <= cfg["hourStart"] <= 23 or not 0 <= cfg["hourEnd"] <= 23:
        errors.append("Operating hours must be between 0 and 23.")
    if cfg["minShiftLength"] > cfg["maxShiftLength"]:
        errors.append("Minimum shift length can't exceed maximum shift length.")
    if cfg["minShiftLength"] < 1:
        errors.append("Minimum shift length must be at least 1 hour.")
    if cfg["maxShiftLength"] > (cfg["hourEnd"] - cfg["hourStart"] + 1):
        errors.append("Maximum shift length is longer than the operating day.")
    if not cfg["hourStart"] <= cfg["lateHourStart"] <= cfg["hourEnd"] + 1:
        errors.append("Late-staffing start hour falls outside your operating hours.")
    if cfg["reqStaffOpen"] < 1 or cfg["reqStaffLate"] < 0:
        errors.append("Staffing requirements can't be negative.")
    if not cfg.get("days"):
        errors.append("At least one day must be scheduled.")
    if cfg.get("leadSlotName") and cfg["leadSlotName"] not in (cfg.get("slotNames") or []):
        errors.append("The lead slot name must be one of your slot names.")
    if cfg["solverTimeLimit"] < 1:
        errors.append("Solver time limit must be at least 1 second.")
    return errors


def coerce_config(cfg):
    out = dict(cfg)
    for key in INT_FIELDS:
        if key in out and out[key] not in (None, ""):
            try:
                out[key] = int(out[key])
            except (TypeError, ValueError):
                out[key] = DEFAULT_CONFIG[key]
    for key in BOOL_FIELDS:
        if key in out:
            out[key] = bool(out[key])
    if "slotNames" in out:
        out["slotNames"] = [str(s).strip() for s in (out["slotNames"] or []) if str(s).strip()]
    if "days" in out:
        out["days"] = [str(d).strip() for d in (out["days"] or []) if str(d).strip()]
    return out


def init_db():
    db.connect(reuse_if_open=True)
    db.create_tables([Employee, Availability, Config])
    if Config.select().count() == 0:
        Config.create(id=1, data_json=json.dumps(DEFAULT_CONFIG))
    if not db.is_closed():
        db.close()


def get_config():
    row = Config.get_or_none(Config.id == 1)
    cfg = dict(DEFAULT_CONFIG)
    if row:
        try:
            cfg.update(json.loads(row.data_json))
        except (TypeError, ValueError):
            pass
    return cfg


def save_config(new_cfg):
    """Returns (config, errors). Nothing is written when errors is non-empty."""
    merged = get_config()
    merged.update(coerce_config(new_cfg or {}))
    errors = validate_config(merged)
    if errors:
        return get_config(), errors
    row = Config.get_or_none(Config.id == 1)
    if row is None:
        Config.create(id=1, data_json=json.dumps(merged))
    else:
        row.data_json = json.dumps(merged)
        row.save()
    return merged, []
