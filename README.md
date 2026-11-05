# Zed Argo CD

Offline YAML support for Argo CD `Application`, `AppProject`, and `ApplicationSet`: schema diagnostics, field completion and hover, local navigation and references, and limited safe edits. It does not connect to a cluster or deploy, sync, or execute generators.

**Store publication is pending.** The Rust adapter now downloads a version-pinned native server through the Zed Extension API, or uses a user-provided executable. It contains no native server payload. The first pinned `v0.1.0` GitHub Release, real Zed acceptance, and the extension-registry review are still required. The [release checklist](docs/store-distribution.md) records the outstanding conditions.

## Install

### Precompiled developer package

Until the first native Release is published, use a development ZIP from a successful [Native candidate workflow run](https://github.com/lazysnail1024/zed-argocd/actions/workflows/native.yml). Find `dev-bundle/` inside the matching `argocd-native-<platform>` artifact. Only a completed run for the current commit provides this layout; older artifacts may still use the retired embedded-server layout. GitHub may require sign-in for workflow artifacts.

The ZIP contains an adapter-only `extension/` folder and a separate, already compiled `server/` folder with its licenses and provenance. No Go or C compiler is needed to consume this package.

1. Unzip the package into a permanent directory, preserving both folders.
2. Install Rust through **rustup** and make it available to Zed. The adapter pins Rust **1.99.1** and `wasm32-wasip2`; the first toolchain and locked Cargo dependency downloads require network access.
3. Merge `settings.example.json` into your Zed settings, replacing its example `lsp.argocd-ls.settings.serverPath` with the **absolute** path to `server/argocd-ls` (`server/argocd-ls.exe` on Windows).
4. Choose **Install Dev Extension** on Zed's Extensions page, or run `zed: install dev extension`, and select the extracted **`extension/`** directory containing `extension.toml`.
5. Keep both folders in place and open a YAML project. Zed builds the Rust adapter, then the adapter checks the configured server's product, contract and readiness before starting it.

For example:

```json
{
  "lsp": {
    "argocd-ls": {
      "settings": {
        "serverPath": "/absolute/path/to/zed-argocd-dev-linux-amd64/server/argocd-ls"
      }
    }
  }
}
```

Windows paths may use `C:/tools/zed-argocd-dev-windows-amd64/server/argocd-ls.exe`. Keep Zed's YAML language support enabled. An explicit invalid `serverPath` stops startup and reports an error; it is never silently ignored.

### Managed native server

The source adapter's selection order is `settings.serverPath`, `argocd-ls` on the worktree's shell `PATH`, then the pinned GitHub Release. A local executable must pass the adapter identity checks. Managed downloads additionally require a clean release with the exact pinned server version.

With no local executable configured, this adapter selects **`v0.1.0`** and its Linux amd64/arm64, macOS amd64/arm64, or Windows amd64 asset. It uses Zed's `download_file` API for both `SHA256SUMS` and the executable, checks the exact asset's SHA-256 **before any execution**, and verifies server identity before activating the cache. A missing Release or asset is an installation error. As of the 2026-11-05 implementation review, that first Release has not been published, so use the developer `serverPath` installation above until it is available.

After a successful installation, startup revalidates cached bytes and identity without any network request. An offline first installation needs an executable installed through `serverPath` or `PATH`. A damaged or incomplete managed cache requires a successful re-download; unverifiable bytes never run. Checksums obtained from the same HTTPS Release detect corrupted or mismatched downloads; they are not an independent publisher signature.

Managed upgrades occur when an extension update changes the pinned version. Each version and platform gets its own cache directory. Failed downloads, checksum checks, permissions or identity checks leave previous versions intact and report an error; the new adapter does not silently select an older version. To roll back, install the previous adapter version or explicitly select a compatible known-good server. Old version caches are retained for rollback. For developer ZIP upgrades, extract into a new directory, update `serverPath`, select its `extension/` directory, restart Zed and verify a YAML diagnostic before removing the previous package.

Source-checkout installation and contributor overrides are described in [Development](DEVELOPMENT.md). See also [Zed's development-extension instructions](https://zed.dev/docs/extensions/developing-extensions).

## First YAML file

Open a project folder in Zed and save this as `application.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: example
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: HEAD
    path: guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: guestbook
```

Hover a schema field such as `project`, or request completion inside `spec`. As a diagnostic check, temporarily change `spec.project` to `123`: a string-type schema error should appear. Restore `default` afterward. The server does not fetch the example repository; external references may be reported as unresolved informational diagnostics.

To pin the Argo CD version, add `.argocd-ls.yaml` to the project root:

```yaml
schemaVersion: 1
targetVersion: "3.0.0"
controlPlaneNamespace: argocd
```

Without a pin, the current catalog defaults to **3.5.6**. It contains 88 verified exact targets. An unsupported version is an error, not a fallback. Optional `lsp.argocd-ls.settings.targetVersion` overrides file pins; removing it restores the file pin or default. CLI commands do not read Zed settings.

## Verify and troubleshoot

- If startup fails, run your configured `server/argocd-ls` (`argocd-ls.exe` on Windows) or managed cached executable with `version --json` and check its identity, readiness, execution permissions, and absolute path. Confirm an old `serverPath` or shell `PATH` entry is not selecting a different server.
- For a managed download failure, check that the pinned Release and platform asset exist and Zed allows the download and identity-process capabilities. Restore network access and restart the language server to retry. Close Zed before manually inspecting or removing a broken cache slot; a valid cache needs no network.
- For extension compilation or startup errors, run `zed: open log`. Check that rustup and the pinned toolchain are available to Zed. [Zed documents additional logging](https://zed.dev/docs/extensions/developing-extensions).
- If no Argo features appear, confirm the file is in YAML mode and the `argocd-ls` server has not been disabled in language settings. Keep the general YAML server enabled as well.
- If configuration is rejected, use `explain-config` below and remove unsupported target overrides. Existing YAML schema associations can cause duplicate diagnostics; this extension does not modify them.

## CLI

Run from the extracted bundle folder, replacing `/path/to/project` with your YAML project:

```sh
./server/argocd-ls explain-config --root /path/to/project
./server/argocd-ls check --root /path/to/project --target-version 3.0.0 --format json
./server/argocd-ls check --help
```

On Windows, invoke `server/argocd-ls.exe` directly.

Checks exit **0** when complete without errors, **1** for diagnostic errors (also warnings with `--warnings-as-errors`), **2** for configuration/input/analysis incompleteness, and **3** for cancellation or deadline. `analysisComplete` describes the particular check; `analysisReady` describes the server's local editing contract.

## Limits

Analysis is local and static. Controller behavior, generator/template execution, dynamic or controller-specific patch effects, hydration, value-file expansion, and complex policy/RBAC evaluation are outside its scope. Ambiguous or derived YAML locations do not receive guessed edits. Analysis has a five-second budget, fixed text budgets, and bounded alias expansion; refused input is incomplete rather than silently accepted.

Development installation remains the available delivery mode until the release and registry checklist is completed. Native CLI/LSP CI is configured for Linux amd64/arm64, macOS amd64/arm64, and Windows amd64; it does not establish real Zed GUI acceptance or minimum supported OS versions. All five platforms still require editor acceptance for this installer. No signed native artifacts or completed store publication are claimed. Contributor builds, packaging, tests, and provenance are in [Development](DEVELOPMENT.md).

## License and acknowledgments

Original code and documentation are [MIT licensed](LICENSE). Third-party licenses and copyright notices remain applicable.

Thanks to [k8s-crd-toolkit](https://github.com/lazysnail1024/k8s-crd-toolkit) (MIT), [zed_extension_api](https://github.com/zed-industries/zed/tree/main/crates/extension_api) (Apache-2.0), [serde](https://github.com/serde-rs/serde), [serde_json](https://github.com/serde-rs/json), and [sha2](https://github.com/RustCrypto/hashes) (MIT OR Apache-2.0), [sigs.k8s.io/yaml](https://github.com/kubernetes-sigs/yaml) (see its vendored license), [yaml.v3](https://github.com/go-yaml/yaml) (MIT/Apache-2.0), [glsp](https://github.com/tliron/glsp) and [jsonschema/v6](https://github.com/santhosh-tekuri/jsonschema) (Apache-2.0). Exact dependencies are recorded in `go.mod`, `go.sum`, and `Cargo.lock`.

[Argo CD](https://github.com/argoproj/argo-cd) supplies the Apache-2.0 CRDs pinned in `compat/catalog.lock.json`. Raw assets retain upstream bytes; embedded schemas are extracted and normalized derivatives. [PyYAML](https://github.com/yaml/pyyaml) (MIT) is used during asset generation. Tree-sitter, go-pointer, and the YAML grammar retain their MIT notices; the Tree-sitter Unicode license is retained separately. Distribution must include the licenses and applicable notices for the actual bundled components.

The vendored Go text tables use equivalent Unicode escapes for CJK character data; the digit example comment uses Unicode notation. Character values and third-party license files are unchanged.

Configuration presentation was informed by [helm.zed](https://github.com/cabrinha/helm.zed) and [zed-ansible](https://github.com/kartikvashistha/zed-ansible), both MIT licensed; their code was not copied.

## Project navigation configuration

Set `controlPlaneNamespace: argocd` in the root `.argocd-ls.yaml` when your local
AppProject resources belong to `argocd`. This is independent of `targetVersion`:
it identifies the Project namespace, not the namespace of each Application.
Cross-namespace Applications still use that configured Project namespace. Without
it, completion shows qualified candidates but cannot establish a navigation target.
Configuration changes refresh automatically; no cluster access is needed.

## Helm editing

Pure YAML in a chart's `templates` directory receives ordinary Argo CD checks.
Files containing Helm expressions show `HELM_UNRENDERED`. They need
an explicit render before final resource checks. Ordinary typing never runs Helm.
The extension registers both `YAML` (`yaml`) and `Helm` (`helm`), using the Helm
language provided by the separate [Helm extension](https://github.com/cabrinha/helm.zed).
The native protocol acceptance suite checks those manifest mappings, unsaved
template transitions, and closing/reopening the same URI with a different
language ID. These stdio tests do not launch Zed. Actual Helm language selection,
server attachment, and coexistence with YAML/Helm servers still require real Zed
validation on each supported platform.

Copy [the example task](examples/zed-tasks.json) into your project's `.zed/tasks.json`
and merge it with existing tasks. Configure `ARGOCD_LS_BINARY`, `ARGOCD_CHART`,
`ARGOCD_HELM_BINARY` (absolute paths), and `ARGOCD_RENDER_OUTPUT` (a new output
directory) in the task's `env`. Run **task: spawn**, then select the Argo CD task.
Add `--values` and a local values path to `args` when needed. The terminal reports
missing tools, rendering failures and resource errors. Diagnostics refer to the
retained generated YAML; they do not guess source-template line numbers. Install
Helm 3 only for this explicit workflow; static YAML editing does not require it.

Clients supporting dynamic file watching receive a workspace watcher registration.
Other clients, including failed registrations, retain the 30-second reconciliation
scan; editing or saving also refreshes analysis. GUI event delivery remains a
separate acceptance step from the native protocol tests.
