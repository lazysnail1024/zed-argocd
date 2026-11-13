# Documentation

Installation and release instructions describe published artifacts. Development instructions apply to the checked-out source; a published binary may not include later source changes.

| Topic | Documentation |
| --- | --- |
| Install, upgrade and get started | [Project README](../README.md) |
| Build, update dependencies and run checks | [Development](../DEVELOPMENT.md) |
| Automatic targets, explicit configuration and reference policy | [Configuration](configuration.md) |
| Bundled version catalog, schema behavior and compatibility limits | [Version support](multi-version-support.md) |
| Native server and Zed adapter installation, release and rollback | [Distribution](distribution.md) |
| Shared toolkit API and ownership boundary | [Toolkit contract](../contracts/toolkit-design-6.md) |

[Native release notes](native-release-notes.md) and [Zed release notes](../editors/zed/release-notes.md) are inputs to their respective release workflows. Contracts, dependency locks, compatibility records, schemas, provenance and licenses under `contracts/`, `compat/`, `schemas/`, `internal/assets/` and `vendor/` are required build or verification inputs.
