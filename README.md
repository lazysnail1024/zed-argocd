# Argo CD language server

Local YAML diagnostics, completion, hover, navigation and safe edits for Argo CD.
The Go language server works independently; the Zed extension is a thin adapter.

**Alpha `0.0.1-alpha.6`:** default support covers **2.0.0–3.6.1**, including stable patch releases.
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
| Navigate | Project definitions return local namespace-labelled candidates without configuration; an explicit control-plane namespace still filters candidates. Definitions include physical generator list templates and their `$values` uses. References and policy require authoritative bindings. Document symbols and bounded workspace symbol search. Available symbols can appear during discovery; a partial result does not prove that a missing resource is absent. |
| Modify | Versioned field/enum fixes, Project replacement and configuration actions; private source-ref rename updates its Helm uses. Cross-file AppProject rename requires a complete closed scope, an explicit control-plane namespace, unique exact bindings and all affected files open at accepted versions. |
| Report | Current-buffer diagnostics are independent of background discovery. Source revisions and dependency proofs reject stale answers and edits. |
| Scale | Persistent compact facts, prioritized file work and bounded foreground workers. Four light reads and one heavy operation may run at once; discovery, input or result limits remain visible rather than being treated as complete analysis. |

The toolkit source dependency supplies text snapshots, YAML values and origins,
schema queries, bounded indexing and workspace primitives. Argo CD resource
semantics, configuration, scheduling and LSP delivery remain in this server.
[Module architecture and dependency ownership](docs/architecture.md) ·
[Shared toolkit API contract](contracts/toolkit-design-6.md).

## Standalone CLI / other editors

Download your platform archive from the [LSP alpha release](https://github.com/lazysnail1024/zed-argocd/releases/tag/v0.0.1-alpha.6),
extract it, and run:

```sh
argocd-ls check --root . --format json
argocd-ls serve --stdio
```

## Helm sources and explicit rendering

Plain YAML inside a chart receives normal analysis. For Helm sources, literal
project and source-reference fields can navigate when the surrounding YAML
structure is static. Dynamic scalar values remain unknown. Structural template
control flow, includes, dynamic keys and unsafe source projections return no
navigation results; source navigation never claims rendered validation or offers
semantic edits. Escaped ApplicationSet tags can coexist with static neighbors.

In Zed, open the command palette, choose **editor: toggle code actions**, then
**Render chart and check Argo CD (saved files)** on an owned chart document. It uses Helm from `PATH`, finds the nearest ancestor
`Chart.yaml`, renders with the chart's default values, and opens generated YAML.
Save all open chart/configuration buffers first. The action runs only when
selected; collecting actions, opening a file and saving never render a chart.
The result message includes a durable `report.json` path beside generated YAML.

For Helm language mode, install Zed's Helm extension and add a one-time global
language configuration. Use globs restricted to your actual chart paths:

```json
{
  "file_types": { "Helm": ["**/charts/my-chart/templates/**/*.yaml"] },
  "languages": {
    "Helm": { "language_servers": ["helm", "argocd-ls", "..."] }
  }
}
```

Adapt the chart name/path to your repository. Avoid a generic `**/templates/**`
association, which also matches ordinary resource directories. If both Argo
extensions are installed, add `"argo-workflows-ls"` to that server list. No
`.zed/tasks.json` is required. Opening a common parent directory lets the server
index multiple Git repositories beneath it. The server indexes the roots supplied
by the editor; worktrees hosted by separate server processes keep separate indexes.

The same operation is available from a chart directory, or for a selected file:

```sh
argocd-ls render-check --format json
argocd-ls render-check --root . --file chart/templates/app.yaml --format json
```

Helm 3 and 4 are accepted. `--chart`, `--helm-binary`, `--values`, `--output`,
`--release-name`, `--namespace`, `--kube-version` and `--api-version` remain explicit
overrides. Automatic output uses a fresh temporary directory; preserve it if you
need a lasting report. Rendered diagnostic locations refer to generated YAML.

Analysis is local: it does not access a cluster, execute controllers or render Helm
implicitly. The alpha includes automated CLI/LSP and package checks; real Zed GUI
acceptance is recorded separately. [Documentation](docs/README.md) ·
[Development](DEVELOPMENT.md) · [Test contracts](tests/README.md) ·
[Lessons learned](docs/lessons-learned.md) · [MIT license](LICENSE)
