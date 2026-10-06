# Development guide

## Branch workflow

`master` is the long-lived production branch and the branch Render deploys.
Create a short-lived branch from current `master` for each feature or fix, open
a pull request, run the regression checks, and merge after review. Delete the
feature branch once merged. Archived historical tags are references, not places
to start new work.

## Local setup and tests

Follow the [application guide](flask-app/README.md#local-setup) for Python 3.12,
environment variables, and local SQLite setup. Node 22 is used for frontend and
browser tests. Exact test commands and dependencies are in
[Tests](flask-app/README.md#tests) and the
[regression workflow](.github/workflows/regression-tests.yml).

Run tests with synthetic data. The PostgreSQL harness creates and drops temporary
databases on a dedicated local server; do not point it at production Neon.
Avoid importing the application with a production database URL during local
verification, because startup initializes storage and normal API requests can
run retention cleanup.

## Making changes

- Keep database migrations additive and preserve existing records and snapshots.
- Preserve folder scoping, code/edit-link destinations, and committed-save behavior.
- Check weekday and weekend workflows separately; their constraints differ.
- Document changes in the application guide; keep this project's README concise.
- Keep secrets, real employee records, local databases, and logs out of commits.
- Back up and verify recovery separately before production schema changes.

Use an isolated environment for demonstrations and integration checks.
Production credentials stay in the hosting environment.
