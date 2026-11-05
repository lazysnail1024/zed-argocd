# Argo CD language server — 0.0.1-alpha.2

This prerelease before `0.0.1` corrects historical ApplicationSet schema coverage.

Argo CD 2.3.x's optional stable ApplicationSet addon manifests reference a
standalone CRD. The catalog now includes those historical CRDs with
their pinned upstream sources and
[provenance](https://github.com/lazysnail1024/zed-argocd/blob/v0.0.1-alpha.2/compat/catalog.lock.json).
Automatic compatibility accepts
legacy fields such as `ksonnet` in ApplicationSet templates, correcting the false
unknown-field diagnostics caused by the alpha.1 omission. No project configuration
or target version is needed.

Exact targets without an upstream ApplicationSet CRD or a fixed installation
reference do not receive an assumed standalone-project schema.

- Default zero-configuration compatibility window: **2.0.0–3.6.1**.
- Independent native Go language server and CLI; standard `serve --stdio` interface.
- Native artifacts for Linux amd64/arm64, macOS amd64/arm64, and Windows amd64.
- SHA256SUMS, original dependency/schema/runtime legal texts, and exact source provenance.

Choose the archive for your platform and retain its legal notices. Raw executable
assets are provided for the Zed adapter's verified downloader. The precompiled
Zed adapter has a separate `zed-v0.0.1-alpha.2` release and its own version lifecycle.
That adapter pins the corrected native `v0.0.1-alpha.2` release. Update any native
executable supplied separately through `serverPath` or PATH when upgrading.

Published `v0.0.1-alpha.1` and `zed-v0.0.1-alpha.1` tags, releases and assets are
immutable and remain unchanged. This correction is distributed as new alpha.2
artifacts; it does not replace an existing download.

This alpha does local static analysis. It does not execute Argo controllers or
infer a deployment's runtime version. Automated native CLI/LSP and package tests
are separate from real Zed GUI acceptance; no Zed store publication is claimed.
