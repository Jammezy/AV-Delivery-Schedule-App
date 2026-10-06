# Folder rosters and scheduling settings

The administrator's selected folder scopes Employees, staffing totals, Settings,
Week check, weekday and weekend generation, Submissions, Codes, and saved schedules.
The folder selector changes the viewing/editing context; it does not redirect
employee collection codes. A collection code or edit grant establishes its own folder.

## Storage and migration

`FolderEmployee` records explicit employee membership, lead status, minimum and
maximum hours, and whether the employee is currently included. `FolderConfig`
stores an independent settings document for each folder. Global `Employee` IDs
remain stable identity references for response history; their legacy scheduling
attributes are only initial import values. The legacy `Config` singleton is retained
as migration source, not live administrator configuration.

Startup runs an additive, transactionally locked, one-time migration identified by
`SchemaMigration.name = folder-isolation-v1`. It copies existing global settings into
each existing folder and establishes memberships from folder availability and
accepted submission relationships. It preserves saved snapshots, original IDs,
legacy tables, and employees with no known folder. It never assigns such employees
to all folders. Administrators can explicitly import an unassigned employee from
the expandable import control in Employees. Importing a record copies its legacy
attributes into that folder's membership.

Membership supports employees who have not submitted availability. An accepted
submission also establishes membership. Matching names do not automatically identify
or link people across folders. New folders start with default settings and an empty
roster. Removing a member excludes their current availability from generation while
preserving historical submissions, saved schedules, employee identity, and other
folder memberships. Edit grants for the removed membership are revoked.

Both SQLite and PostgreSQL use the existing serialized `write_transaction()`.
No reset, production database access, or destructive reinitialization is needed.
Back up the deployed database using the established backup procedure before deployment.
To roll back, restore a validated pre-deployment backup: running an older release
would resume global roster/config behavior and cannot interpret scoped edits.

## API

Administrator endpoints require bearer authentication and a valid folder:

- `GET/PUT /api/folders/<folder_id>/config`
- `GET/POST /api/folders/<folder_id>/employees`
- `PUT/DELETE /api/folders/<folder_id>/employees/<employee_id>`
- `GET /api/folders/<folder_id>/staffing-plan`

Employee POST accepts either a new `name` or an explicit unassigned `employeeId`.
`GET /api/employees/unassigned` lists only identities with no folder memberships.
Existing `/api/config`, `/api/employees`, `/api/staffing-plan`, `/api/roster`, and
name-based employee routes require `?folderId=<id>`; missing scope is rejected.
The browser uses folder routes and stable IDs. Public employee forms obtain only
the public fields of their code-authorized folder through the collection context.

Folder deletion includes its memberships and configuration in the confirmation
fingerprint and removes them without changing other folders or saved identities.
Historical exports continue to use original snapshots.

## Validation

Run `python -m unittest test_app test_boundary test_collection_codes test_folder_isolation`
and `node --test test_frontend.cjs` with the existing test dependencies. The new tests
cover independent settings and shared-identity attributes, manually added members,
missing/cross-folder scope, code-authorized context, migration/restart preservation,
folder deletion, and late folder-load/save responses. PostgreSQL integration uses
`test_postgres.py` against a disposable local server only.
