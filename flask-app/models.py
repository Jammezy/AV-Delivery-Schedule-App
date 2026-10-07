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
from contextlib import contextmanager

from peewee import (
    SqliteDatabase, Model, CharField, BooleanField, IntegerField,
    TextField, DateTimeField, ForeignKeyField, PostgresqlDatabase,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL:
    from playhouse.db_url import connect as _connect
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    db = _connect(DATABASE_URL, connect_timeout=5)
else:
    db = SqliteDatabase(
        os.environ.get("DATABASE_PATH", os.environ.get("DB_PATH", "schedule.db")),
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


class Folder(BaseModel):
    name = CharField()
    archived = BooleanField(default=False)
    created_at = DateTimeField(default=datetime.datetime.utcnow)


class FolderEmployee(BaseModel):
    folder = ForeignKeyField(Folder, on_delete="CASCADE")
    employee = ForeignKeyField(Employee, on_delete="RESTRICT")
    is_lead = BooleanField(default=False)
    min_hours = IntegerField(default=0)
    max_hours = IntegerField(default=40)
    active = BooleanField(default=True)

    class Meta:
        indexes = ((('folder', 'employee'), True),)


class FolderConfig(BaseModel):
    folder = ForeignKeyField(Folder, unique=True, on_delete="CASCADE")
    data_json = TextField()


class SchemaMigration(BaseModel):
    name = CharField(unique=True)


def folder_members(folder_id):
    return (FolderEmployee.select(FolderEmployee, Employee).join(Employee)
            .where((FolderEmployee.folder == folder_id) & FolderEmployee.active)
            .order_by(Employee.name))


def enroll_employee(employee, folder_id):
    """Explicit enrollment; legacy attributes are only used for initial import."""
    member, _ = FolderEmployee.get_or_create(folder=folder_id, employee=employee,
        defaults=dict(is_lead=employee.is_lead, min_hours=employee.min_hours,
                      max_hours=employee.max_hours))
    return member


class SubmissionState(BaseModel):
    active_folder = ForeignKeyField(Folder, null=True)
    revision = IntegerField(default=0)


class FolderAvailability(BaseModel):
    employee = ForeignKeyField(Employee, on_delete="RESTRICT")
    folder = ForeignKeyField(Folder, on_delete="RESTRICT")
    data_json = TextField(default="{}")
    comment = TextField(default="")
    allow_extra_openings = BooleanField(default=False)
    allow_extra_closings = BooleanField(default=False)
    consent_context = TextField(null=True)
    submitted_at = DateTimeField(default=datetime.datetime.utcnow)
    save_version = IntegerField(default=0)

    class Meta:
        indexes = ((('employee', 'folder'), True),)

    def get_data(self):
        return {k: normalize_level(v) for k, v in json.loads(self.data_json).items()
                if normalize_level(v)}

    def save(self, *args, **kwargs):
        # A newly accepted/imported sheet establishes explicit roster membership.
        # Existing inactive memberships remain inactive when history is edited.
        if self.get_id() is None:
            enroll_employee(self.employee, self.folder_id)
        return super().save(*args, **kwargs)


class SavedSchedule(BaseModel):
    folder = ForeignKeyField(Folder, on_delete="RESTRICT")
    created_at = DateTimeField(default=datetime.datetime.utcnow)
    snapshot_json = TextField()


class GenerationJob(BaseModel):
    # Immutable inputs and fenced leases survive web-service restarts.
    id = CharField(primary_key=True)
    folder = ForeignKeyField(Folder, on_delete="CASCADE")
    fingerprint = CharField(index=True)
    status = CharField(default="queued", index=True)
    payload_json = TextField()
    result_json = TextField(null=True)
    checkpoint_json = TextField(null=True)
    error = TextField(null=True)
    error_status = IntegerField(null=True)
    owner = CharField(null=True)
    lease_until = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.datetime.utcnow)
    started_at = DateTimeField(null=True)
    finished_at = DateTimeField(null=True)
    attempts = IntegerField(default=0)


class SavedWeekendSchedule(BaseModel):
    folder = ForeignKeyField(Folder, on_delete="RESTRICT")
    created_at = DateTimeField(default=datetime.datetime.utcnow)
    snapshot_json = TextField()


class AdminSession(BaseModel):
    token_hash = CharField(unique=True)
    expires_at = DateTimeField()


class CollectionSettings(BaseModel):
    folder = ForeignKeyField(Folder, unique=True, on_delete="CASCADE")
    response_cap = IntegerField(default=100)
    received = IntegerField(default=0)


class CollectionCode(BaseModel):
    folder = ForeignKeyField(Folder, on_delete="CASCADE")
    verifier = CharField(unique=True)
    encrypted_code = TextField()
    label = CharField(default="")
    received = IntegerField(default=0)
    response_limit = IntegerField(default=30)
    revoked = BooleanField(default=False)
    deleted = BooleanField(default=False)
    created_at = DateTimeField(default=datetime.datetime.utcnow)
    expires_at = DateTimeField(null=True)


class IntakeSubmission(BaseModel):
    folder = ForeignKeyField(Folder, on_delete="CASCADE")
    code = ForeignKeyField(CollectionCode, on_delete="RESTRICT")
    name = CharField()
    data_json = TextField()
    comment = TextField(default="")
    allow_extra_openings = BooleanField(default=False)
    allow_extra_closings = BooleanField(default=False)
    consent_context = TextField(null=True)
    submitted_at = DateTimeField(default=datetime.datetime.utcnow)
    status = CharField(default="Pending")
    employee = ForeignKeyField(Employee, null=True, on_delete="SET NULL")
    review_version = IntegerField(default=0)
    accepted_fingerprint = CharField(null=True)
    parent_id = IntegerField(null=True)
    request_key = CharField(unique=True)
    payload_hash = CharField()


class EditGrant(BaseModel):
    submission = ForeignKeyField(IntakeSubmission, on_delete="CASCADE")
    # Create the composite index after legacy tables gain these columns.
    employee = ForeignKeyField(Employee, null=True, on_delete="SET NULL", index=False)
    folder = ForeignKeyField(Folder, null=True, on_delete="CASCADE", index=False)
    token_hash = CharField(unique=True)
    expires_at = DateTimeField()
    revoked = BooleanField(default=False)
    redeemed = BooleanField(default=False)


class SubmissionSession(BaseModel):
    submitted_request_key = CharField(null=True)
    code = ForeignKeyField(CollectionCode, on_delete="CASCADE")
    token_hash = CharField(unique=True)
    state_revision = IntegerField()
    expires_at = DateTimeField()
    edit_grant = ForeignKeyField(EditGrant, null=True, on_delete="CASCADE")


class RateBucket(BaseModel):
    # Fixed slot space, no attacker-selected identifiers or unbounded rows.
    slot = IntegerField(primary_key=True)
    window = IntegerField()
    count = IntegerField(default=0)


@contextmanager
def write_transaction():
    # Serialize migration and folder/submission changes across server workers.
    # PostgreSQL requires its own lock, not SQLite's BEGIN IMMEDIATE syntax.
    with db.atomic("IMMEDIATE") if isinstance(db, SqliteDatabase) else db.atomic():
        if isinstance(db, PostgresqlDatabase):
            db.execute_sql("SELECT pg_advisory_xact_lock(9032401)")
        yield


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
    "allowPreferredBoundaryExtras": False,
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
    "requireLeadDuringLate": False,
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
BOOL_FIELDS = ["requireLeadDuringOpen", "requireLeadDuringLate", "blockClopening", "allowSelfRegister"]


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
    if not 1 <= cfg["solverTimeLimit"] <= 1800:
        errors.append("Solver time limit must be between 1 and 1800 seconds (30 minutes).")
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
    with write_transaction():
        db.create_tables([Employee, Availability, Config, Folder, FolderEmployee, FolderConfig, SchemaMigration, SubmissionState,
                          FolderAvailability, SavedSchedule, GenerationJob, SavedWeekendSchedule, AdminSession, CollectionSettings,
                          CollectionCode, IntakeSubmission, EditGrant, SubmissionSession, RateBucket])
        # Additive, serialized, transactional migration; preserve every existing row.
        from playhouse.migrate import SqliteMigrator, PostgresqlMigrator, migrate
        migrator = PostgresqlMigrator(db) if isinstance(db, PostgresqlDatabase) else SqliteMigrator(db)
        if 'save_version' not in {c.name for c in db.get_columns('folderavailability')}:
            db.execute_sql('ALTER TABLE folderavailability ADD COLUMN save_version INTEGER NOT NULL DEFAULT 0')
        grant_columns = {c.name for c in db.get_columns('editgrant')}
        for column in ('employee_id', 'folder_id'):
            if column not in grant_columns:
                target, action = ('employee', 'SET NULL') if column == 'employee_id' else ('folder', 'CASCADE')
                db.execute_sql(f'ALTER TABLE editgrant ADD COLUMN {column} INTEGER NULL REFERENCES {target}(id) ON DELETE {action}')
        if 'employee_id' not in grant_columns:
            # Backfill once from saved IDs, never infer identity from a name.
            for grant in EditGrant.select():
                grant.employee = grant.submission.employee_id
                grant.folder = grant.submission.folder_id
                grant.save(only=[EditGrant.employee, EditGrant.folder])
        db.execute_sql('CREATE INDEX IF NOT EXISTS editgrant_employee_folder ON editgrant (employee_id, folder_id)')
        if 'response_limit' not in {c.name for c in db.get_columns('collectioncode')}:
            # A constant SQL default backfills existing rows without rebuilding the
            # referenced code table (SQLite table rebuilds would break its children).
            db.execute_sql('ALTER TABLE collectioncode ADD COLUMN response_limit INTEGER NOT NULL DEFAULT 30')
        if 'submitted_request_key' not in {c.name for c in db.get_columns('submissionsession')}:
            db.execute_sql('ALTER TABLE submissionsession ADD COLUMN submitted_request_key VARCHAR(255) NULL')
        columns = {c.name for c in db.get_columns('folderavailability')}
        for name, field in [('allow_extra_openings', BooleanField(default=False)),
                            ('allow_extra_closings', BooleanField(default=False)),
                            ('consent_context', TextField(null=True))]:
            if name not in columns:
                migrate(migrator.add_column('folderavailability', name, field))
        Config.get_or_create(id=1, defaults={"data_json": json.dumps(DEFAULT_CONFIG)})
        if not SubmissionState.get_or_none(SubmissionState.id == 1):
            imported = Folder.create(name="Imported availability")
            for row in Availability.select():
                employee, _ = Employee.get_or_create(name=row.employee_name)
                FolderAvailability.create(employee=employee, folder=imported,
                    data_json=row.data_json, submitted_at=row.submitted_at)
            SubmissionState.create(id=1, active_folder=imported, revision=1)
        if not SchemaMigration.get_or_none(SchemaMigration.name == 'folder-isolation-v1'):
            legacy_config = get_config()
            for folder in Folder.select():
                FolderConfig.get_or_create(folder=folder, defaults={'data_json': json.dumps(legacy_config)})
            associations = {(r.folder_id, r.employee_id) for r in FolderAvailability.select()}
            associations.update((r.folder_id, r.employee_id) for r in IntakeSubmission.select().where(
                (IntakeSubmission.status == 'Accepted') & IntakeSubmission.employee.is_null(False)))
            for folder_id, employee_id in associations:
                enroll_employee(Employee.get_by_id(employee_id), folder_id)
            SchemaMigration.create(name='folder-isolation-v1')
        # Upgrade only agreements that were valid immediately before this release.
        # Stale/null records retain their choices and their reconfirmation state.
        from boundary import boundary_context, legacy_consent_token
        for folder in Folder.select():
            cfg = get_config(folder.id)
            FolderAvailability.update(consent_context=boundary_context(cfg)['token']).where(
                (FolderAvailability.folder == folder.id) &
                (FolderAvailability.consent_context == legacy_consent_token(cfg))).execute()
    if not db.is_closed():
        db.close()


def get_config(folder_id=None):
    # The legacy singleton is retained for migration only; request handlers must
    # supply an authorized folder. New folders use independent default settings.
    row = (Config.get_or_none(Config.id == 1) if folder_id is None else
           FolderConfig.get_or_none(FolderConfig.folder == folder_id))
    cfg = dict(DEFAULT_CONFIG)
    if row:
        try:
            cfg.update(json.loads(row.data_json))
        except (TypeError, ValueError):
            pass
    cfg["days"] = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    cfg["availabilityDays"] = cfg["days"] + ["Sat", "Sun"]
    return cfg


def save_config(new_cfg, folder_id):
    """Returns (config, errors). Nothing is written when errors is non-empty."""
    if 'allowPreferredBoundaryExtras' in (new_cfg or {}) and type(new_cfg['allowPreferredBoundaryExtras']) is not bool:
        return get_config(folder_id), ['Additional preferred boundary shifts must be a boolean.']
    merged = get_config(folder_id)
    merged.update(coerce_config(new_cfg or {}))
    errors = validate_config(merged)
    if errors:
        return get_config(folder_id), errors
    row, _ = FolderConfig.get_or_create(folder=folder_id, defaults={'data_json': json.dumps(merged)})
    row.data_json = json.dumps(merged)
    row.save()
    return merged, []
