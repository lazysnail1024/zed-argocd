# Documentation

Installation and release instructions describe published artifacts. Development instructions apply to the checked-out source; a published binary may not include later source changes.

| Topic | Documentation |
| --- | --- |
| Install, upgrade and get started | [Project README](../README.md) |
| Build, update dependencies and run checks | [Development](../DEVELOPMENT.md) |
| Module ownership, data flow, budgets and shared-library boundaries | [Architecture](architecture.md) |
| Required native, semantic, resource and editor checks; CI evidence replay | [Test contracts](../tests/README.md) |
| Automatic targets, explicit configuration and reference policy | [Configuration](configuration.md) |
| Bundled version catalog, schema behavior and compatibility limits | [Version support](multi-version-support.md) |
| Native server and Zed adapter installation, release and rollback | [Distribution](distribution.md) |
| Shared toolkit API for the pinned dependency | [Toolkit contract](../contracts/toolkit-design-6.md) |
| Problems, causes, current corrections and regression coverage | [Lessons learned](lessons-learned.md) |
| Observing loaded bytes and behavior in an actual Zed session | [Host acceptance](../editors/zed/tests/HOST_ACCEPTANCE.md) |

[Native release notes](native-release-notes.md) and [Zed release notes](../editors/zed/release-notes.md) are inputs to their respective release workflows. Contracts, dependency locks, compatibility records, schemas, provenance and licenses under `contracts/`, `compat/`, `schemas/`, `internal/assets/` and `vendor/` are required build or verification inputs.

Documentation describes current behavior and maintenance procedures. Temporary
plans, audit snapshots and run-by-run progress belong outside this documentation
set. Update the relevant current guide when code changes; keep machine-checked
contracts, release descriptions and upstream legal/provenance inputs intact.
