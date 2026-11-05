# Architecture and release boundaries

## Server

The repository root is the independent Go module. `cmd/argocd-ls` provides the CLI
and stdio LSP. `internal/` owns configuration, analysis, workspace state, protocol
handling and embedded schema access. `compat/`, `schemas/` and the existing
catalog-generation tools preserve pinned upstream inputs. The root `VERSION` is
the native server's release version.

The server does not import Zed, read Cargo manifests or require Rust to build.
Native release tooling is in `tools/`; `.github/workflows/native.yml` executes the
five native host matrices and `.github/workflows/release.yml` publishes `v<VERSION>`.
Native archives contain the executable, legal notices and source/build provenance.
Changing `VERSION` on `main` starts the complete native release gate. Only after
it passes does the workflow create the version tag at that exact commit and
publish its artifacts. Matching tag pushes and manual runs remain available.

## Zed adapter

`editors/zed/` is an independent Rust crate with its own lockfile, toolchain,
manifest, snippets, tests and package command. It can be copied and built without
Go sources or the server's release scripts. `native-server-version` fixes the
server release consumed by that adapter; it is independent of the adapter's own
Cargo/extension version.

The adapter resolves an explicit server path, then shell PATH, then its exact
release pin. It validates integrity and native identity and starts
`argocd-ls serve --stdio`. It owns no Argo schema or semantic analysis.

`.github/workflows/zed.yml` tests the adapter on the supported host OS families
and compiles the portable WASI Preview 2 component once. The independent
`zed-release.yml` publishes `zed-v<adapter-version>`. No Go build is needed for
an adapter release; no Rust build is needed for a native release.
Changing `editors/zed/extension.toml` on `main` starts the adapter release gate.
Before publishing, it checks that the exact `native-server-version` release is
published with all five platform executables and `SHA256SUMS`. For an initial
parallel release it polls every 30 seconds for at most 45 minutes; only an absent
release or an unpublished draft is allowed to wait. This dependency is on the
published native artifacts, not on a Go build job or a shared version number.

Each product serializes its own releases. Existing tags must resolve to the
verified commit, and existing releases, including drafts, are never overwritten.

## Future editors

VS Code and Neovim can invoke the same native executable and stdio protocol.
There are no placeholder editor interfaces, adapter registries or speculative
plugin frameworks. An additional editor integration can live in its own directory
and choose a compatible native version without changing the analysis code.

## Validation of the boundary

The refactor is checked by building each side after removing the other side's
files from an isolated source snapshot. Adapter version and native pin are varied
independently; restoring the old Cargo-version coupling must fail its consumer
tests. Release archives are inspected and their extracted programs executed.
See the [ablation report](ablation.md) for measured outcomes and rejected deletions.
