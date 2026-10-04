# Shared collection codes

Employees use the existing home-page link, enter a supervisor-provided code, and submit their name and availability. They do not create accounts. Every new code starts with **30 successful responses total**, shared by everyone holding it. Supervisors can add responses to an individual code or create a second code with another 30. This does not verify employee identity: supervisors review responses before they become scheduling inputs.

## Before manually merging / deploying

Production has not been changed by this branch. Render currently automatically deploys `master`; merging this PR will therefore deploy the code.

1. Configure a strong, nonempty `ADMIN_PASSWORD` in Render if not already configured. The application now refuses to start with a missing password or `admin123`.
2. Generate these two separate secrets on a trusted computer using the application's Python dependencies, and enter them as Render environment variables. Do not commit them, send them in chat, or put them in frontend environment variables:

   ```sh
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   python -c 'import secrets; print(secrets.token_hex(32))'
   ```

   Use the first output for `COLLECTION_ENCRYPTION_KEY` and the second for `COLLECTION_VERIFIER_KEY`. Keep both stable across restarts/workers and securely backed up. Changing the encryption key without re-encrypting stored codes prevents their display; changing the verifier key invalidates existing codes/CSRF tokens and resets rate identities. Key rotation requires a planned migration.
3. Keep the existing server-only Neon `DATABASE_URL`. No Neon console change or employee authentication provider is required. No production database credentials or records were accessed for development.
4. Review the PR checks before merging. On startup, the existing locked `init_db()` adds the six feature tables and the `collectioncode.response_limit` column if missing. Existing codes receive a limit of 30; their usage, code secrets and responses are preserved. Later restarts preserve any increased limits. It does not rewrite existing accepted availability, employees, folders or saved schedules. Take a normal database backup before deployment. Confirm code creation/submission/review with synthetic data after deployment. The per-code increase feature needs no new Render secrets or manual Neon changes.

Without valid collection secrets, collection endpoints fail closed with 503; other supervisor features can still load. HTTPS is required in production. Cookies are Secure on Render (or an HTTPS request), HttpOnly, SameSite=Strict, and expire after two hours. Local HTTP development deliberately omits Secure.

## Supervisor workflow

1. Open **Codes** and choose the **Scheduling folder**. This dropdown stays in sync with the main folder selector. Only that folder's codes, total submissions, limit and open/closed status are shown. Newly created codes belong to that folder; switching folders does not move any code or response. Archived folders must be restored before code creation. Creating a code never opens a closed folder. Use **Open folder for submissions** when ready. The existing app accepts submissions into one active folder at a time; viewing another folder does not change the active destination.
2. Optionally type a label such as “Team A” and an expiry date. Click **Create code**, then **Copy invitation**. Send that same invitation to the group through your normal communication channel. Creating/copying does not send messages automatically.
3. Create another code for another group or when more responses are needed. Each starts at 30. To increase just one code, enter the number of **Additional responses** in its row and click **Add responses**. Adding 2 raises 30 to 32; it does not reset responses already received or change any other code. An exhausted code can accept responses again if its collection is open, it has not expired, and the period has capacity. Limits can only increase, up to 10,000 total per code. Revoked/deleted codes cannot be increased. The page and copied invitation use the current limit.
4. **Revoke** immediately stops new submissions, including already unlocked sessions. **Delete** also stops submissions and hides the code by default. Enable **Show deleted codes** to inspect it again. Both keep existing responses and their usage counts.
5. In **Submissions**, review incoming responses. Multiple sheets with the same name are flagged. Choose the roster employee and click **Use this submission**, then confirm. Add missing employees through the existing Employees tab. Acceptance replaces that employee's accepted availability for that folder only; previous response snapshots remain available. Rejection does not refund quota.
6. Employees can submit a new sheet using any active code with capacity. It remains pending until reviewed. If an employee needs to view/correct a specific sheet, use **Create edit link** on that response. The link expires after 24 hours, can be redeemed once into a temporary session, exposes only that sheet and fixes its name. Corrections create another pending sheet and consume one response from the original code. It cannot be used after code revocation/exhaustion; provide a fresh code for a new sheet instead. Creating another edit link revokes prior links for that sheet. **Revoke edit links** also invalidates redeemed edit sessions.

The **Folder submission limit** defaults to 100 total successful submissions for folders without a saved limit. Existing saved limits (including 300) and usage counts are preserved; restarting does not reset them. This is a cumulative limit across all codes for one folder, not a week/month or automatically resetting period. Repeat submissions and corrections count toward both the code allowance and folder limit. **Save folder limit** changes only the selected folder's cap (30–10,000). Creating another code or adding responses to a code does not change it. If the folder is full, raise its separate limit before more submissions can be accepted. There is a maximum of 100 code records per folder, including deleted codes. Counts cannot be refunded by retries, rejection, deletion, or retention cleanup.

Before code entry, the employee page makes no claim about the response allowance. Once validated, the page shows the code's assigned folder and its actual saved response limit from the server. It refreshes this context on form load and before submitting, preserving draft answers and consent choices. Supervisor increases are reflected in an already open form; stale/closed-folder sessions cannot silently submit into another folder. The page introduction now describes availability for both weekdays and weekends.

## Data and security behavior

- Anonymous pages never list the roster or fetch an existing response by name. The legacy roster and name-lookup endpoints now require supervisor authorization. Public configuration exposes only fields needed to paint availability and explain consent.
- All feature administration requires the existing server-validated supervisor bearer session. Temporary employee sessions grant submission access only. Shared codes are encrypted for later supervisor display and independently HMAC-verified; edit/session tokens are stored as SHA-256 hashes. Codes never appear in public URLs. Edit tokens use a URL fragment, which is immediately removed before redemption, and are not transmitted in the HTTP URL/referrer.
- Intake is append-only through public endpoints. Accepted `FolderAvailability` remains the only solver input. Review checks both the response version and the latest accepted-sheet version so concurrent review or supervisor edits cannot be silently overwritten. Existing consent choices and their original context are copied on acceptance; stale consent is not silently upgraded.
- Quotas, limit increases, revocation, period checks, acceptance and counters use the existing database-wide write lock/transaction, including across PostgreSQL workers. Concurrent requests cannot exceed the code's saved limit (30 by default). Concurrent supervisor increases are added together. Invalid forms do not count. A retry with the same request ID and identical normalized data returns the original receipt without adding a response; reusing that ID with changed data is rejected. This guarantee lasts while that receipt is retained and the session is valid.
- Submission writes require an HttpOnly session cookie and an explicit session-bound CSRF header. Revoked/expired grants, closed/archived folders, changed collection revisions, full codes and full collection limits are checked on the server.
- Persistent rate limits cap unlock and submission requests by session/code/address plus a global limit. There are at most 513 fixed rate-bucket records, not one row per attempted IP/name. Temporary sessions are limited to 200 per code; expired sessions are pruned on unlock. Request body size is bounded by the existing Flask limit. API responses are noncacheable; pages suppress referrers, framing and MIME sniffing.
- Forwarded IP headers are not trusted. Render's proxy may cause multiple employees to share the observed address; configured limits accommodate a 35-person burst but may temporarily block legitimate traffic. A future proxy-aware change must verify Render's current proxy chain before using ProxyFix. Rate limits reduce database growth but are not a complete denial-of-service defense: repeated requests still reach the application, and a malicious code holder can spend its available responses. Increasing a code's limit does not increase request rate limits or temporary session limits. Add edge request controls if operational evidence warrants them.
- Soft-deleted codes keep their audit relationship to responses. Existing 18-month retention includes response snapshots and code records; a code is not physically purged while retained intake references it. Deleting a folder explicitly includes the feature counts/version in the confirmation preview, then removes its intake, codes, settings and cascaded grants/sessions. Database expiry/deletion does not make backups disappear immediately.

## Tables and API scope

New tables: `collectionsettings`, `collectioncode`, `intakesubmission`, `editgrant`, `submissionsession`, `ratebucket` (Peewee names). Existing tables remain compatible with older accepted data. Old public submission clients must adopt the code/session flow.

| Access | Endpoints |
| --- | --- |
| Public minimal metadata | `GET /api/config`, `GET /api/submission-context` |
| Code or edit-link redemption | `POST /api/collection/unlock` |
| Temporary session | `GET /api/collection/context` |
| Temporary session + CSRF | `POST /api/availability` |
| Supervisor | `GET/POST /api/admin/codes`, `PATCH/DELETE /api/admin/codes/:id` |
| Supervisor | `POST /api/admin/codes/:id/responses` with integer `additionalResponses` |
| Supervisor | `PUT /api/admin/collections/:folderId`, `GET /api/admin/intake` |
| Supervisor | `POST /api/admin/intake/:id/review`, `POST/DELETE /api/admin/intake/:id/edit-links`, `DELETE /api/admin/edit-links/:id` |

## Validation and rollback

Run `python -m unittest test_app test_boundary test_collection_codes`, `node test_frontend.cjs`, and `node test_collection_browser.cjs`. The browser suites use disposable SQLite databases, random test-only keys and synthetic names. Existing boundary browser suites now explicitly submit through the code gate and supervisor acceptance rather than assuming anonymous name lookup. `legacy_test_client.py` is a test-only adapter; it is not installed as the production Flask client.

Run `test_postgres.py` and `test_collection_postgres.py` with `TEST_POSTGRES_PORT` pointing to a dedicated **local disposable** PostgreSQL server. They create/drop uniquely named test databases and never read the production URL. GitHub Actions runs both plus existing scheduling, deletion, retention and browser regressions.

Rollback should preserve the added tables rather than dropping them. Disable public collection at the edge if reverting to an older application version: older code exposes anonymous name lookup and unprotected submission. Pending intake cannot be interpreted by the old solver. Do not lose keys or remove tables to work around a deployment failure.

## Remaining work from the broader handoff

This PR implements the shared-code feature and directly related access/data protections. It is not a legal compliance certification or a complete security audit. The site still stores names and availability even without employee accounts. The operator's actual identity, jurisdiction, data uses, hosting/analytics providers and retention practices are needed to finalize accurate privacy/terms text. No new legal pages or tracking-cookie banner were fabricated. Any analytics consent controls must match the tracking actually used.

Separately audit dependencies, production secret history, existing supervisor authentication/password recovery, token storage and session revocation, database role privileges/RLS, deployment headers/CSP/HSTS, backups and restoration, logging, accessibility and incident response. Neon RLS is not automatically needed on every table for this server-only architecture; role design and explicit authorization must be assessed before enabling it, since table owners/bypass roles can ignore RLS. No RLS changes were made here. There are no BYU integration or login requirements in this implementation.
