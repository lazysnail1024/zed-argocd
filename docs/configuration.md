# Configuration and version support

The [coverage and verification notes](multi-version-support.md) list the exact
370-version inventory and the limits of static editing support.

## No file is required

The default target is `auto`. The bundled catalog contains the stable releases of
Argo CD in **2.0.0–3.6.1**. Schema data comes from pinned upstream release commits,
is deduplicated by content, and ships inside the executable. Opening a file does
not download schemas or need cluster credentials.

A document must satisfy a complete supported schema, preserving relationships
between fields. Combining fields that never coexisted in a supported release is
not a valid compatibility strategy. Completion and hover expose the supported
field space. Local static semantic checks continue to run.

At catalog collection time, the compatibility window is the newest stable major
and the previous major, starting at the previous major's first release. It is refreshed when the server's
catalog is regenerated and published. An installed binary cannot know about
upstream releases published after its catalog was built.

## Optional strict target

For a known cluster, request an exact version with the CLI:

```sh
argocd-ls check --root . --target-version 3.6.1 --format json
```

Or use `.argocd-ls.yaml`:

```yaml
schemaVersion: 1
targetVersion: "3.6.1"
referencePolicy: open
```

Removing `targetVersion` restores `auto`. Explicit unsupported versions fail;
they never silently select another schema. Existing scope and reference-policy
configuration remains available. Editor `settings.targetVersion`, if configured,
keeps its existing precedence over the file. The CLI does not read Zed settings.

`referencePolicy: open` treats unavailable external resources as unresolved local
information. `closed` is useful only when the project scope includes the intended
referenced resources. The server does not infer Kubernetes topology.

## Boundaries

Schema compatibility and static editing are not controller execution, server-side
admission, dynamic expression evaluation, or a claim of official CLI equivalence.
An ambiguous source location does not receive a guessed edit. Input, index and
analysis-budget failures remain visible as incomplete analysis.

Use `argocd-ls explain-config --root .` for the effective configuration,
`argocd-ls version --json` for the bundled catalog and release identity, and
`argocd-ls check --help` for CLI options. See the catalog lock and schema generation
tools for the exact upstream sources and hashes. The checked-in catalog's cutoff
and inventory are in [version support](multi-version-support.md); current file
indexing, completeness and edit boundaries are in the [capability table](../README.md#capabilities-in-the-current-source).
