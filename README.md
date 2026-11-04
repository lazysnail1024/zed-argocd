# Zed Argo CD

Offline YAML support for Argo CD `Application`, `AppProject`, and `ApplicationSet`: schema diagnostics, field completion and hover, local navigation and references, and limited safe edits. It does not connect to a cluster or deploy, sync, or execute generators.

**Install a precompiled development bundle with Zed's Install Dev Extension command.** There is no registry installation or hosted release download yet.

## Install

Download the development ZIP from a successful [Native candidate workflow run](https://github.com/lazysnail1024/zed-argocd/actions/workflows/native.yml), inside the matching `argocd-native-<platform>` artifact under `dev-bundle/`. Only runs containing that directory provide the new bundle; older native-only artifacts do not. GitHub may require sign-in for workflow artifacts. Use the ZIP for your operating system and CPU architecture. The native language server is already compiled; no Go or C compiler and no `serverPath` setting are required.

1. Unzip the bundle into a permanent directory.
2. Install Rust through **rustup** and make it available to Zed. The bundle pins Rust **1.99.1** and `wasm32-wasip2`; initial toolchain and locked Cargo dependency downloads require network access.
3. Open Zed's Extensions page, choose **Install Dev Extension** (or run `zed: install dev extension`), and select the extracted `zed-argocd-dev-<platform>` folder containing `extension.toml`.
4. Keep that folder in place and open a YAML project. Zed compiles the small Rust adapter automatically, then the adapter installs and verifies its embedded native server in Zed's extension work directory.

Keep Zed's YAML language support enabled. Remove any old `lsp.argocd-ls.settings.serverPath` override to use the bundle: an explicit override takes priority and an invalid override fails startup. Choose the bundle matching your operating system and CPU architecture. The native server needs no runtime downloads.

To upgrade, obtain a newer verified bundle for the same platform, extract it into a
new directory, and select that directory with **Install Dev Extension**. Restart
Zed, then check the server identity and a YAML diagnostic. Keep the previous
verified ZIP and extracted directory: to roll back, select the previous directory
with the same command and restart. The adapter checks cached native bytes against
the selected bundle and restores its exact payload. Remove obsolete `serverPath`
overrides when using bundled delivery; an invalid override intentionally blocks
startup instead of falling back. This procedure still needs real Zed acceptance
on each claimed consumer platform.

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

- If startup fails, run the bundled native server or your configured override executable with `version --json` and check its identity, readiness, execution permissions, and absolute path.
- For extension compilation or startup errors, run `zed: open log`. Check that rustup and the pinned toolchain are available to Zed. [Zed documents additional logging](https://zed.dev/docs/extensions/developing-extensions).
- If no Argo features appear, confirm the file is in YAML mode and the `argocd-ls` server has not been disabled in language settings. Keep the general YAML server enabled as well.
- If configuration is rejected, use `explain-config` below and remove unsupported target overrides. Existing YAML schema associations can cause duplicate diagnostics; this extension does not modify them.

## CLI

Run from the extracted bundle folder, replacing `/path/to/project` with your YAML project:

```sh
./native/server.bin explain-config --root /path/to/project
./native/server.bin check --root /path/to/project --target-version 3.0.0 --format json
./native/server.bin check --help
```

On Windows, copy `native/server.bin` to a path ending in `.exe` before invoking it directly.

Checks exit **0** when complete without errors, **1** for diagnostic errors (also warnings with `--warnings-as-errors`), **2** for configuration/input/analysis incompleteness, and **3** for cancellation or deadline. `analysisComplete` describes the particular check; `analysisReady` describes the server's local editing contract.

## Limits

Analysis is local and static. Controller behavior, generator/template execution, dynamic or controller-specific patch effects, hydration, value-file expansion, and complex policy/RBAC evaluation are outside its scope. Ambiguous or derived YAML locations do not receive guessed edits. Analysis has a five-second budget, fixed text budgets, and bounded alias expansion; refused input is incomplete rather than silently accepted.

Development installation is the available delivery mode. Native CLI/LSP CI covers Linux amd64/arm64, macOS amd64/arm64, and Windows amd64; this does not establish real Zed GUI acceptance on all platforms or minimum supported OS versions. Signed release artifacts and registry delivery remain unavailable. Contributor builds, packaging, tests, and provenance are in [Development](DEVELOPMENT.md).

## License and acknowledgments

Original code and documentation are [MIT licensed](LICENSE). Third-party licenses and copyright notices remain applicable.

Thanks to [k8s-crd-toolkit](https://github.com/lazysnail1024/k8s-crd-toolkit) (MIT), [zed_extension_api](https://github.com/zed-industries/zed/tree/main/crates/extension_api) (Apache-2.0), [serde](https://github.com/serde-rs/serde) and [serde_json](https://github.com/serde-rs/json) (MIT OR Apache-2.0), [sigs.k8s.io/yaml](https://github.com/kubernetes-sigs/yaml) (see its vendored license), [yaml.v3](https://github.com/go-yaml/yaml) (MIT/Apache-2.0), [glsp](https://github.com/tliron/glsp) and [jsonschema/v6](https://github.com/santhosh-tekuri/jsonschema) (Apache-2.0). Exact dependencies are recorded in `go.mod`, `go.sum`, and `Cargo.lock`.

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
Files containing Helm expressions show `HELM_UNRENDERED` in YAML mode. They need
an explicit render before final resource checks. Ordinary typing never runs Helm.
The extension currently registers only YAML; Helm language mode integration has
not been accepted in real Zed.

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
