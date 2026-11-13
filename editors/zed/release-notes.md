# Argo CD Zed adapter — 0.0.1-alpha.5

This adapter pins native `v0.0.1-alpha.5`, retaining automatic compatibility for Argo CD **2.0.0–3.6.1**.

Project navigation needs no namespace configuration; ambiguous local matches remain candidates. Static Helm-source fields and generator-local ApplicationSet templates gain precise definitions. The explicit **Render chart and check Argo CD (saved files)** action uses Helm 3 or 4 on PATH, discovers the nearest chart and selected scope, refuses unsaved source buffers, and opens generated YAML with a saved report. No task file or render-on-save behavior is added.

Install or upgrade using the [README instructions](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md), or download `zed-argocd-0.0.1-alpha.5.zip` with `SHA256SUMS`. The adapter verifies its pinned native server; explicit `serverPath` or PATH overrides must be updated separately.

The ZIP contains precompiled WASM, snippets and original dependency notices. `zed-adapter.json` records source identity. Native CI, real-editor acceptance and extension-store publication remain separate claims.
