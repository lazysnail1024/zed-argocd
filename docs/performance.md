# Measuring language-server work

Set `ARGOCD_LS_TRACE=1` when launching `argocd-ls serve --stdio` to emit detailed
work records on stderr. Tracing is disabled by default. It does not change LSP
responses, analysis budgets, queue capacities, cancellation, or publication
guards. Existing `argocd-analysis` and `argocd-document-analysis` statistics retain
their diagnostic/index-pass meaning; in particular, `parsedFiles` is not a count
of every foreground or recovery parse.

## Record and attribution

Each new line starts with `argocd-work ` followed by JSON with `schemaVersion: 1`,
a session-local `sequence`, `origin`, `event`, `timing`, and, at work termination,
`work`. Foreground records also contain `method` and the JSON-RPC `requestId` when
its encoded length is at most 128 bytes. The sequence remains the correlation
key if a client reuses an ID. These records contain no resource text,
configuration text, file lists, or URI lists.

| Origin | Work boundary | Events |
| --- | --- | --- |
| `request` | Admitted analysis/navigation/completion/edit/explanation request, including its acquisition and recovery | `started`, `reply`, `worker-released` |
| `document-background` | One current-buffer diagnostic invocation | `started`, `completed`, `publication` |
| `workspace-background` | One bounded index advance, including acquisition and queued jobs | `started`, `completed`, `publication` |

Only `worker-released` and `completed` carry work totals. Sum each such terminal
record once by `sequence`; summing lifecycle events would count a request more
than once. `reply` describes delivery or refusal; `publication` distinguishes an
accepted background result from an obsolete result that was discarded. Performed
work is retained in the terminal record even if its result is later discarded.
An exiting process can interrupt an unfinished trace, so the absence of a
terminal record does not prove zero work.

Each operation receives its own context recorder. Concurrent foreground and
background workers do not subtract process-global counters. Scalar counts,
bounded operation names and nanosecond totals are retained; no per-file work
history is kept. With tracing disabled, normal calls skip recorder creation,
trace clocks, context traversal and dynamic counter-name construction. Detailed
stderr writes are serialized. A slow stderr reader can affect traced timings;
measurement clients should drain it concurrently.

The current detailed boundary covers language-server analysis requests and
diagnostic/index work. Initialization, unsupported/busy requests rejected before
admission, render execution, CLI commands, transport decoding, native filesystem
metadata calls, and the internals of external libraries are not claimed as
separate instrumented spans.

In particular, accepted `didOpen`/`didChange` snapshots perform UTF-8 validation,
newline indexing and text copying in the serial owner. That work can scale with
document length; it is not represented by a CST parse counter or its own span.
Configuration pin-tree YAML decoding and once-only embedded catalog decoding
are also outside the specialized config/schema decode counters (when performed
inside a measured worker, their elapsed cost remains in `serviceNs`). Use client
end-to-end edit/publication timings for costs outside admitted worker spans.

## Actual work counters

| Field | Meaning |
| --- | --- |
| `readAttempts` | Calls to bounded source/config acquisition, including cancellation, stat, size or regular-file refusal |
| `readBytes` | Bytes actually returned by bounded `ReadAll`, including partial/rejected/unstable input |
| `readFiles` | Successful physical `ReadAll` operations, including data subsequently refused by an authority or stability check |
| `readFailures` | Acquisition invocations that return an error; a missing optional config still records its failed acquisition |
| `readRetries` | Additional physical stability attempts inside one source acquisition |
| `parseCalls`, `parseBytes` | Calls and UTF-8 byte lengths submitted to the source CST parser; `sourceParseCalls`, `recoveryParseCalls`, `helmParseCalls` separate their purposes |
| `projectionCalls` | Calls to the bounded YAML value projection API |
| `configDecodes` | Actual strict configuration decodes, including the separate version-2 field-presence decode |
| `schemaLoads` | Actual embedded schema JSON decodes and keyword checking |
| `schemaCompiles` | Actual embedded schema compiler invocations; reuse does not increase this count |
| `schemaReuseHits` | An existing immutable compiled preparation is selected for analysis/query work |
| `schemaQueries` | Product calls to the shared `Lookup`, `Values`, `AllowsKey`, `Type` and `SelectShapes` APIs; the corresponding `schemaQuery*` fields give the operation breakdown |
| `schemaValidations` | Calls to compiled schema validation, including whole-document compatibility branch validation |
| `domainPasses`, `domainResources` | Domain invocations and resources reached in each invocation's first resource pass, including recursive ApplicationSet variants |
| `factsRestored` | Exact materialized document views admitted from a completed result under the existing identity/budget guards |
| `resultCacheHits` | Completed result reuse events; separate from the number of documents restored |
| `indexFactReuses` | Unchanged acquired input reusing its detached index facts; these are not materialized document views |

Parse counts concern the CST parser, not every scalar YAML decode. Schema query
counts concern product API entries, not internal graph-node visits. Timing
fields ending in `Ns` are inclusive wall-clock durations at those boundaries,
not CPU usage. For example, schema loading/compilation and domain recursion can
overlap an enclosing operation's measured duration; summing all durations does
not produce total CPU or request latency.

## Queue and cancellation timings

`ownerWaitNs` measures decoded-message arrival to owner admission. `dispatchNs`
measures admission to the worker's actual start. Foreground capacity rejects a
request when full; it does not maintain a waiting foreground queue.

The background work queue does wait. Its trace-only, bounded enqueue bookkeeping
records `queueJobs`, cumulative `queueWaitNs`, and `queueWaitMaxNs` when existing
queue identities are popped. Coalescing retains the first waiting enqueue time;
removal discards it. This observes the existing queue without changing scheduling.

`serviceNs` ends when computation finishes. `admitToReplyNs` includes delivery;
`admitToReleaseNs` ends after the actual request slot is released. Explicit
cancellation records `cancelToReplyNs` and `cancelToReleaseNs` independently.
These can occur in either order: a quick cancellation response alone is not
proof that the worker has stopped. Join the lifecycle events by sequence when
comparing them. Background `deliveryNs` is attached to its separate publication
event.

## Reproducible acceptance

`tests/work_observability_e2e.py` exercises a mixed first open, hover and recovered
completion during discovery, alternating current-version edits, root/target/
dependency rotation, and cancellation followed by another request in the same
capacity lane. It adds a trace-off control with identical public results. Its
known closed-file byte count, nonempty field result, schema compilation and real
cancelled parse calibrate the counters independently of implementation details.

`tests/incremental_performance_e2e.py` retains its original 1,000/3,000-resource,
30-comment-edit gates: `diskReads=0`, `parsedFiles=1`, `domainDocuments=2`,
`repositoryDecodes<=1`, and `acquisitionFallbacks=0`. Detailed work totals do not
replace or relax those gates. Kernel RSS samples remain separate from these
counters; Go heap size is not presented as RSS.

`tests/incremental_equivalence_e2e.py` retains its original nine histories and
adds complete-text application of versioned Project rename and nonempty field
completion/resolved repair. Its independent UTF-16/CRLF oracle checks exact
current versions, all affected documents, unchanged other-scope bytes, actual
diagnostic/binding recovery and untouched disk files. Performance improvements
must preserve those public results and existing invalidation guards.
