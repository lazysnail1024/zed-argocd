# Architecture ablation and acceptance

The initial alpha removes obsolete coupling after exercising the actual native
program, Rust adapter and release archives. These checks are reproducible from
the repository; CI retains successful and failed evidence as workflow artifacts.

## Deletions retained

| Removal | Observable result | Decision |
| --- | --- | --- |
| Every editor file from a clean native source fixture | Production native build, release staging and extracted executable identity succeed | Keep the LSP independent of editors |
| Go sources and native release tools from an adapter fixture | Rust installer tests and portable WASM build succeed | Keep the Zed crate self-contained |
| Native version derived from the adapter's Cargo version | Adapter version can change to `9.9.9-alpha.1` while native pin stays `0.0.1-alpha.1` | Keep the separate native pin |
| Combined candidate/development-bundle pipeline | Native and Zed production packagers independently create verified runtime archives | Delete the superseded candidate and mixed-bundle scripts |
| Old partial catalog collector | The current collector discovers and verifies the complete release/tag inventory | Delete `tools/collect_remaining.py` |
| Repeated deep AppSet scenarios for identical patch contracts | 180 eligible versions map to 17 distinct schema plus complete local-rule groups; all 198 grouped scenarios pass, while the independent 370-version matrix remains exhaustive | Test each deep contract once, with a separate auto control |

No editor registry, generic version provider or placeholder VS Code/Neovim
implementation was added. The stdio protocol and native identity are the boundary.

## Removals rejected by behavior

| Isolated mutation | Observed regression | Decision |
| --- | --- | --- |
| Keep only the newest schema | Valid historical `ksonnet` content loses schema support | Retain whole-release alternatives |
| Remove query branch selection | Eight LSP controls detect suggestions incompatible with existing fields, while CLI validation still succeeds | Retain query selection using whole release branches |
| Open every structured object | Wrong types and impossible old/new field combinations are accepted by unrelated branches | Retain structural closure, preserving upstream open maps |
| Inline all shared schema definitions | Automatic JSON grows from 854,610 to 8,991,596 bytes; the three-kind CLI probe exceeds 30 seconds | Retain build-time schema sharing |
| Restore Cargo-version-to-native-version coupling | The independent-pin installer assertions fail | Retain explicit `native-server-version` |
| Remove compiled WASM release identity checking | A stale native pin can be packaged under a new manifest | Retain the small identity section and package guard |
| Remove original dependency notices | The unpacked archive fails the independent license inventory check | Retain original legal texts |

On the local Linux host, the unmodified three-kind CLI probe's five-run median
was 300.95 ms in the initial run and 179.65 ms in the query follow-up. These are
separate host observations, not evidence of a speed improvement or a portable performance
promise. The inline mutant passed the narrower Application probe; its timed-out
three-kind run does not prove semantic parity for every resource.

## Reproduce

```sh
python3 tests/native_isolation.py --output artifacts/native-isolation
python3 tests/ablate_multi_version.py /path/to/argocd-ls artifacts/version-ablation
cd editors/zed
python3 tests/ablate_distribution.py --output ../../artifacts/zed-ablation
```

Before publication, local native release staging passed 23 checks. Each adapter
package suite passed 28 cases and its isolation/ablation suite passed five
controls. The native matrix and adapter workflows repeat the relevant checks
against the exact release commit. Local isolated-source fixtures have synthetic
commits and are not represented as published production builds. Real Zed GUI
installation is a separate manual check.

The first full local native run passed 30 of 31 gates; redundant per-patch AppSet
execution exceeded its unchanged 480-second budget. The grouped replacement
passes all 198 scenarios in about 52 seconds and records every covered version.
All eight existing mutation controls passed in that full native run. The timeout
report is retained rather than relabeled as a successful run.
