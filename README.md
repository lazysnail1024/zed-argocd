# Argo CD language server

Local YAML diagnostics, completion, hover, navigation and safe edits for Argo CD.
The Go language server works independently; the Zed extension is a thin adapter.

**Alpha `0.0.1-alpha.2`:** default support covers **2.0.0–3.6.1**, including the stable patch releases
in that window. No project configuration or target version is needed.

## Install in Zed

1. Download **[zed-argocd-0.0.1-alpha.2.zip](https://github.com/lazysnail1024/zed-argocd/releases/download/zed-v0.0.1-alpha.2/zed-argocd-0.0.1-alpha.2.zip)**
   from the [Zed alpha release](https://github.com/lazysnail1024/zed-argocd/releases/tag/zed-v0.0.1-alpha.2).
2. Close Zed, extract the ZIP, and copy its **`argocd-lsp`** folder into:

   | OS | Installation directory |
   | --- | --- |
   | Linux | `$XDG_DATA_HOME/zed/extensions/installed/` (default `~/.local/share/zed/extensions/installed/`) |
   | macOS | `~/Library/Application Support/Zed/extensions/installed/` |
   | Windows | `%LOCALAPPDATA%\Zed\extensions\installed\` |

3. Start Zed and open a project folder containing Argo CD YAML. The adapter downloads
   the matching native server and checks its SHA-256 and identity. Subsequent starts
   work offline. Keep Zed's general YAML support enabled.

This precompiled package needs **no Rust or Go installation**. The native download
supports Linux amd64/arm64, macOS amd64/arm64 and Windows amd64. This is a manual
alpha installation; the extension is not yet in the Zed extension store.

For an upgrade, keep a copy of the previous extension folder, close Zed, and replace
it with the new release's folder. Also update a native server installed separately
through `serverPath` or PATH. [Checksums, offline setup and troubleshooting](docs/distribution.md).

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

## Standalone CLI / other editors

Download your platform archive from the [LSP alpha release](https://github.com/lazysnail1024/zed-argocd/releases/tag/v0.0.1-alpha.2),
extract it, and run:

```sh
argocd-ls check --root . --format json
argocd-ls serve --stdio
```

Analysis is local: it does not access a cluster, execute controllers or render Helm
implicitly. The alpha includes automated CLI/LSP and package checks; real Zed GUI
acceptance is recorded separately. [Development and tests](DEVELOPMENT.md) ·
[Architecture](docs/architecture.md) · [MIT license](LICENSE)
