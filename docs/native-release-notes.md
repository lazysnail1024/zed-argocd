# Argo CD language server — initial alpha

This is a prerelease before `0.0.1`.

- Default zero-configuration compatibility window: **2.0.0–3.6.1**.
- Independent native Go language server and CLI; standard `serve --stdio` interface.
- Native artifacts for Linux amd64/arm64, macOS amd64/arm64, and Windows amd64.
- SHA256SUMS, original dependency/schema/runtime legal texts, and exact source provenance.

Choose the archive for your platform and retain its legal notices. Raw executable
assets are provided for the Zed adapter's verified downloader. The precompiled
Zed adapter has a separate `zed-v0.0.1-alpha.1` release and its own version lifecycle.

This alpha does local static analysis. It does not execute Argo controllers or
infer a deployment's runtime version. Automated native CLI/LSP and package tests
are separate from real Zed GUI acceptance; no Zed store publication is claimed.
