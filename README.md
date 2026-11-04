# Zed Argo CD

An experimental Zed extension and offline Go language server for Argo CD Application, AppProject, and ApplicationSet YAML. No cluster access, deployment, sync, or generator execution is performed.

## Build and run

The native server requires Go 1.27.3 and a C compiler. The checked-in vendor tree includes the pinned toolkit and native parser sources; no sibling checkout is required.

```sh
GOWORK=off go build -mod=vendor -buildvcs=false -trimpath -o build/argocd-ls ./cmd/argocd-ls
./build/argocd-ls version --json
./build/argocd-ls check --root ./examples --target-version 3.0.0 --format json
./build/argocd-ls serve --stdio
cargo build --locked --release --target wasm32-wasip2
```

Cargo uses the version in `rust-toolchain.toml`; dependencies must be available before an offline build. The toolkit API and implementation identity are pinned in [contracts/toolkit-design-1.md](contracts/toolkit-design-1.md) and [contracts/toolkit.lock.json](contracts/toolkit.lock.json). The toolkit is pinned to public Go pseudo-version `v0.0.0-20261103033718-ae52b82ec6c9` (commit `ae52b82ec6c93244787ec40c87e4171616c0fb73`), with vendored sources for independent builds. Dependency availability does not imply product readiness.

Use `.argocd-ls.yaml` in the workspace root to select a target and namespace:

```yaml
schemaVersion: 1
targetVersion: "3.0.0"
controlPlaneNamespace: argocd
```

`explain-config --root PATH` shows effective configuration. `check --help` lists CLI options. The experimental stdio server provides schema diagnostics, static local references, field queries, reference candidates, and versioned edits. Tests launch real CLI and LSP processes; for example, after building:

```sh
python3 tests/candidates_e2e.py
python3 tests/candidate_review_e2e.py
python3 tests/candidate_concurrency_e2e.py
python3 tests/domain_matrix_e2e.py build/argocd-ls
python3 tests/domain_matrix_lsp_e2e.py
python3 tests/schema_queries_e2e.py build/argocd-ls artifacts/schema-queries
```

Reports and protocol transcripts are written under `artifacts/`. Analysis has a five-second budget. Explicit Helm commands have a thirty-second deadline and a combined stdout/stderr limit of 10 MiB; failures retain command output and status. Unix commands use a separate process group; Windows uses a suspended process assigned to a kill-on-close Job Object before resuming. Windows process lifetime behavior still requires native acceptance.

Each analysis and the open-buffer collection have a fixed 128 MiB text limit. Configuration buffers count toward the limit. A refused buffer revision remains unavailable until a fitting full replacement or close/reopen; stale buffer or disk text cannot substitute for it. These limits are not a process RSS cap.

Cached code actions remain valid after a completed unchanged save or reconciliation. Actual input or document-version changes invalidate them, including changes later restored. Pending analysis must finish before an edit can resolve. Supported localhost file URIs select the same configuration path while replies and edits retain the client URI.

## Current limits

The local editing contract uses the 88 verified catalog targets; an omitted target selects 3.5.6. Complete checks exit 0, diagnostic errors exit 1, and `--warnings-as-errors` also makes warnings exit 1 without changing their severity. Configuration, input or projection incompleteness exits 2; a deadline or interrupt exits 3. `analysisReady` describes the local contract, independently of development build provenance and platform/editor/distribution acceptance. Completed counts exclude skipped projections and cancelled work. 

The local static rule subset is checked against all 88 catalog patches using [fixed upstream source provenance](compat/local-rules-sources.json). Controller operations, generator or template-function execution, patch final effects, hydration evaluation, value-file expansion, and complex policy/RBAC are outside the local-editing contract. 

Field queries cover reliable blank/partial mapping and first-sequence keys, empty enum values, exact plain-text edits, typed maps/arrays and integer-or-string unions, with target and immutable upstream provenance in Hover. Escaped keys and ambiguous recovery receive no guessed edits. Local refs, allOf, const/default and discriminator branches have separate synthetic consumer coverage because the embedded CRDs do not use them. 

Bounded acyclic aliases and YAML merges project values with explicit-key and earlier-merge precedence. Derived or shared-anchor locations do not receive inferred navigation or text edits; exact unrelated siblings remain usable. Ref ambiguity includes every effective declaration, including declarations without exact locations. Failed input with unresolved identity keeps its scope incomplete; proven foreign kinds remain silent. Duplicate GVK keys remain incomplete regardless of key order. Alias/merge graph documents share a fixed 100,000 expanded-node allowance per logical analysis result across roots/scopes. Refused graphs are explicitly incomplete and excluded from completed counts; plain documents retain their existing input budgets. This limit is not a whole-process RSS cap. 

Native platform resource behavior, real Zed editor acceptance, and release delivery remain separate gates. Local readiness has passed scoped review and integration; native delivery and full editor acceptance remain separate gates. The adapter rejects an unready server. Building the WASM extension does not make this a supported or publishable extension. No release download manifest is available.

For development installation, use Zed's **Install Dev Extension** with this directory. The adapter reads an absolute executable path from `lsp.argocd-ls.settings.serverPath` and verifies server identity and readiness. `lsp.argocd-ls.settings.targetVersion` explicitly overrides workspace/scope target pins. Effective worktree settings are forwarded initially and on changes; removing the override restores the file pin or product default. An unsupported target remains an explicit analysis error. This uses workspace configuration, separately from initialization options. Zed's standard `binary.path` bypasses the adapter handshake. Keep the YAML language server enabled for general YAML support.

## License and acknowledgments

Original code and documentation are [MIT licensed](LICENSE). Third-party licenses and copyright notices remain applicable.

Thanks to [k8s-crd-toolkit](https://github.com/lazysnail1024/k8s-crd-toolkit) (MIT), [zed_extension_api](https://github.com/zed-industries/zed/tree/main/crates/extension_api) (Apache-2.0), [serde](https://github.com/serde-rs/serde) and [serde_json](https://github.com/serde-rs/json) (MIT OR Apache-2.0), [sigs.k8s.io/yaml](https://github.com/kubernetes-sigs/yaml) (see its vendored license), [yaml.v3](https://github.com/go-yaml/yaml) (MIT/Apache-2.0), [glsp](https://github.com/tliron/glsp) and [jsonschema/v6](https://github.com/santhosh-tekuri/jsonschema) (Apache-2.0). Exact dependencies are recorded in `go.mod`, `go.sum`, and `Cargo.lock`.

[Argo CD](https://github.com/argoproj/argo-cd) supplies the Apache-2.0 CRDs pinned in `compat/catalog.lock.json`. Raw assets retain upstream bytes; embedded schemas are extracted and normalized derivatives. [PyYAML](https://github.com/yaml/pyyaml) (MIT) is used during asset generation. Tree-sitter, go-pointer, and the YAML grammar retain their MIT notices; the Tree-sitter Unicode license is retained separately. Distribution must include the licenses and applicable notices for the actual bundled components.

The vendored Go text tables use equivalent Unicode escapes for CJK character data; the digit example comment uses Unicode notation. Character values and third-party license files are unchanged.

Configuration presentation was informed by [helm.zed](https://github.com/cabrinha/helm.zed) and [zed-ansible](https://github.com/kartikvashistha/zed-ansible), both MIT licensed; their code was not copied.

## Native build and packaging candidates

`python3 tools/native.py build --platform linux/amd64 --output build/native` records actual native toolchain, source hashes, identity, runtime-library inspection and legal inputs. It uses the pinned Go version, C compiler and offline vendor tree. Compilation runs from a temporary copy of the verified source inventory, so ignored checkout files cannot enter Go, embedded-asset or native C inputs. A required input absent from that inventory fails the build. Native subprocesses use fixed Go/CGO defaults and `GOENV=off`; inherited compiler flags, include/library search paths and persisted Go settings are excluded. The selected Go/C tool installations and system SDK remain trusted; this is not a security sandbox. Development builds retain actual dirty/unknown-source provenance. Release mode additionally requires `--mode release --version X.Y.Z --source-commit FULL_SHA`, exact committed bytes and a clean checkout; it does not publish anything.

`python3 tools/native.py package --manifest build/native/manifest.json --output dist/native` packages that already built native without rebuilding. Copied Go legal text is checked against the selected pinned toolchain; copied CRD legal text is checked against the catalog source digest, independently of the build manifest. Output directories must be new. Archives have a fixed single-root layout, deterministic metadata, binary/archive SHA256, actual linked-module and CRD legal texts, and build provenance. The package has no hosted download URL. Windows builds reject unreviewed non-system DLL dependencies rather than silently omitting them.

Run `python3 tests/native_ci.py --platform linux/amd64 --output artifacts/native-ci-local` for the grouped native build, packaging and current CLI/LSP consumers. The output directory must be new. The runner observes Git provenance independently before building; it also runs finite save/watch action controls, without claiming periodic-timer acceptance on every platform.

The native workflow defines five platform jobs: Linux amd64/arm64, macOS amd64/arm64 and Windows amd64. It retains native build/package, query/projection/lifecycle and bounded child-process evidence. Hosted runner execution does not prove the minimum supported OS or real Zed behavior. The five hosted jobs have not yet been run for this product. Signed release artifacts and registry delivery are not available.
