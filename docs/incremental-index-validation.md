# Incremental workspace validation

These are observations from real Linux native CLI/stdio processes built with
Go 1.27.3. They are individual development runs, not formal cold-start/p95
benchmarks or Zed GUI acceptance. Executable and consumer identities are retained
in the experiment reports. Native CI repeats the required consumers against the
submitted source.

## Source dependency and scope

The shared toolkit is consumed from source commit
`79c46c0625b94d61156e40994ac603db69e2438f`, Go pseudo-version
`v0.0.0-20261110063134-79c46c0625b9`, with checked-in vendor sources and the
byte-identical `toolkit-design-2` contract. It requires no release tag, sibling
checkout or local replacement. This change does not change the native VERSION,
Zed adapter version or adapter download pin.

The persistent session replaces the repeated workspace acquisition path used by
the LSP. The CLI retains its explicit one-shot contract. Resource identity,
AppProject policy and ApplicationSet semantics remain in this repository.

## Preserved behavior and reduced work

The same mixed-parent protocol consumer passed eleven checks against both original
main (`5ee4c0b4a0025c631001e83c978d80e06bb95f7b`) and the refactored small-workspace
candidate. Those checks preserve ownership, definitions, buffer authority,
watch/close behavior, changed locations, delete/recreate behavior and clean exit;
they are not claimed as failures of the original server.

A separate 80-file negative-control run of the persistent-workspace consumer
allowed the legacy server's completion signal so that it could reach the actual
work assertions. The original server reread all 80 files after an unchanged save
and after one watched-file change. The persistent candidate performed zero reads
and parses for the unchanged save, and one read/parse for the changed file.

The 50,000-file mixed-parent consumer adds an important partial-index assertion:
a local schema field correction can be created and resolved while discovery
continues, but resolving that same action after the buffer changes returns
ContentModified. In the recorded candidate run all fourteen assertions passed;
the first diagnostic arrived in 0.173 s and discovery advanced from 495 to 751
known files during the quick-fix interaction, without claiming Ready.

```sh
python tests/mixed_parent_index_e2e.py build/argocd-ls \
  artifacts/mixed-parent-50000 --ordinary-files 50000 \
  --padding-bytes 4096 --background-edits
python tests/persistent_workspace_e2e.py build/argocd-ls \
  artifacts/persistent-50000 --count 50000 --padding 4096
```

## Full discovery and retention

The persistent consumer requires terminal Ready, complete membership and an empty
queue. It also checks unchanged saves, exact watched-file work, a same-size and
same-mtime missed event, a 640-file burst larger than the 512-entry queue, and clean
shutdown. The default generated 50,000-file fixture is mostly foreign ConfigMaps;
it proves cumulative input retention and discovery, not dense Argo CD capacity.

One recorded run against executable
`db8f676bb37bb74c2f98bc13f003a4d7152b04a55ac0b021573e5e0cf9725491`
passed all seven checks:

| Observation | Result |
| --- | ---: |
| Cumulative YAML input | 204,800,000 bytes (195.31 MiB) |
| First effective current-file diagnostic | 0.170 s |
| Complete Ready | 17.180 s |
| Unchanged save | 0 disk reads, 0 parses |
| Single watched-file change | 1 disk read, 1 parse |
| Same-size/same-mtime missed-event recovery | 18.890 s |
| Queue peak during 640 new targets | 512 |
| Kernel peak RSS | 239,180 KiB (233.57 MiB) |

A distinct 10,000-owned-Application fixture with 20 KiB padding also passed all
seven checks: 195.31 MiB cumulative input, 0.210 s first local diagnostic, 19.37 s
Ready, 189.84 MiB peak RSS, and the same zero/one file-work results. It guards
against an improvement that would merely skip foreign YAML. Summary retention
remains bounded; arbitrary numbers of dense owned resources are not promised
to fit.

The candidate executable
`060d0c8e12c5f1b7a3959d8b8bcaacd6b3201601fcdaddcebbf0c7adff6e0cb4`
also passed the seven semantic checks on the reference parent-workspace mix:
45,000 ConfigMaps, 4,500 Applications, one AppProject and 499 Workflows, plus
100,000 ordinary non-YAML files. YAML input was again 195.31 MiB. First local
diagnostics arrived in 0.166 s; terminal complete Ready arrived in 38.989 s;
kernel peak RSS was 285,668 KiB (278.97 MiB). The retained summary charge was
61,947,549 bytes at Ready, below the unchanged 64 MiB limit. Missed-event recovery
took 27.589 s, and all 640 burst candidates were retained with queue peak 512.

The 30-second reconciliation request arrived during initial discovery and was
retained. The 38.989 s terminal observation includes two passes (100,000 disk
reads but 50,001 parses); it must not be described as a single-pass scan time.
The fixture initially ran as an isolated variation with unchanged assertions.
Its generation is now available through the regular consumer's options, and the
Linux CI gate additionally requires an actual kernel RSS sample below 512 MiB:

```sh
python tests/persistent_workspace_e2e.py build/argocd-ls \
  artifacts/reference-parent-50000 --count 50000 --padding 4096 \
  --reference-mix --ordinary-files 100000 --max-rss-mib 512 \
  --ready-timeout 120 --reconcile-timeout 120
```

The regular native pipeline subsequently exposed a path-length boundary: under
its longer artifact directory, the same reference fixture reached 49,401 known
files and 67,018,885 charged bytes, reported Limited and failed the Ready check.
Its observed RSS was still 287,436 KiB. Full URI/path strings are intentionally
charged, so the implementation raises the logical summary/index allowance from
64 to 96 MiB. It keeps the long test path, conservative accounting, file/queue
limits, complete-Ready assertion and independent 512 MiB kernel-RSS gate. This
is a capacity correction based on a failing workload; it is not removal of that
failure from the test or a claim that the old allowance always fit 50,000 files.

The rerun used an even longer output path and executable
`5e2691ce14bc38fb26b3c20e50a7169ea46e76a0bb766e6b34372147410bb815`.
All eight checks, including the kernel-RSS gate, passed. Initial complete Ready
arrived in 25.591 s with 69,923,283 charged bytes, the first local diagnostic in
0.172 s, and peak RSS was 302,688 KiB (295.59 MiB). Save remained 0 reads/0 parses,
one watch remained 1 read/1 parse, missed-event recovery took 16.858 s, and all
640 burst targets were present. This initial Ready preceded the periodic timer,
so it completed one discovery pass; its timing is not a controlled speedup
comparison with the earlier two-pass observation.

## ApplicationSet expansion admission

Dropping raw text from a final summary is insufficient if effective ApplicationSet
templates have already been copied hundreds of times. A real CLI counterexample
used a 222,163-byte ApplicationSet with 512 top-level list generators and 8,000
`valuesObject` keys. Before admission, the process reached 397,984 KiB VmHWM in
1.582 s and was stopped by the experiment's 384 MiB guard. That is a lower bound
on the unfixed peak, not a completed baseline measurement.

The same YAML bytes after the fix returned `INPUT_BUDGET_EXCEEDED`, exit status 2
and `analysisComplete: false` in 0.295 s, with 39,876 KiB observed VmHWM. A separate
run with local repository mapping enabled exercised the earlier reference-extraction
entry point: 0.229 s and 16,912 KiB, with the same explicit refusal. A 16-generator,
2,000-key control still completed analysis; its expected unresolved-project
diagnostics were preserved.

The fix admits work before constructing variants and accounts for:

- Expanded logical nodes and copied strings across ordinary YAML documents.
- Reuse of the same immutable source identity without double charging; different
  revisions remain different identities.
- Raw alias projection and ApplicationSet expansion without changing exact source
  provenance merely to make accounting work.
- Static templatePatch alias expansion by traversing the YAML syntax graph before
  decoding expanded values.
- Repeated long path strings needed by unknown fields and override origins.

Admission failure leaves independent documents usable and the affected analysis
incomplete. Tests include affordable controls, aggregate node/string refusal,
atomic reuse refusal, context cancellation, alias amplification and long-path
controls. The limits are logical work/string limits, not a universal process-RSS
guarantee.

The CLI after-fix executable SHA-256 was
`ddf90f45281ef85c9bfb6aa098e5f6a66b82df93128e5f7cffa6047a33f3bc11`.
The expanded fixture SHA-256 was
`f5f4035673f631f78fdc556dbdcc30daf8e0101f2d38d079c6fd4a2c78a2807a`.

## Publication and retained safeguards

`publication_delivery_e2e.py` uses Linux file-open observation to edit unrelated
root B while a background worker reads a large file in root A. The original
candidate committed A's updated facts but discarded its publication as stale,
leaving A's new diagnostic absent even after Ready. The fixed candidate requeues
the accepted URI for domain calculation/publication without another disk read.
The same consumer detects the broken candidate and accepts the repair; unrelated
root B's latest diagnostics, one disk read and clean shutdown remain positive
controls.

A separate close/reopen counterexample checked the client's final visible list,
not just whether an error had appeared at some point. The old candidate delivered
a workspace `CD_PROJECT_UNRESOLVED` diagnostic and then cleared it with a late
Schema-only empty publication for the same revision. The fix suppresses that
weaker result using the accepted revision and target/configuration identity
already associated with the delivered workspace result. The proof stores no
snapshot or summary. Same-content acceptance also advances the registry revision
without reparsing, so a reset editor version cannot leave that proof stuck on the
old input identity.

The enhanced publication consumer passed 18/18 cases on candidate
`41eba85b0b17b5f8edc41d0769d533a41051afba469592a4e54479ad69a83227`;
the old candidate failed its new final-domain-list assertion. Separate incomplete
input controls still require early local restoration after close/reopen, so
precedence is not implemented by disabling local diagnostics.

The implementation deletes dead full-workspace query branches and reuses existing
file dirty state instead of introducing another publication cache. It retains
revision tickets, event fences, dependency invalidation, scope completeness and
delivery proofs because removing them produces stale navigation, false uniqueness,
missing recovery or unsafe edits in real-process controls.

The adapted regression suite passed 16 scenarios/283 assertions and the parent
suite passed two scenarios/34 assertions on their fixed development candidate.
The later native pipeline ran all eight targeted regression mutations together
and detected all eight on its isolated source snapshot (consumer executable
`7825fbd3f7482706a63a0137d888a57b203d4799d854e27b26b0d8e120a53795`).
Its publication and long-path capacity failures were retained and corrected by
the focused controls described above. Build failures and runner errors do not
count as detected behavior loss.
