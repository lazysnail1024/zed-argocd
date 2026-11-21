# Argo CD Zed adapter - 0.0.1-alpha.7

This adapter pins native `v0.0.1-alpha.7` (built with `k8s-crd-toolkit` `v0.0.1-alpha.2`), adding analysis-status and compatibility explanations, explicit render profiles, unbound `AppProject` candidate inspection, and lower LSP memory/startup overhead while retaining automatic compatibility for Argo CD **2.0.0-3.6.1**.

Project navigation, static Helm-source definitions and explicit chart rendering remain available. Native analysis keeps its existing deadlines, memory limits and edit-authority checks.

Install or upgrade using the [README instructions](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md), or download `zed-argocd-0.0.1-alpha.7.zip` with `SHA256SUMS`. The adapter verifies its pinned native server; explicit `serverPath` or PATH overrides must be updated separately.

The ZIP contains precompiled WASM, snippets and original dependency notices. `zed-adapter.json` records source identity. Native CI, real-editor acceptance and extension-store publication remain separate claims.
