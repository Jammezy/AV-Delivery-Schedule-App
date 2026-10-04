"""One bounded retention pass. No startup migrations, Flask or payload logging."""
import calendar
import datetime as dt
from contextlib import contextmanager

from peewee import PostgresqlDatabase, SqliteDatabase, fn
from models import (db, write_transaction, Folder, FolderAvailability, Availability,
                    SavedSchedule, SavedWeekendSchedule, SubmissionState)

CHILDREN = (("availability", FolderAvailability, FolderAvailability.submitted_at),
            ("weekdaySchedules", SavedSchedule, SavedSchedule.created_at),
            ("weekendSchedules", SavedWeekendSchedule, SavedWeekendSchedule.created_at))
AGED = CHILDREN + (("legacyAvailability", Availability, Availability.submitted_at),
                   ("folders", Folder, Folder.created_at))


def utc_naive(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, dt.datetime):
        raise ValueError("Expected a UTC timestamp")
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def cutoff_for(now):
    now = utc_naive(now)
    month = now.year * 12 + now.month - 1 - 18
    year, zero_month = divmod(month, 12)
    month = zero_month + 1
    return now.replace(year=year, month=month,
                       day=min(now.day, calendar.monthrange(year, month)[1]))


def _sqlite_timestamp(value):
    try:
        return utc_naive(value).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        return None


def validate_schema():
    """Fail closed; the standalone job must never initialize/migrate storage."""
    for _, model, field in AGED:
        columns = {c.name: c for c in db.get_columns(model._meta.table_name)}
        if not {"id", field.column_name}.issubset(columns):
            raise RuntimeError("Required retention schema is missing")
        if isinstance(db, PostgresqlDatabase) and columns[field.column_name].data_type != "timestamp without time zone":
            raise RuntimeError("Retention requires the existing naive UTC timestamp schema")
    columns = {c.name for c in db.get_columns("submissionstate")}
    if not {"id", "active_folder_id", "revision"}.issubset(columns) or not SubmissionState.get_or_none(SubmissionState.id == 1):
        raise RuntimeError("Initialize the app schema before running retention")


def _valid(field):
    return fn.isfinite(field) if isinstance(db, PostgresqlDatabase) else fn.retention_utc(field).is_null(False)


def _expired(field, cutoff):
    value = field if isinstance(db, PostgresqlDatabase) else fn.retention_utc(field)
    boundary = cutoff if isinstance(db, PostgresqlDatabase) else cutoff.isoformat(timespec="microseconds")
    return _valid(field) & (value < boundary)


def _folder_candidates(cutoff, after_cleanup=False):
    query = Folder.select(Folder.id).where(_expired(Folder.created_at, cutoff))
    for _, model, field in CHILDREN:
        children = model.select(model.id).where(model.folder == Folder.id)
        if after_cleanup:
            # Invalid dates are retained and protect their parent too.
            children = children.where(~_expired(field, cutoff) | field.is_null(True))
        query = query.where(~fn.EXISTS(children))
    return query


def _counts(cutoff, after_cleanup=False):
    counts = {key: model.select().where(_expired(field, cutoff)).count()
              for key, model, field in AGED if model is not Folder}
    counts["folders"] = _folder_candidates(cutoff, after_cleanup).count()
    return counts


@contextmanager
def _transaction(apply, timeout_ms, statement_timeout_ms):
    if isinstance(db, PostgresqlDatabase):
        with db.atomic():
            # Transaction-local settings also work with Neon's pooled endpoint.
            if not apply:
                db.execute_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            db.execute_sql("SET LOCAL TIME ZONE 'UTC'")
            db.execute_sql("SET LOCAL lock_timeout = %s", (str(timeout_ms) + "ms",))
            db.execute_sql("SET LOCAL statement_timeout = %s", (str(statement_timeout_ms) + "ms",))
            if apply:
                db.execute_sql("SELECT pg_advisory_xact_lock(9032401)")
            yield
    else:
        previous = db.execute_sql("PRAGMA busy_timeout").fetchone()[0]
        db.execute_sql("PRAGMA busy_timeout = %d" % timeout_ms)
        try:
            with write_transaction() if apply else db.atomic():
                yield
        finally:
            db.execute_sql("PRAGMA busy_timeout = %d" % previous)


def run_retention(*, now=None, apply=False, max_rows=1000, timeout_ms=5000,
                  statement_timeout_ms=60000):
    """One atomic pass, at most max_rows committed removals, all-or-nothing.

    Call with a thread-owned connection; callers close it in finally.
    Candidate selection is always inside the shared write lock when applying.
    """
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        raise ValueError("max_rows must be between 1 and 10000")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000:
        raise ValueError("timeout_ms must be between 1 and 60000")
    if type(statement_timeout_ms) is not int or not 1 <= statement_timeout_ms <= 60000:
        raise ValueError("statement_timeout_ms must be between 1 and 60000")
    now = utc_naive(now if now is not None else dt.datetime.now(dt.timezone.utc))
    cutoff = cutoff_for(now)
    if isinstance(db, SqliteDatabase):
        db.register_function(_sqlite_timestamp, "retention_utc", 1)
    db.connect(reuse_if_open=True)
    if not isinstance(db, (PostgresqlDatabase, SqliteDatabase)):
        raise RuntimeError("Unsupported database")
    with _transaction(apply, timeout_ms, statement_timeout_ms):
        validate_schema()
        candidates = _counts(cutoff, after_cleanup=True)
        invalid = {key: model.select().where(~_valid(field) | field.is_null(True)).count()
                   for key, model, field in AGED}
        deleted = dict.fromkeys(candidates, 0)
        budget = max_rows
        if apply:
            for key, model, field in AGED:
                if model is Folder:
                    continue
                ids = [r.id for r in model.select(model.id).where(_expired(field, cutoff))
                       .order_by(field, model.id).limit(budget)] if budget else []
                if ids:
                    deleted[key] = model.delete().where(model.id.in_(ids) & _expired(field, cutoff)).execute()
                    budget -= deleted[key]
            ids = [r.id for r in _folder_candidates(cutoff).order_by(Folder.created_at, Folder.id).limit(budget)] if budget else []
            for folder_id in ids:
                SubmissionState.update(active_folder=None, revision=SubmissionState.revision + 1).where(
                    (SubmissionState.id == 1) & (SubmissionState.active_folder == folder_id)).execute()
                deleted["folders"] += Folder.delete().where(Folder.id == folder_id).execute()
        remaining = _counts(cutoff, after_cleanup=True) if apply else candidates.copy()
    # Return only after COMMIT succeeds. Never report a rolled-back delete as done.
    return {"mode": "apply" if apply else "dry-run", "policy": "latest-submission/record-creation;18-calendar-months;strict-before;UTC",
            "runAt": now.isoformat() + "Z", "cutoff": cutoff.isoformat() + "Z",
            "maxRows": max_rows, "candidates": candidates, "invalidTimestamps": invalid,
            "deleted": deleted, "remaining": remaining, "backlogRemaining": sum(remaining.values()) > 0}
