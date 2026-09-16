# ============================================================
# app.py - Flask backend
# ============================================================

import os
import re
import json
import secrets
import datetime
from functools import wraps
from collections import defaultdict

from flask import Flask, request, jsonify, send_from_directory

from models import (
    db, init_db, Employee, Availability, get_config, save_config,
    normalize_level,
)
import solver as solver_module

app = Flask(__name__, static_folder="public", static_url_path="")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
if ADMIN_PASSWORD == "admin123":
    print("\n*** Using the default admin password 'admin123'. Set ADMIN_PASSWORD "
          "before anyone else can reach this site. ***\n")

TOKEN_TTL = datetime.timedelta(hours=8)
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW = datetime.timedelta(minutes=15)
KEY_PATTERN = re.compile(r"^[A-Za-z]{2,4}_\d{2}$")

_tokens = {}
_login_attempts = defaultdict(list)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def clean_name(raw):
    return re.sub(r"\s+", " ", (raw or "")).strip()


def resolve_name(raw):
    """Match what was typed against the roster ignoring case, and return the
    roster's spelling. Without this, 'avery', 'Avery' and 'AVERY' each become a
    separate person, and all three land in the solver."""
    name = clean_name(raw)
    if not name:
        return ""
    exact = Employee.get_or_none(Employee.name == name)
    if exact:
        return exact.name
    folded = name.casefold()
    for e in Employee.select():
        if e.name.casefold() == folded:
            return e.name
    return name


# --- database connection per request -------------------------------------
@app.before_request
def _open_db():
    if db.is_closed():
        db.connect(reuse_if_open=True)


@app.teardown_request
def _close_db(_exc):
    if not db.is_closed():
        db.close()


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        expiry = _tokens.get(token)
        if not expiry or expiry < now():
            _tokens.pop(token, None)
            return jsonify({"error": "Your session expired. Log in again."}), 401
        return fn(*args, **kwargs)
    return wrapper


# ---------------- auth ----------------
@app.post("/api/admin/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    cutoff = now() - LOGIN_WINDOW
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    if len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS:
        return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429

    data = request.get_json(silent=True) or {}
    if not secrets.compare_digest(str(data.get("password") or ""), ADMIN_PASSWORD):
        _login_attempts[ip].append(now())
        return jsonify({"error": "Incorrect password"}), 401

    for tok, exp in list(_tokens.items()):
        if exp < now():
            _tokens.pop(tok, None)
    token = secrets.token_hex(24)
    _tokens[token] = now() + TOKEN_TTL
    return jsonify({"token": token})


# ---------------- config ----------------
@app.get("/api/config")
def get_config_route():
    return jsonify(get_config())


@app.put("/api/config")
@require_admin
def put_config():
    updated, errors = save_config(request.get_json(silent=True) or {})
    if errors:
        return jsonify({"errors": errors, "config": updated}), 400
    return jsonify(updated)


# ---------------- employees ----------------
def serialize(e):
    return {"name": e.name, "isLead": e.is_lead,
            "minHours": e.min_hours, "maxHours": e.max_hours}


@app.get("/api/roster")
def public_roster():
    """Names only, so the availability form can offer autocomplete
    instead of trusting free text and creating duplicate people."""
    cfg = get_config()
    return jsonify({
        "names": sorted(e.name for e in Employee.select()),
        "allowSelfRegister": bool(cfg.get("allowSelfRegister", True)),
    })


@app.get("/api/employees")
@require_admin
def list_employees():
    return jsonify([serialize(e) for e in Employee.select().order_by(Employee.name)])


@app.put("/api/employees/<path:name>")
@require_admin
def upsert_employee(name):
    name = resolve_name(name)
    if not name:
        return jsonify({"error": "A name is required."}), 400
    data = request.get_json(silent=True) or {}
    emp, _ = Employee.get_or_create(name=name)
    emp.is_lead = bool(data.get("isLead"))
    emp.min_hours = max(0, int(data.get("minHours") or 0))
    emp.max_hours = max(0, int(data.get("maxHours") or 40))
    if emp.min_hours > emp.max_hours:
        return jsonify({"error": "Minimum hours can't exceed maximum hours."}), 400
    emp.save()
    return jsonify(serialize(emp))


@app.delete("/api/employees/<path:name>")
@require_admin
def delete_employee(name):
    name = resolve_name(name)
    with db.atomic():
        Employee.delete().where(Employee.name == name).execute()
        Availability.delete().where(Availability.employee_name == name).execute()
    return jsonify({"ok": True})


# ---------------- availability ----------------
@app.post("/api/availability")
def submit_availability():
    data = request.get_json(silent=True) or {}
    name = resolve_name(data.get("name"))
    if not name:
        return jsonify({"error": "Enter your name before submitting."}), 400

    cfg = get_config()
    existing = Employee.get_or_none(Employee.name == name)
    if existing is None:
        if not cfg.get("allowSelfRegister", True):
            return jsonify({
                "error": "That name isn't on the roster. Pick your name from the list, "
                         "or ask your supervisor to add you."
            }), 400
        Employee.create(name=name, is_lead=False, min_hours=0, max_hours=40)

    raw = data.get("availability") or {}
    if not isinstance(raw, dict) or len(raw) > 2000:
        return jsonify({"error": "That submission didn't look right. Reload and try again."}), 400

    cleaned = {}
    valid_days = set(cfg["days"])
    for key, value in raw.items():
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            continue
        day, _, hour = key.partition("_")
        if day not in valid_days:
            continue
        if not cfg["hourStart"] <= int(hour) <= cfg["hourEnd"]:
            continue
        lvl = normalize_level(value)
        if lvl:
            cleaned[key] = lvl

    if not cleaned:
        return jsonify({"error": "You haven't marked any hours yet."}), 400

    row, _ = Availability.get_or_create(employee_name=name, defaults={"data_json": "{}"})
    row.data_json = json.dumps(cleaned)
    row.submitted_at = now()
    row.save()

    emp = Employee.get(Employee.name == name)
    available = len(cleaned)
    preferred = sum(1 for v in cleaned.values() if v == 2)
    return jsonify({
        "ok": True, "name": name,
        "availableHours": available, "preferredHours": preferred,
        "minHours": emp.min_hours, "maxHours": emp.max_hours,
        "shortOfMinimum": max(0, emp.min_hours - available),
    })


@app.get("/api/availability/<path:name>")
def get_one_availability(name):
    name = resolve_name(name)
    row = Availability.get_or_none(Availability.employee_name == name)
    emp = Employee.get_or_none(Employee.name == name)
    if not row:
        return jsonify({"found": False,
                        "employee": serialize(emp) if emp else None})
    return jsonify({
        "found": True,
        "availability": row.get_data(),
        "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
        "employee": serialize(emp) if emp else None,
    })


@app.get("/api/availability")
@require_admin
def get_all_availability():
    employees = [serialize(e) for e in Employee.select().order_by(Employee.name)]
    rows = {r.employee_name: r for r in Availability.select()}
    availability = {}
    submitted_at = {}
    for nm, row in rows.items():
        availability[nm] = row.get_data()
        submitted_at[nm] = row.submitted_at.isoformat() if row.submitted_at else None
    return jsonify({
        "employees": employees,
        "availability": availability,
        "submittedAt": submitted_at,
        "missing": [e["name"] for e in employees if e["name"] not in availability],
    })


# ---------------- diagnostics ----------------
def _load_inputs():
    cfg = get_config()
    employees = [serialize(e) for e in Employee.select().order_by(Employee.name)]
    availability = {r.employee_name: r.get_data() for r in Availability.select()}
    return cfg, employees, availability


@app.get("/api/diagnostics")
@require_admin
def diagnostics():
    """Everything blocking or squeezing this week, without running the solver."""
    cfg, employees, availability = _load_inputs()
    return jsonify({
        "config": cfg,
        "diagnostics": solver_module.analyze(employees, availability, cfg),
        "burdenMap": {
            d: {h: w for (dd, h), w in
                solver_module.burden_weights(employees, availability, cfg).items()
                if dd == d}
            for d in cfg["days"]
        },
    })


# ---------------- schedule generation ----------------
@app.post("/api/generate")
@require_admin
def generate():
    cfg, employees, availability = _load_inputs()
    if not employees:
        return jsonify({"error": "Nobody has submitted availability yet."}), 400

    body = request.get_json(silent=True) or {}
    result = solver_module.generate_schedule(
        employees, availability, cfg, seed=body.get("seed")
    )
    result["employees"] = employees
    result["config"] = cfg
    return jsonify(result)


# ---------------- static frontend ----------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# Runs on import so gunicorn / PythonAnywhere / any WSGI server gets a
# working database. The old version only did this under __main__, which
# meant the first request on a real deployment died with "no such table".
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
