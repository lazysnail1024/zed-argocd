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

## Navigation and Helm acceptance

The native platform consumers include `navigation_defaults_e2e.py` and
`helm_source_e2e.py`; each takes a binary path and a dedicated evidence directory.
The orthogonal boundary suite installs checksum-pinned Helm 3 and 4 through
`tools/install_test_helm.py` and checks explicit render actions, saved-buffer
refusal, chart discovery and selected-file scope. Run it locally with:

```sh
python tools/install_test_helm.py --output artifacts/helm3
python tools/install_test_helm.py --major 4 --output artifacts/helm4
python tests/orthogonal_ci.py boundaries --binary build/argocd-ls --helm artifacts/helm3/helm --helm4 artifacts/helm4/helm --output artifacts/helm-boundaries
```

Use fresh output directories. `ablate_config_helm.py` prepares source snapshots by
default; its explicit `--run --binary PATH --helm PATH --output DIR` mode verifies
passing controls before compiling three isolated behavior-guard deletions. The
orthogonal mutation suite runs that same consumer. It never modifies the
candidate binary or working implementation.

## Shared toolkit source

The toolkit dependency is pinned to the full source commit recorded in
`contracts/toolkit.lock.json`, represented by the Go pseudo-version in `go.mod`.
It does not require a toolkit release tag. The vendored `source`, `yamlview`,
`yamlvalue`, `schemaquery`, `edit`, `index`, and `workspace` packages match that
commit; builds need no sibling checkout or local `replace` directive.
[contracts/toolkit-design-6.md](contracts/toolkit-design-6.md) is the byte-identical
API contract, with its implementation and content identities in `contracts/toolkit.lock.json` and
`contracts/toolkit-snapshot.json`.

When advancing the source pin, update the Go dependency, contract snapshot and
lock identities together. Regenerate vendor with `go mod vendor`, then run
`python3 tools/vendor_c.py` to restore and verify the native parser source trees.
Keep `go.sum`, vendored sources and provenance records in the same change.
The contract snapshot is hash-checked build input, not an editable architecture
note. State ownership and the boundary between toolkit and Argo CD policy are
defined by the [shared toolkit API contract](contracts/toolkit-design-6.md).

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

See the [test coverage and execution matrix](tests/README.md) for every test family,
mandatory Linux semantic/resource lanes, oracle controls and current limitations.
The [machine-checked inventory](tests/inventory.json) rejects unclassified tests.

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
The [test inventory](tests/inventory.json) maps existing consumers to their acceptance gates.

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

### CI plans, build receipts and replay

`python tools/ci_baseline.py --commit <full-sha> --output artifacts/baseline.json`
exports immutable test inputs and lane contracts. It does not certify execution.
Native acceptance keeps fifteen native dimensions, all orthogonal suites, race,
source mutations and constrained resource consumers. The ordinary Linux release
producer serves native consumers and orthogonal baselines; a separate ordinary
development producer serves resource lanes. Both start with empty compilation
and module caches. No warm compiler cache is enabled. Compressed evidence is
uploaded without recompression.

Consumers require the producer receipt digest from the same workflow dependency,
run and attempt, then verify source inputs, bytes and the executable identity.
A failed-only job rerun cannot certify inherited earlier-attempt producer bytes:
rerun the producer and all dependent consumers together. Fault, provenance,
synthetic-schema and race builds retain their independent execution paths.

`tools/ci_plan.py` records base, head and tested SHA independently. Only modifications
to README.md and DEVELOPMENT.md are eligible for the prose-only native plan.
Mixed, renamed, deleted, unknown, schema, dependency, test or CI changes select
full; an unavailable diff also selects full. Main and release always select full.
The stable candidate gate recomputes the plan and rejects unexpected skips,
missing jobs, wrong identities and unplanned blocking work. The Zed acceptance
workflow always runs independently, including prose-only PRs.

Transport controls run with `python tests/session_transport_e2e.py --output
artifacts/session-controls`. Domain oracles remain in the real LSP consumers;
the shared transport does not decide Argo semantics. Schema fixtures and their
stable IDs live under `tests/fixtures/contracts/schema-query`; edit expectations
explicitly and review the diff. CI never updates fixtures. Run
`python tests/incremental_equivalence_e2e.py <binary> artifacts/equivalence` to
compare incremental and fresh final states, including overlays and root identity.
Repeated semantics alone do not prove a cache hit; existing incremental work-count
and cancellation consumers remain required.

Published file-install acceptance validates successful upstream identity, current
adapter source/version/native pin, downloaded asset IDs and digests, then retains
upgrade/rollback and fault controls. It is not real Zed host evidence. To replay:
`python editors/zed/tests/install_e2e.py --source-commit <published-source-sha>
--report artifacts/zed-installer`.

For CI timing, save complete paginated Actions jobs JSON per run/attempt and use
`python tools/ci_trends.py --jobs <jobs.json> --output artifacts/trends.json`.
Keep first-attempt failures and diagnostic reruns separately. Pair only matching
platform/toolchain/selection/cache groups; report missing samples. Timing summaries
do not relax product thresholds or certify the planned 5 warm/3 cold paired runs,
20 natural PR observations, or actual desktop acceptance.

Native acceptance uploads the original consumer JSON reports separately from the lane summary. The aggregate validates their case and assertion inventories against `tests/child_contracts.json`, their recorded runner and binary hashes, and their artifact digests. Changes to consumer scenarios must update this acceptance contract in the same review. Full transcripts remain in the separate evidence archive. Reports with zero cases, missing assertions, or nonboolean success values cannot satisfy the gate.

Linux orthogonal reports use the reviewed case and assertion inventories in
`tests/orthogonal_contracts.py`, including the independent schema query fixtures
and named mutation failures. Resource reports validate every raw timing and RSS
sample before applying the original counts and thresholds. Both runners reject
nonfinite JSON and duplicate object keys. The required orthogonal contracts and
resource lifecycle jobs replay negative controls against their freshly produced
reports before archiving evidence. To replay downloaded reports locally:

```sh
python tests/acceptance_evidence_e2e.py --orthogonal artifacts/orthogonal-ci/tests --output artifacts/orthogonal-controls
python tests/acceptance_evidence_e2e.py --resources artifacts/resources --output artifacts/resource-controls
```

Use a new output directory for each replay. It retains each input, verifier and
input hashes, command, exit status, stdout, stderr, and rejection reason. These
controls establish evidence acceptance; they do not measure product performance.

`tests/observation_evidence_e2e.py` additionally replays ordinary observations,
mutation positive controls and designated raw failures, retained-budget RSS, and
the CLI process deadline. Pass `--contracts` or `--mutations` with the respective
orthogonal output directory, `--retained` with the multiroot retained-budget
report, or `--resources` with the lifecycle output directory, plus a new
`--output` directory. The corresponding required CI jobs run these controls.
