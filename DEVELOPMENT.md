# Development

## Native language server

Use the Go version in `go.mod` and a C compiler for the vendored parser. Dependencies
are vendored; no Rust toolchain or editor checkout is needed.

```sh
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
go vet -mod=vendor ./cmd/... ./internal/...
./build/argocd-ls version --json
./build/argocd-ls check --root /path/to/yaml --format json
```

Root `VERSION` controls release-mode builds; `docs/native-release-notes.md` supplies
the native release description. From a clean commit, run:

```sh
python tools/release.py version --source-commit "$(git rev-parse HEAD)"
```

The native workflow runs the existing CLI, stdio LSP, performance, resource,
mutation and archive consumers on their designated hosts. Release staging and
collection require exact clean source identity, matching native metadata, complete
licenses and all five platforms. See `.github/workflows/native.yml` for executable
commands and retained evidence. Merging a changed `VERSION` into `main` starts
`release.yml`; after all gates pass it creates `v<VERSION>` at the verified commit
and publishes the native assets. An exact version-tag push or a manual run on
`main` or that tag can also start the same workflow.

## Shared toolkit source

The toolkit dependency is pinned to the full source commit recorded in
`contracts/toolkit.lock.json`, represented by the Go pseudo-version in `go.mod`.
It does not require a toolkit release tag. The vendored `source`, `yamlview`,
`yamlvalue`, `schemaquery`, `edit`, `index`, and `workspace` packages match that
commit; builds need no sibling checkout or local `replace` directive.
[contracts/toolkit-design-4.md](contracts/toolkit-design-4.md) is the byte-identical
API contract, with its implementation and content identities in `contracts/toolkit.lock.json` and
`contracts/toolkit-snapshot.json`.

When advancing the source pin, update the Go dependency, contract snapshot and
lock identities together. Regenerate vendor with `go mod vendor`, then run
`python3 tools/vendor_c.py` to restore and verify the native parser source trees.
Keep `go.sum`, vendored sources and provenance records in the same change.
The contract snapshot is hash-checked build input, not an editable architecture
note. State ownership and the boundary between toolkit and Argo CD policy are
documented in [the current architecture](docs/architecture.md).

## Zed adapter

All adapter code, Rust configuration and assets are under `editors/zed`.

```sh
cd editors/zed
cargo test --locked
cargo build --locked --release --target wasm32-wasip2
```

Select this directory when using Zed's Install Dev Extension. The independent
`native-server-version` file chooses a released LSP; the adapter's own version can
change independently. The adapter package command is also in this directory.
`zed.yml` owns adapter CI. Bump the matching Cargo and extension versions, including
the local package entry in `Cargo.lock`, and update `editors/zed/release-notes.md`.
Merge the changed `editors/zed/extension.toml` into `main` to start `zed-release.yml`.
It also accepts matching `zed-v*` tags and manual runs. After its own gates pass,
it waits up to 45 minutes for the exact native pin to be published with all five
platform executables and `SHA256SUMS`, then creates its tag and publishes the ZIP.
The native release can precede the adapter release and use a different commit;
no Go build is part of the adapter workflow. Existing releases and drafts are
never replaced. See [release automation](docs/distribution.md#release-automation)
for waiting and failure behavior.

The precompiled ZIP is the ordinary alpha installation path and does not need a
Rust toolchain on the user's machine. See [distribution](docs/distribution.md).

## Changes and tests

From the repository root, use the existing consumers for the affected boundary:

```sh
go test -mod=vendor ./internal/...
go test -mod=vendor -race -count=1 ./internal/lsp ./internal/workspace
python3 tests/mixed_parent_index_e2e.py build/argocd-ls artifacts/mixed-parent
python3 tests/persistent_workspace_e2e.py build/argocd-ls artifacts/persistent --count 1000 --padding 4096
python3 tests/config_uri_e2e.py build/argocd-ls artifacts/config-uris
python3 tests/navigation_editing_e2e.py build/argocd-ls artifacts/navigation-editing
python3 tests/navigation_editing_guards_e2e.py build/argocd-ls artifacts/navigation-guards
python3 tests/project_rename_e2e.py build/argocd-ls artifacts/project-rename
python3 tests/shared_schemaquery_e2e.py build/argocd-ls artifacts/shared-schemaquery
```

Use a fresh output directory per run. The complete native entry point is
`python3 tests/native_ci.py --platform <native-platform> --output artifacts/native-check`;
run it on the matching actual host. It builds and executes the required gates.
The Linux workflow additionally executes the large workspace, work-count,
mutation and race consumers, including `navigation_editing_ablation.py`,
`ablate_project_rename.py` and `ablate_shared_schemaquery.py`. These source controls take an output directory,
compile each variant, then require a named protocol or real-index assertion to fail;
compile errors or runner failures are not evidence of detection.
[native.yml](.github/workflows/native.yml) and
[tests/native_ci.py](tests/native_ci.py) are the executable gate definitions.
Specific regressions map to their consumers in [lessons learned](docs/lessons-learned.md).

Use the existing real-process E2E consumers for changed behavior. Keep raw failure
output and reject timeouts, missing reports and unexpected successful mutants.
Foreign-platform synthetic archives test aggregation rules only; they do not
replace native execution. Keep actual upstream CRD tests separate from synthetic
schema query fixtures.

Version collection and normalization preserve upstream commit IDs and SHA-256
source identities. The default auto window is derived from the latest two stable
majors. A new catalog must pass version compatibility consumers before release.
See [version collection and acceptance](docs/multi-version-support.md) for the
catalog update procedure and [the documentation index](docs/README.md) for current
configuration, implementation and distribution boundaries. Source changes do not
update already published native binaries or the adapter's committed download pin.
