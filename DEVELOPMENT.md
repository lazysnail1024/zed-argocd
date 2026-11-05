# Development

## Native language server

Use the Go version in `go.mod` and a C compiler for the vendored parser. Dependencies
are vendored; no Rust toolchain or editor checkout is needed.

```sh
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
go vet -mod=vendor ./...
./build/argocd-ls version --json
./build/argocd-ls check --root /path/to/yaml --format json
```

Root `VERSION` controls release-mode builds. From a clean commit, run:

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
`zed.yml` owns adapter CI. Bump the matching Cargo and extension versions and merge
the changed `editors/zed/extension.toml` into `main` to start `zed-release.yml`.
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

Use the existing real-process E2E consumers for changed behavior. Keep raw failure
output and reject timeouts, missing reports and unexpected successful mutants.
Foreign-platform synthetic archives test aggregation rules only; they do not
replace native execution. Keep actual upstream CRD tests separate from synthetic
schema query fixtures.

Version collection and normalization preserve upstream commit IDs and SHA-256
source identities. The default auto window is derived from the latest two stable
majors. A new catalog must pass version compatibility consumers before release.
See [configuration](docs/configuration.md), [architecture](docs/architecture.md),
and the ablation report for the current boundaries and measured experiments.
