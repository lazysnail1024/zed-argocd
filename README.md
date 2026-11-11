# Argo CD language server

Local YAML diagnostics, completion, hover, navigation and safe edits for Argo CD.
The Go language server works independently; the Zed extension is a thin adapter.

**Alpha `0.0.1-alpha.4`:** default support covers **2.0.0–3.6.1**, including stable patch releases.
No project configuration or target version is needed. Current-file diagnostics
remain available when a large, multi-repository workspace scan is incomplete.

## Install in Zed

Close Zed, then run one command:

Linux / macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/lazysnail1024/zed-argocd/main/editors/zed/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/lazysnail1024/zed-argocd/main/editors/zed/install.ps1 | iex
```

Restart Zed and open a project containing Argo CD YAML. **Run the same command to
upgrade.** The installer verifies the release ZIP against `SHA256SUMS`, preserves
one previous installation for rollback, and leaves Zed's native-server cache alone.
If you prefer to inspect a script before running it, download it from
[`editors/zed/`](editors/zed/) first.

This precompiled package needs **no Rust or Go installation**. The native download
supports Linux amd64/arm64, macOS amd64/arm64 and Windows amd64. This is a manual
alpha installation; the extension is not yet in the Zed extension store.

If using a native server installed separately via `serverPath` or PATH, update it
independently. [Rollback, manual/offline installation and troubleshooting](docs/distribution.md).

## Try it

Save this as `example.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: example
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    path: guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: guestbook
```

Hover a field or request completion inside `spec`. Change
`project: default` to
`project: 123` to check diagnostics, then restore it.

The default compatibility mode accepts content supported by a version in the
window. It does not guess your cluster version. An existing explicit `targetVersion`
still requests strict checking; remove that key to restore automatic compatibility.
[Version behavior and advanced configuration](docs/configuration.md).

## Capabilities in the current source

The table describes the checked-out implementation. The fixed alpha download
above does not automatically acquire changes merged into the source repository.

| Capability | Supported behavior and boundary |
| --- | --- |
| Identify | Recognizes Argo CD `Application`, `AppProject` and `ApplicationSet`, including effective YAML alias/merge values; unrelated CRDs stay outside Argo CD rules. |
| Understand | Embedded CRD schemas, automatic or exact release selection, field/enum completion, hover and static domain constraints. Dynamic ApplicationSet expressions remain unknown where they cannot be evaluated safely. |
| Relate | Resolves configured-scope AppProject bindings, Application multi-source references and supported static ApplicationSet variants. Namespace, workspace ownership and exclusion rules remain explicit. |
| Navigate | Definitions, references, document symbols and bounded workspace symbol search. Available symbols can appear during discovery; a partial result does not prove that a missing resource is absent. |
| Modify | Versioned field/enum fixes, Project replacement and configuration actions; private source-ref rename updates its Helm uses. Cross-file AppProject rename requires a complete closed scope, an explicit control-plane namespace, unique exact bindings and all affected files open at accepted versions. |
| Report | Current-buffer diagnostics are independent of background discovery. Source revisions and dependency proofs reject stale answers and edits. |
| Scale | Persistent compact facts, prioritized file work and bounded foreground workers. Four light reads and one heavy operation may run at once; discovery, input or result limits remain visible rather than being treated as complete analysis. |

The toolkit source dependency supplies text snapshots, YAML values and origins,
schema queries, bounded indexing and workspace primitives. Argo CD resource
semantics, configuration, scheduling and LSP delivery remain in this server.
[Modules, limits and the shared boundary](docs/architecture.md).

## Standalone CLI / other editors

Download your platform archive from the [LSP alpha release](https://github.com/lazysnail1024/zed-argocd/releases/tag/v0.0.1-alpha.4),
extract it, and run:

```sh
argocd-ls check --root . --format json
argocd-ls serve --stdio
```

Analysis is local: it does not access a cluster, execute controllers or render Helm
implicitly. The alpha includes automated CLI/LSP and package checks; real Zed GUI
acceptance is recorded separately. [Documentation](docs/README.md) ·
[Development and tests](DEVELOPMENT.md) · [MIT license](LICENSE)
