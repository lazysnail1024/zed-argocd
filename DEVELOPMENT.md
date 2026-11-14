# Development

These instructions apply to the checked-out source. Published native binaries
and the Zed adapter use the independent versions described in
[distribution](docs/distribution.md). See [architecture](docs/architecture.md)
for module ownership and [test contracts](tests/README.md) for execution and
evidence requirements.

## Native server

Use the Go version in [go.mod](go.mod), a C compiler and Python. CI pins Python
and host compiler versions in [.github/workflows/native.yml](.github/workflows/native.yml).
Go dependencies and parser sources are vendored; the native build does not
require Rust, Zed or a sibling toolkit checkout.

```sh
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
go vet -mod=vendor ./cmd/... ./internal/...
./build/argocd-ls version --json
./build/argocd-ls check --root /path/to/yaml --format json
```

A direct `go build` produces a development executable. Distribution builds use
[tools/native.py](tools/native.py) to validate the toolchain, source inputs,
identity, dependency notices and native archive. Release identity requires a
clean committed tree and the actual target host; cross-compilation does not
replace platform execution.

## Choose the affected verification boundary

Use the existing consumer for the changed behavior. The
[test matrix](tests/README.md#required-coverage-matrix) maps configuration,
navigation, edits, workspace lifecycle, rendering and distribution to consumers;
[inventory.json](tests/inventory.json) is the checked file-to-runner map.

```sh
go test -mod=vendor ./internal/...
go test -mod=vendor -race -count=1 ./internal/lsp ./internal/workspace
python3 tests/test_inventory.py
python3 tests/navigation_editing_e2e.py build/argocd-ls artifacts/navigation-editing
python3 tests/project_rename_e2e.py build/argocd-ls artifacts/project-rename
python3 tests/shared_schemaquery_e2e.py build/argocd-ls artifacts/shared-schemaquery
```

Use a fresh output directory per consumer. These examples do not cover the
complete CI matrix. The complete native entry point is
`python3 tests/native_ci.py --platform <os/arch> --output artifacts/native-check`
on the matching actual host. Linux semantic, Helm, mutation and resource lanes
have their own [commands and prerequisites](tests/README.md#run-locally).

Keep passing controls, exact protocol output, complete case inventories and
failure reasons. A timeout, compiler failure or unrelated exception cannot prove
semantic fault detection. Fixtures under `tests/fixtures/contracts/schema-query`
are reviewed expectations; CI never rewrites them. A scenario change must update
its acceptance contract and inventory in the same review.

## Shared toolkit source dependency

[contracts/toolkit.lock.json](contracts/toolkit.lock.json) records the exact
toolkit source commit; [go.mod](go.mod) uses its immutable pseudo-version. The
vendored packages are `source`, `yamlview`, `yamlvalue`, `schemaquery`, `edit`,
`index` and `workspace`. This pin does not follow toolkit `main` or require a
published toolkit release.

[contracts/toolkit-design-6.md](contracts/toolkit-design-6.md) is the
byte-identical API contract for that pin. Its hashes, implementation identity and
source inventory are checked through `toolkit.lock.json`, `toolkit-snapshot.json`
and `vendor-toolkit-sources.json`. Preserve it when editing prose documentation;
it is a build input.

When advancing the source pin, update the Go dependency, contract snapshot and
lock identities together. Run `go mod vendor`, then `python3 tools/vendor_c.py`
to restore and verify the parser's native source trees. Commit `go.sum`, vendor
content and provenance records together. Verify the native provenance lane and
the affected real-process consumers. The
[ownership table](docs/architecture.md#shared-toolkit-boundary) identifies the
behavior retained in this repository.

## Catalog maintenance

The offline catalog is generated from pinned upstream release commits and
content hashes. Its automatic window covers the latest two stable majors at the
recorded collection cutoff. Follow
[version collection and acceptance](docs/multi-version-support.md#updating-the-catalog)
to collect, normalize, bootstrap, accept and rebuild a changed catalog. Schema
changes invalidate existing verification markers. Actual upstream CRDs and
synthetic query fixtures remain separate verification boundaries.

## Zed adapter

Adapter code, Rust configuration and packaging live in `editors/zed`.
Use its [rust-toolchain.toml](editors/zed/rust-toolchain.toml):

```sh
cd editors/zed
cargo test --locked
cargo build --locked --release --target wasm32-wasip2
```

Select this directory in Zed's **Install Dev Extension** action. Its
`native-server-version` chooses the native download independently of the adapter
version. `zed.yml` verifies the adapter without native source files. Ordinary
users install the precompiled ZIP and need neither Go nor Rust.

The file installer, Rust host-interface tests, WASM/package tests and a real Zed
session prove different boundaries. For GUI verification, use the current
[host acceptance procedure](editors/zed/tests/HOST_ACCEPTANCE.md) and retain
loaded adapter/native identities with the observations.

## Release maintenance

Root `VERSION` and `docs/native-release-notes.md` control the native release.
The adapter uses `editors/zed/extension.toml`, matching Cargo metadata and
`editors/zed/release-notes.md`. Keep the local package version in `Cargo.lock`
consistent. Validate a clean native source identity with:

```sh
python3 tools/release.py version --source-commit "$(git rev-parse HEAD)"
```

Merging a changed version file starts the corresponding release workflow.
Matching tags and manual runs are also supported. Tags/releases are created only
after their acceptance gates; existing published releases and drafts are never
replaced. See [release automation](docs/distribution.md#release-automation) for
triggers, native-pin waiting and retry behavior. Updating docs or merging source
changes does not update a published binary or its committed download pin.
