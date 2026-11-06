# Argo CD language server — 0.0.1-alpha.3

This prerelease fixes open-file diagnostics in large workspaces, including one
Zed window with several Git repositories under a shared parent directory.

- Open Argo CD YAML buffers are analyzed first, independently of the full
  workspace scan. Schema errors, hover and field completion can arrive even when
  a sibling repository contains an oversized or unparseable YAML file.
- A failed or timed-out workspace scan reports incomplete analysis without
  clearing diagnostics already validated against current open-buffer contents.
- Cross-file navigation and edits still require a sufficiently complete index;
  nested Git repositories remain in the declared workspace scope.
- Analysis logs can distinguish document and workspace phases. See
  [diagnostics and limitations](https://github.com/lazysnail1024/zed-argocd/blob/main/docs/workspace-diagnostics.md).

The zero-configuration Argo CD compatibility window remains **2.0.0–3.6.1**.
This includes the historical ApplicationSet CRD correction from alpha.2.

Native binaries and archives cover Linux amd64/arm64, macOS amd64/arm64 and
Windows amd64. Each artifact is supplied with verification records and
`SHA256SUMS`. The separately versioned Zed adapter
`zed-v0.0.1-alpha.3` pins this native release.

Previous alpha releases and downloads are unchanged. The tests cover native
CLI/LSP, real stdio protocol behavior, six-repository workspaces, version
compatibility and package distribution. They do **not** constitute real Zed
GUI acceptance or publication to the Zed extension store.
