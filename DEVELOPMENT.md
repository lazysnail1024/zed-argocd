# Development

For installation and everyday use, see [README.md](README.md).

## Build inputs

The native server requires Go 1.27.3 and a C compiler. Vendored dependencies allow independent builds without a sibling checkout. Cargo uses Rust 1.99.1 from `rust-toolchain.toml`; fetch its locked dependencies before an offline build.

```sh
cargo build --locked --release --target wasm32-wasip2
```

The toolkit API and implementation identity are pinned in [the contract snapshot](contracts/toolkit-design-1.md) and [dependency lock](contracts/toolkit.lock.json). Exact dependency versions are recorded in `go.mod`, `go.sum`, and `Cargo.lock`.

## Native build and packaging

`python3 tools/native.py build --platform linux/amd64 --output build/native` records actual native toolchain, source hashes, identity, runtime-library inspection and legal inputs. It uses the pinned Go version, C compiler and offline vendor tree. Compilation runs from a temporary copy of the verified source inventory, so ignored checkout files cannot enter Go, embedded-asset or native C inputs. A required input absent from that inventory fails the build. Native subprocesses use fixed Go/CGO defaults and `GOENV=off`; inherited compiler flags, include/library search paths and persisted Go settings are excluded. The selected Go/C tool installations and system SDK remain trusted; this is not a security sandbox. Development builds retain actual dirty/unknown-source provenance. Release mode additionally requires `--mode release --version X.Y.Z --source-commit FULL_SHA`, exact committed bytes and a clean checkout; it does not publish anything.

`python3 tools/native.py package --manifest build/native/manifest.json --output dist/native` packages that already built native without rebuilding. Copied Go legal text is checked against the selected pinned toolchain; copied CRD legal text is checked against the catalog source digest, independently of the build manifest. Output directories must be new. Archives have a fixed single-root layout, deterministic metadata, binary/archive SHA256, actual linked-module and CRD legal texts, and build provenance. The package has no hosted download URL. Windows builds use the fixed MinGW distribution recorded in `tools/windows-runtime.json`, validate its compiler/static runtime and complete notice bytes, and link the runtime statically. Packages retain the MinGW, winpthreads and GCC runtime notices. Non-system DLL imports remain rejected; the original linker map is retained in build artifacts.

Run `python3 tests/native_ci.py --platform linux/amd64 --output artifacts/native-ci-local` for the grouped native build, packaging and current CLI/LSP consumers. The output directory must be new. The runner observes Git provenance independently before building and records the complete required step list before running consumers. A failed or omitted required consumer fails the gate, even if every other step succeeds. Failure evidence is retained. The consumers include save/watch actions and the real 30-second lost-event reconciliation timer.

The native workflow defines five platform jobs: Linux amd64/arm64, macOS amd64/arm64 and Windows amd64. It retains native build/package, query/projection/lifecycle and bounded child-process evidence. The performance and usability audit consumers below are required in these jobs, not just manual commands. Linux amd64 additionally runs the full pinned-target ApplicationSet matrix, the fixed 1,000/3,000-resource performance workloads and source mutation checks. The other platforms exercise ApplicationSet behavior at the first and latest tested targets, while all platforms run the new boundary regressions. Hosted runner execution does not prove the minimum supported OS or real Zed behavior. See the [native workflow](https://github.com/lazysnail1024/zed-argocd/actions/workflows/native.yml) for each commit's actual platform outcomes; a successful job on another commit is not acceptance of the current bytes. Signed release artifacts and registry delivery are not available.

## Behavioral verification

After building a server at `build/argocd-ls`, these runners exercise real CLI/LSP processes:

```sh
python3 tests/candidates_e2e.py
python3 tests/candidate_review_e2e.py
python3 tests/candidate_concurrency_e2e.py
python3 tests/domain_matrix_e2e.py build/argocd-ls
python3 tests/domain_matrix_lsp_e2e.py
python3 tests/schema_queries_e2e.py build/argocd-ls artifacts/schema-queries
```

Reports and protocol transcripts are retained under `artifacts/`. Native CLI/LSP verification and real Zed acceptance are separate: a successful native job does not demonstrate the editor UI on that platform. Keep evidence bound to the actual source and artifact identities.

## Source provenance

The static local rule subset is checked across the catalog using [fixed upstream source provenance](compat/local-rules-sources.json). Raw CRDs retain upstream bytes; embedded schemas are normalized derivatives. Distribution must retain the licenses and applicable notices for actual bundled components, including the native runtime. See [acknowledgments](README.md#license-and-acknowledgments).

## Precompiled development extension

Produce a development ZIP and extracted installable folder from an existing native build:

```sh
python3 tools/dev_bundle.py --manifest build/native/manifest.json --platform linux/amd64 --output dist/dev-linux-amd64
python3 tests/dev_bundle_e2e.py --manifest build/native/manifest.json --output artifacts/dev-bundle-e2e
```

The producer reuses `native.py package` to verify native SHA, actual product/readiness/platform/build identity, toolchain and all legal inputs. It never rebuilds Go. Run it on the native target platform with the pinned Go toolchain available for provenance inspection. The archive contains the Rust adapter sources and lock, snippets, usage documentation, native payload and its manifest, and complete native build/legal provenance. ZIP entries are sorted with fixed metadata. Outputs must be new directories. The generated copy enables `bundled-server`; source checkouts leave it disabled. No hosted URL or formal release is implied.

For source-checkout development, first build the native server using the commands above. Set `lsp.argocd-ls.settings.serverPath` to its absolute path, then select the checkout root in Zed Install Dev Extension. Windows paths may use `C:/src/zed-argocd/build/native/argocd-ls.exe`. This explicit override always wins over an embedded server and must pass the same identity/readiness handshake. Zed's generic `binary.path` bypasses the adapter and its checks.

The bundled adapter checks the host platform before extraction and compares cached executable bytes with its embedded payload. It repairs missing or altered regular cache files, refuses unsafe cache entry kinds, and returns an absolute executable path before applying the existing identity/readiness handshake.

## Interaction and delivery acceptance

The interaction remediation consumers exercise real framed stdio and CLI processes:

```sh
python3 tests/interaction_remediation_e2e.py build/argocd-ls artifacts/interaction --large
python3 tests/host_capabilities_e2e.py build/argocd-ls artifacts/host-capabilities
python3 tests/appset_overrides_e2e.py build/argocd-ls artifacts/appset-overrides --all-targets
python3 tests/quickfix_scaling_e2e.py build/argocd-ls artifacts/quickfix-scaling
python3 tests/incremental_performance_e2e.py build/argocd-ls artifacts/performance --require-incremental
python3 tests/lsp_regressions_e2e.py build/argocd-ls artifacts/lsp-regressions
python3 tests/semantic_regressions_e2e.py build/argocd-ls artifacts/semantic-regressions
python3 tests/workspace_regressions_e2e.py build/argocd-ls artifacts/workspace-regressions
python3 tests/audit_regression_mutations.py build/argocd-ls artifacts/audit-mutations
```

Each output directory must be new. `ARGOCD_LS_TRACE=1` writes bounded per-analysis
disk-read, analyzer-parse, repository-decode and domain counts, plus selected fix validation counts to stderr. The
performance runner enables this trace and records 30 comment edits per size,
raw timings, p50/p95 and Linux RSS samples when available. Full reconciliation
is reported separately. Diagnostic-pass traces exclude on-demand read-query work; they report analyzed file counts, not every internal parser invocation. Fix traces count actual started analysis/domain calls, including failed or cancelled calls. Traces do not include document text or candidate names.

Buffer-only changes reuse acquired membership and unchanged immutable file
results. Configuration, open/close, watched events and periodic reconciliation
perform fresh acquisition. Application changes check the changed resource
against Projects; Project changes invalidate their scope's domain checks. Repository mappings reuse unchanged per-file reference extraction; the separately reported `repositoryDecodes` counter covers that YAML decoding work.
Resolve validates only the selected Project replacement while retaining the
current revision, target, complete-scope and no-new-Error requirements.

The native workflow also builds the actual bundled WASM adapter and retains a
platform development ZIP. Workflow preparation is not evidence of an uploaded
artifact: a run must complete on the exact source before its bundle is available.
The local native, bundle and WASM layers do not replace a real Zed installation
record or establish acceptance on platforms that have not run.

The [November audit repair plan and acceptance matrix](docs/audit-remediation-2026-11.md)
maps every remaining audit issue to its fix and regression consumer. The new
consumers use real CLI/LSP processes and include working controls. The Linux
scan-cancellation case uses inotify to observe the scan opening a file before
issuing the cancelling edit; platforms without inotify record this case as
unsupported instead of pretending to run it. Windows runs real drive-URI
incremental checks. Symlink tests record a skip only if the host cannot create
the test-owned symlink. The fixed Linux performance runs enforce actual work
counts (zero disk reads, one changed file parsed, two domain documents), retain
all 30 samples per size, and report p50/p95 without changing the debounce timer.
Source mutations must compile, the unmodified binary must pass, and the changed
binary must fail the selected regression assertion; a compiler or runner failure
does not count as a detected regression.
