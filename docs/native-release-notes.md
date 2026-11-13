# Argo CD language server — 0.0.1-alpha.5

This prerelease reduces configuration for local navigation and adds explicit Helm rendering from the editor.

- Project definitions return matching local namespace candidates without a control-plane namespace setting. Explicit namespace pins continue filtering candidates; policy, reverse references and edits still require authoritative bindings.
- Physical ApplicationSet generator templates support project and `$values` definitions at their original source locations.
- Helm sources support conservative navigation of static project and source-reference fields. Dynamic scalar values remain unknown; structural directives, includes and dynamic mapping keys are refused for navigation. Plain YAML in charts retains normal analysis, and foreign resources do not receive Argo CD Helm notices.
- The explicit **Render chart and check Argo CD (saved files)** action discovers the nearest chart, uses Helm from PATH and default chart values, and opens generated YAML. It refuses unsaved chart/configuration buffers and derives the configured target scope from the selected source file. Helm 3 and 4 are supported. A report is saved beside generated YAML; rendering never runs on save or during action collection.
- Current main's audited workspace, test-effectiveness and toolkit updates are retained. The compatibility window remains **2.0.0–3.6.1**; no cluster version is inferred.

Native archives target Linux amd64/arm64, macOS amd64/arm64 and Windows amd64, with build/source records, original legal notices and `SHA256SUMS`. The forthcoming independently versioned `zed-v0.0.1-alpha.5` adapter will pin native `v0.0.1-alpha.5`.

Native, editor and distribution evidence remain separate acceptance boundaries. This prerelease does not claim publication to the Zed extension store. Previous prerelease tags and downloads remain unchanged.
