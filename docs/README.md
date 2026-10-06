# Documentation

Start with the [project overview](../README.md). The
[application guide](../flask-app/README.md) is the reference for current setup,
configuration, maintenance, tests, and user behavior.

- [Architecture](ARCHITECTURE.md): browser/API/database flow and module responsibilities.
- [Workflows and scheduling behavior](WORKFLOWS.md): the detailed project overview,
  including fairness scoring and persistence.
- [Development guide](../CONTRIBUTING.md): safe local work and branch management.

## Design notes

These documents preserve implementation decisions and original rollout/review notes.
Some describe earlier stages of a feature; use the application guide and current
code when a later behavior differs.

- [Collection codes and edit links](design/COLLECTION_CODES.md)
- [Folder isolation](design/FOLDER_ISOLATION.md)
- [Preferred opening and closing consent](design/PREFERRED_BOUNDARY_CONSENT.md)

## Repository history

`master` is the production source branch. Completed feature branches are removed
after merging. Historical alternatives are retained as `archive/...` tags; tags
preserve the code without appearing as active development branches. See
[the cleanup record](REPOSITORY_CLEANUP.md) for the October 2026 archive map.
