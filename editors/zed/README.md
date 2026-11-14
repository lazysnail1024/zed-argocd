# Argo CD adapter for Zed

Download the precompiled adapter and native server from the repository's
[Releases](https://github.com/lazysnail1024/zed-argocd/releases). See the
[installation guide](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md)
for the quickest setup.

This directory is a standalone Zed extension. It discovers or downloads the
`argocd-ls` executable and starts `serve --stdio`. YAML analysis lives in the
Go language server outside this directory. The adapter contains no native server.

## Server selection

The adapter uses an optional `lsp.argocd-ls.settings.serverPath`, then PATH,
then the exact release in `native-server-version`. Downloads are verified against
`SHA256SUMS` and the server identity before activation. A verified cached server
works offline. The adapter's own version can advance independently of this pin.
The extension ID is `argocd-lsp`; the language-server/settings ID is `argocd-ls`.
YAML and Helm language modes map to `yaml` and `helm` respectively.
See [configuration](../../docs/configuration.md#zed-and-other-lsp-clients) for
target overrides and [distribution](../../docs/distribution.md) for upgrades,
rollback and offline use.

## Develop this adapter

Run these commands from this directory with Rust installed through rustup:

```sh
cargo test --locked
cargo build --locked --release --target wasm32-wasip2
```

For source development, select this directory in Zed's **Install Dev Extension**
action. Use the release installation guide when using a precompiled adapter.

Cargo and package tests do not constitute a real Zed GUI acceptance run. Use the
[host procedure](tests/HOST_ACCEPTANCE.md) when validating loaded bytes, editor
actions, restart and process cleanup.
