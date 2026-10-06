# Repository cleanup — October 6, 2026

## Scope

This cleanup improves navigation and removes completed branch names. It preserves
the production branch name (`master`), `flask-app` application path, runtime source,
dependencies, browser assets, all tests, workflow contents, database models, and
deployment commands. It makes no database, credential, or Render service changes.

The detailed root overview is preserved in `docs/WORKFLOWS.md`; design notes are
moved to `docs/design/`. The tracked `flask-app/server.log` was a local Gunicorn
startup log and is removed from the current checkout; its history remains in Git.
Ignore rules now also cover local environment files.

## Branch audit and archive map

The initial repository had 36 branches and no open pull requests. Thirty feature
branch heads were ancestors of `master`. Five other heads had different histories:
two patches were equivalent to changes already in `master`, and three were older
iterations of features subsequently incorporated through other commits. They are
preserved as tags rather than merged again over newer production code.

| Former branch | Archive tag | Commit |
| --- | --- | --- |
| `feature/delete-semester-folder` | `archive/2026-10-06/pr-11` | `251acaf78b58f1fd87d833596281d89d28d7050b` |
| `feature/delete-semester-folder-6266976282720100252` | `archive/2026-10-06/pr-12` | `dcee64e34da95d38457fef09847dbadcf082c2c0` |
| `feature/employee-consent-visibility-10376601151206672166` | `archive/2026-10-06/pr-19` | `88c7b70667c5cf818f8994556a924b6c29bfa0ff` |
| `feature/late-hours-lead-coverage` | `archive/2026-10-06/pr-14` | `e66f542e2f932af7f320520f966c24cb00c4991c` |
| `pr-20-2822065563447316786` | `archive/2026-10-06/pr-21` | `b86cfbac5b187c0b26a6438ef00a4eaa186ecb64` |

`archive/2026-10-06/pre-cleanup-master` preserves the production source before
cleanup at `52eb83a483419a33bc585259bed0ec2dc6496e3f`. A complete Git bundle of
all original branch heads and history was also saved before changes. Neither
archive contains a backup of live database records.

To examine a tag, use `git show <tag>`. To recover one as a local branch:

```sh
git switch -c recovered-pr-11 archive/2026-10-06/pr-11
```

Keep recovered historical branches separate from production. Some older versions
predate current folder isolation and collection protections.
