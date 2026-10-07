# AV Delivery Schedule App

Collect employee availability and turn it into delivery schedules that balance
staffing requirements, preferred hours, and opening and closing duties.

[Open the website](https://av-delivery-schedule-app.onrender.com/) ·
[Application guide](flask-app/README.md) ·
[Architecture](docs/ARCHITECTURE.md) ·
[Development](CONTRIBUTING.md)

## What it does

- **Collect availability:** employees enter a shared collection code and mark
  unavailable, available, and preferred hours without creating an account.
- **Keep scheduling periods separate:** each folder owns its roster, availability,
  collection codes, settings, and saved schedules.
- **Give supervisors control:** manage response allowances, edit availability,
  inspect response history, and issue links for employee corrections.
- **Generate weekday schedules:** enforce availability, staffing, shift lengths,
  weekly hours, lead coverage, and opening/closing rules using constraint solving.
- **Balance preferences and workload:** maximize the lowest employee deal score,
  then preferred hours, then minimize permitted cap overruns and spread boundary duties.
- **Plan weekends:** use a separate fixed-shift rotation generator with date ranges,
  exclusions, and recurring assignments.
- **Save and export:** reopen database-backed schedule snapshots and export
  weekday schedules to Excel with fairness results, and formatted weekend
  schedules with excluded-date signup sheets.

## How it is built

| Layer | Technology / purpose |
| --- | --- |
| Browser | HTML, CSS, and JavaScript; employee and supervisor interfaces |
| API | Python and Flask; collection, administration, scheduling, and persistence |
| Scheduling | Google OR-Tools CP-SAT for weekdays; separate weekend rotation logic |
| Database | Peewee ORM; PostgreSQL on Neon in production, SQLite for local development |
| Hosting | Render web service, deployed from `master` |
| Verification | Python, frontend, browser, and disposable PostgreSQL regression checks |

The weekday solver uses sequential objectives with one shared time budget. Later
stages preserve earlier optima, and a valid schedule can still be returned when
the time limit prevents proving every stage optimal. Weekend rotation uses its
own rules. See [workflows and scheduling behavior](docs/WORKFLOWS.md) for details.

## Explore the repository

```text
flask-app/                 Application, browser assets, maintenance tools, and tests
  app.py                  Flask API and supervisor workflows
  models.py               Database models and additive initialization/migrations
  solver.py               Weekday scheduling constraints and fairness objectives
  weekend_generator.py    Weekend assignments and rotation
  collection_codes.py     Collection codes, edit links, sessions, and response history
  public/                 Employee and supervisor pages, scripts, and styles
docs/                     Architecture, workflow details, and design notes
.github/workflows/        Automated regression checks
```

| Read this | For |
| --- | --- |
| [Application guide](flask-app/README.md) | Local setup, configuration, deployment, backups, retention, and test commands |
| [Architecture](docs/ARCHITECTURE.md) | Request flow, scheduling boundaries, and persistence |
| [Workflows](docs/WORKFLOWS.md) | Collection, weekday fairness, weekends, and saved records |
| [Design notes](docs/README.md) | Collection-code, folder-isolation, and preferred-boundary decisions |
| [Development guide](CONTRIBUTING.md) | Branch workflow and safe local verification |

## Run locally

Use Python 3.12, create a virtual environment in `flask-app`, and install
`requirements.txt`. Set a unique `ADMIN_PASSWORD` and stable collection keys,
then run `python app.py`. The [setup guide](flask-app/README.md#local-setup)
includes commands for Windows, macOS, and Linux.

For a local demonstration, keep `DATABASE_URL` unset and use a disposable SQLite
database. Production uses the existing Neon database. Browser and PostgreSQL tests
must run against disposable test environments.

## Data and operation

Availability, history, folders, settings, sessions, and saved schedules persist in
the database. Successful weekday generation saves a snapshot automatically;
weekend previews are saved explicitly. Normal API use removes eligible scheduling
records strictly older than **18 calendar months** under the existing retention policy.

The hosted service can sleep between visits, so its first load may take longer.
The live supervisor dashboard requires authentication; production employee
submissions require a valid collection code. For a demonstration, use local
synthetic data rather than publishing credentials or real employee responses.

See the [application guide](flask-app/README.md) for operational limitations,
security behavior, and recovery instructions.
