# Toolkit public API contract

Contract ID: `toolkit-design-6`. Consumers pin an implementation source commit; no release is required. The provider is k8s-crd-toolkit. The current API covers source coordinates, YAML structure and effective values, compiled-schema editor queries, checked text edits, incremental facts, cancellable work-bounded symbol queries, and input/work/discovery bookkeeping. Consumers depend only on this contract and their pinned implementation, without reading provider internals. Verification evidence identifies the exact implementation and platform tested; this contract does not assert that downstream editors have adopted it.

This file is the sole editable source of the API definition. Consumers retain a byte-identical snapshot and its SHA-256. The contract ID describes the contract revision; Go module SemVer selects an implementation. Upgrades must explicitly update both the snapshot and implementation lock. Changing only a digest does not establish compatibility.

## Dependencies and inputs

The public packages are `source`, `yamlview`, `yamlvalue`, `schemaquery`, `edit`, `index`, and `workspace`. `yamlvalue` uses `source` and `yamlview`; the other original infrastructure packages depend only on `source` within this module. `schemaquery` accepts the existing `github.com/santhosh-tekuri/jsonschema/v6` compiled graph and does not depend on the YAML or workspace packages. These packages contain no Argo GVKs or fields, bundled schemas, LSP, namespace interpretation, or product versions. Only `workspace.Scanner` performs filesystem discovery. No package writes user files, installs watchers, or accesses clusters. The native parser lazily starts at most two private workers pinned to OS threads, reused for the process lifetime to bound native allocator thread ownership. These workers only perform native parser execution; they do not schedule application analysis, discovery, or Go view construction. Other packages do not start workers. A URI is an opaque caller-supplied string; callers own path normalization and file identity. Scanner paths are native absolute filesystem paths, not URIs.

The following Go declarations define the public API. See README for usage and validation. Consumers must not import internal packages; no third-party YAML AST is exposed. Compiled JSON Schema metadata is an explicit dependency of `schemaquery`, borrowed immutably. `any` below represents JSON-compatible data values, not configuration bags. There are no domain callbacks or registries.

## source: immutable text and coordinates

```go
type URI string
type Revision uint64
type Span struct { Start, End int }
type Position struct { Line, UTF16Column uint32 }
type Change struct { Range Span; Text string }
type Snapshot struct { /* private */ }

func New(uri URI, revision Revision, text string) (Snapshot, error)
func (s Snapshot) URI() URI
func (s Snapshot) Revision() Revision
func (s Snapshot) Text() string
func (s Snapshot) Position(offset int) (Position, error)
func (s Snapshot) Offset(p Position) (int, error)
func (s Snapshot) Slice(span Span) (string, error)
func (s Snapshot) Apply(next Revision, changes []Change) (Snapshot, error)
```

- Span is a half-open UTF-8 byte interval `[Start, End)`. Position uses zero-based lines and UTF-16 columns. Callers allocate monotonically increasing revisions within a document lifetime; a revision is not an LSP version.
- New rejects invalid UTF-8 and preserves the original text, BOM, and line endings. A BOM counts as one U+FEFF column; a non-BMP character occupies two UTF-16 code units. The final line need not end in a newline. A zero Snapshot has an empty URI, revision 0, and empty text.
- Out-of-bounds positions, offsets inside UTF-8 characters, and positions inside UTF-16 surrogate pairs return errors rather than being clamped. The CR offset in CRLF is the end of the preceding line; the position between the two bytes cannot be converted to Position. The offset after LF starts the next line. Slice requires valid UTF-8 boundaries but does not additionally forbid a boundary between CR and LF.
- Apply applies changes **sequentially**: each Range refers to the text after the preceding change. next must strictly increase. If any change fails, the entire batch produces no new result and the original snapshot remains unchanged.
- Snapshots support concurrent reads and must not share mutable internal slices. Error messages are not a stable API; callers handle errors and preconditions rather than parsing English messages.

## yamlview: a fault-tolerant YAML view with source locations

```go
type NodeKind uint8
const (
    Empty NodeKind = iota
    Mapping
    Sequence
    Scalar
    Alias
    Error
)
type Node struct { /* private */ }
type Member struct { Key, Value Node }
type ParseIssue struct { Span source.Span; Message string }
type Document struct { /* private */ }
type View struct { /* private */ }

func Parse(ctx context.Context, s source.Snapshot) (View, error)
func (v View) Documents() []Document
func (v View) Issues() []ParseIssue
func (d Document) Root() Node
func (d Document) Span() source.Span
func (d Document) ContentSpan(s source.Snapshot) (source.Span, error)
func (d Document) Complete() bool
func (n Node) Kind() NodeKind
func (n Node) Span() source.Span
func (n Node) Members() []Member
func (n Node) Elements() []Node
func (n Node) Raw() string
func (n Node) Damaged() bool
```

- Input must be a valid Snapshot. Output exposes no underlying parser or C pointers. Parse completes all necessary copying and releases underlying resources before returning; consumers need not call Close. Native execution admission observes context cancellation. Once admitted, cancellation is processed by the native parser and all per-call native resources are released before Parse returns. The calling goroutine constructs the copied Go result; it does not occupy a native worker while doing so. Completed or cancelled requests must not remain retained by idle workers. Native parsing yields between timeout slices so a slow call does not occupy a worker for its entire parse. The private workers are shared within one process; callers still own input budgets and application concurrency.
- Documents follow source order. A `---` inside a block scalar is not a document separator. Duplicate keys remain separate Members rather than overwriting one another in a map. Members and Elements return empty slices when not applicable.
- Span and Raw refer to the original bytes of the same input snapshot. They do not decode quotes or escapes, infer YAML types, or expand aliases or merges. Consumers may use `yamlvalue` for bounded effective values; product-specific decoded-position mapping remains outside `yamlview`.
- ContentSpan first verifies that the full original Document.Span bytes match the supplied snapshot, returning an error on mismatch or invalid range. It then returns a source span with the same start, optionally omitting a suffix proven by the complete CST to contain only ASCII printable comment nodes and intervening ASCII space/CR/LF. It preserves all bytes before the first omitted comment, including block-scalar whitespace, and never changes Document.Span, Node.Span, or Raw. It may conservatively return the original span, and always does so for documents containing any TAB or incomplete documents; non-ASCII comments or significant bytes also stop omission. This is a decoder-input optimization, not a source size, input budget, edit range, or replacement for strict YAML validation. A zero Document returns an empty span for a valid snapshot.
- An empty document has an Empty root. Damaged nodes have kind Error or Damaged=true and must not masquerade as ordinary scalars. Empty documents have Complete=true; damaged documents have Complete=false. Adjacent complete documents that can be reliably recovered remain usable.
- Ordinary syntax damage is recorded in Issues and may return a partial View. Infrastructure failures, cancellation, and budget overruns return errors; their results must not be treated as complete views. Context cancellation uses the standard cancellation error chain.
- Fixed supported limits are 5 MiB of input, 2,000 documents per stream, and syntax depth 256. Exceeding a limit explicitly returns an error. There is no budget policy interface. Future increases require resource and cancellation validation first.
- Documents, Issues, Members, and Elements return copies that cannot mutate internal state. The same snapshot produces the same observable structure. Nodes belong only to that input and must not be used to edit another revision.
- A complete syntax view does not establish that an object is valid Kubernetes input or passes domain checks.

## yamlvalue: effective values and source provenance

```go
type Options struct {
    MaxNodes, MaxVisits int
    RejectExplicitTags bool
    Fields []string
}
type Result struct {
    Value any
    Origin *Origin
    Nodes, LogicalNodes int
    Shared map[source.Span]bool
    Fields map[string]string
}
type Error struct { Code, Message string }
type Origin struct {
    Node, Key yamlview.Node
    Kind string
    Children map[string]*Origin
}
func Project(ctx context.Context, snapshot source.Snapshot, doc yamlview.Document, options Options) (Result, error)
func (origin *Origin) At(parts []string) Origin
func Shared(anchors map[source.Span]bool, span source.Span) bool
```

- Project requires a complete document from the supplied snapshot. It produces JSON-compatible values using the pinned mature YAML decoders. Duplicate or non-string mapping keys, unknown tags, cyclic aliases, ambiguous merges, and invalid projections fail explicitly. A scalar exceeding 1 MiB or expanded depth greater than 256 is rejected. Ordinary documents use the strict YAML-to-JSON path; aliases and merges receive bounded expansion and provenance. Explicit mapping entries override merges, and earlier mappings in a merge sequence override later mappings.
- Project uses ContentSpan only to avoid repeatedly decoding proven trailing ASCII comments. Full original document bytes still establish snapshot correspondence; source/provenance positions and input-byte accounting remain based on the original snapshot. Documents containing TAB retain their full span. Non-ASCII comments and control characters remain in the decoder input, preserving strict rejection behavior and the interpretation of YAML Unicode line separators.
- MaxNodes bounds expanded non-alias nodes. Zero allows ordinary documents but no alias/merge expansion; negative limits fail. Positive MaxVisits additionally bounds visits including aliases and ordinary syntax nodes; zero disables this extra visit limit. Callers choose budgets and account for an entire stream or request. Nodes charges expansion work; LogicalNodes charges the effective expanded value. Both are zero for ordinary documents and do not represent RSS.
- RejectExplicitTags also rejects standard explicit tags. Custom tags are always unsupported. The option is caller policy, not inferred from a resource kind or version.
- Origin is absent for ordinary documents, whose locations remain in the original YAML view. For projected values, `exact` means an independent original location, `derived` points to an alias/merge use, and `ambiguous` marks a location shared through an anchor. At uses effective map keys or decimal array indices; for a missing child it retains the nearest known origin, so callers must independently verify path existence. Shared tests containment in a reused anchor span. A derived or shared location is not an independent edit target.
- Fields requests root string values selected by the caller. Independently known values can be returned even on projection failure, but never establish validity, complete recognition, or a complete effective value. Consumers must handle the error and keep these hints separate from committed complete facts.
- The internal YAML graph is temporary and is not exposed. Result maps belong to the caller; provenance can retain input nodes and must not be stored as a closed-file index summary. No schema, GVK table, resource identity, inheritance policy, or edit decision is provided.
- Context is checked during traversal and between decoder calls; a single decoder call is not preemptible. Input and work bounds remain necessary. Error.Code identifies `YAML_PROJECTION_INCOMPLETE` or `INPUT_BUDGET_EXCEEDED` where applicable; error text is descriptive rather than a stable protocol.

## schemaquery: bounded editor queries over compiled JSON Schema

```go
type Limits struct { Work, Depth int }
type Result struct {
    Nodes []*jsonschema.Schema
    Ambiguous bool
    Complete bool
}
var ErrBudget error
var ErrUnsupported error
func Lookup(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (Result, error)
func Values(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) ([]any, error)
func AllowsKey(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (bool, error)
func Type(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (string, error)
func SelectShapes(ctx context.Context, branches []*jsonschema.Schema, value any, active []string, limits Limits) ([]*jsonschema.Schema, error)
```

- Callers compile, load, select, and retain schemas. The compiled graph and input value must remain immutable during a call. Returned schema pointers refer to that graph. The package owns no compiler, registry, persistent cache, product-version rules, YAML parser, or validator replacement.
- Paths use property names and decimal array indices. Queries traverse properties/items, static references, and allOf/anyOf/oneOf. Explicit finite sibling constraints select viable alternatives; the active path is excluded so an unfinished field does not hide its own repair candidates. Ambiguous means multiple shapes remain possible, not that the document is invalid.
- Values returns deduplicated finite enum/const candidates in deterministic JSON-representation order. Candidate validation uses the existing compiled validator and preserves exactly-one semantics for oneOf. Lookup and acceptance checks share one work budget; partial candidates never masquerade as a complete result.
- AllowsKey respects enclosing object closure without requiring placeholder values or a completed object. Product annotations such as Kubernetes preserve-unknown-fields must be reflected in the supplied open schema or handled by the consumer. Type returns the queried type description; an empty successful result and an error have different meanings.
- SelectShapes compares existing field names and ignores the active field, missing required values, ordinary value errors, and negative `not` assertions. A negative assertion in another field cannot prevent querying an independent field. This does not relax Lookup/Values path checks or the compiled validator; conditional and dynamic shapes remain explicitly unsupported. The caller decides whether an empty selection retains its original version catalog.
- Unsupported path-dependent constructs, including conditional/dynamic shapes inspected by the query, return ErrUnsupported. Exhaustion returns ErrBudget. A failed Lookup has Complete=false and no complete partial answer. Consumers must propagate unknown/incomplete state and must not interpret either error as a missing field or safe edit.
- Zero Limits fields use Work=100000 and Depth=256; negative limits permit no work. A validator call counts as one visit. Context is checked between operations, but the validator does not expose preemption within a call; callers must also bound schema and value sizes. Work limits are not a wall-clock deadline.

## edit: atomic text replacements on one snapshot

```go
type Replacement struct {
    Span source.Span
    ExpectedText string
    NewText string
}
type Plan struct {
    BaseRevision source.Revision
    Replacements []Replacement
}
func Apply(base source.Snapshot, next source.Revision, p Plan) (source.Snapshot, error)
```

Every Replacement refers to **the same original snapshot**, unlike the sequential coordinates of source.Apply. All entries are validated before applying replacements in reverse byte order. The input slice, base, and files on disk remain unchanged.

| Input | Result |
|---|---|
| BaseRevision mismatch or next does not strictly increase | Error; original text unchanged |
| Span out of bounds, reversed, or inside a character; NewText is invalid UTF-8 | Error; original text unchanged |
| ExpectedText differs from the original text | Error; original text unchanged |
| Overlapping replacements, insertion inside a replacement, or multiple insertions at one offset | Error; original text unchanged |
| Adjacent replacements or a single insertion at a replacement boundary | Allowed; original coordinates determine the result |
| Valid non-overlapping replacements | Returns the next snapshot; all other bytes unchanged |

This package establishes only text preconditions. Consumers decide whether a domain fix is valid and whether the version still matches when LSP applies it. Cross-file transactions and implicit disk writes are unsupported.

## index: incremental symbol and dependency summaries

```go
type Scope string
type Key string
type SymbolID string
type Symbol struct {
    ID SymbolID
    Key Key
    Name, Kind string
    Span, Selection source.Span
}
type Reference struct {
    Target Key
    Span source.Span
}
type Facts struct {
    SemanticDigest [32]byte
    Symbols []Symbol
    References []Reference
    Dependencies []Key
}
type Limits struct { Files, Symbols, References, Bytes int }
type Ticket struct { /* private */ }
type Index struct { /* private */ }
type State struct { Generation uint64; Pending int }
type File struct {
    URI source.URI
    Scope Scope
    Revision source.Revision
    Fresh bool
    Facts Facts
}
type Definition struct { URI source.URI; Revision source.Revision; Symbol Symbol }
type Use struct { URI source.URI; Revision source.Revision; Reference Reference }
type SymbolResult struct { Items []Definition; State State; Truncated bool }
type SymbolQueryResult struct {
    Items []Definition
    State State
    Truncated, Complete bool
    Work int
}
type Change struct {
    Generation uint64
    SemanticChanged bool
    Affected []source.URI
}
var ErrStaleTicket error
var ErrNoPrevious error
var ErrLimit error
var ErrQueryBudget error
var ErrQueryChanged error

func New(limits Limits) (*Index, error)
func (i *Index) Begin(uri source.URI, scope Scope, revision source.Revision) (Ticket, error)
func (i *Index) Commit(ticket Ticket, facts Facts) (Change, error)
func (i *Index) Reuse(ticket Ticket) (Change, error)
func (i *Index) Remove(uri source.URI) (Change, error)
func (i *Index) GetFile(uri source.URI) (File, bool, State)
func (i *Index) Definitions(scope Scope, key Key) ([]Definition, State)
func (i *Index) References(scope Scope, key Key) ([]Use, State)
func (i *Index) Dependents(scope Scope, key Key) ([]source.URI, State)
func (i *Index) Symbols(scope Scope, query string, limit int) (SymbolResult, error)
func (i *Index) SymbolsWithPrefix(scope Scope, prefix Key, query string, limit int) (SymbolResult, error)
func (i *Index) SymbolsWithPrefixContext(ctx context.Context, scope Scope, prefix Key, query string, limit, maxWork int) (SymbolQueryResult, error)
```

### Identity and ownership

- Scope is an opaque caller-defined visibility partition. It does not imply a directory, Git repository, namespace, or cluster. Each URI occupies one scope per Index; a scope may span many filesystem roots. Keys are opaque **within a scope**, and the `(scope, key)` pair is structurally distinct, not concatenated. Consumers encode any resource kind, namespace, template owner, or configuration identity they need in their own keys.
- A file can contain many documents and symbols, only references, or no facts. Symbol IDs are local to that URI; IDs and keys must be nonempty, and duplicate IDs are rejected. A reference requires a target key and source span, without a local declaration or synthetic owner. Multiple symbols or files may define the same key: queries preserve every candidate and never choose a binding. Symbol trees, reference ownership, and call graphs are domain structures outside this index.
- Spans are byte ranges in the file revision. The index checks nonnegative, ordered ranges and Selection containment within Span. It does not retain text and therefore cannot check UTF-8 boundaries or the actual file length; consumers must validate those against the source snapshot before submission.
- Facts are summaries, not a place to store ASTs or full document text. Mutable containers are copied on entry and exit; stored strings are detached from caller string backing storage. Returned strings are immutable Go values. Input containers must not be concurrently changed during a call.

### Updates, invalidation, and freshness

- Construct an Index with New; do not copy it after use. Methods support concurrent callers, and each call observes an atomic state. There is no multi-call transaction: a consumer combining multiple queries must compare their scope generations and revalidate before a correctness-sensitive operation.
- Begin announces a new analysis input **before** queuing or computing it. Existing locations immediately cease to appear in Definitions, References, Symbols, and GetFile. Old summaries remain privately available for semantic comparison and dependency invalidation. Repeated Begin for one URI does not duplicate pending counts.
- Revisions may increase or remain equal for a configuration recomputation; lower revisions fail while a file is registered. Each successful Begin also allocates a distinct opaque ticket. A ticket from another Index, an earlier Begin, a removed/recreated file, or an already successful Commit is rejected with ErrStaleTicket. Remove/reopen remains safe even if a consumer reuses its numeric source revision. Zero tickets are invalid.
- Commit atomically replaces the complete file fact set, including empty sets. Invalid facts or a capacity error leave the file pending and the ticket retryable; they never publish an empty successful analysis. Begin admission failures leave existing state unchanged, so the caller must record the failed input and must not present older facts as current; Remove can withdraw them when admission cannot be retried.
- Reuse consumes a current ticket and restores its same-scope retained commit without copying its facts. The consumer must first prove identical input bytes and interpretation settings; the index cannot establish that proof. A successful reuse updates the published revision and scope generation, changes no retained budgets, and affects only the URI itself. Missing retained facts or a different committed scope returns ErrNoPrevious and leaves the ticket usable for normal analysis and Commit. Stale, removed/recreated, cross-instance, and replayed tickets are rejected. Pending old locations remain hidden until successful Reuse or Commit.
- Remove withdraws all facts and dependencies, releases their budgets, and revokes pending tickets. Unknown URIs are a no-op. Removing a disk file is not permission to Remove an authoritative open buffer; input ownership is decided before changing the index.
- GetFile returns fresh facts, or pending metadata with no old facts. During a scope migration, the old scope remains pending until its old facts are replaced or removed; the destination is pending too. Intermediate abandoned destinations are released.
- State.Generation changes when the relevant scope changes; legacy queries use the current index generation for unknown scopes. The context-aware symbol query instead returns Generation=0 for an unknown scope, so unrelated scope changes do not invalidate an empty scope traversal. State.Pending counts **known files waiting for facts**, not undiscovered files or downstream diagnostics. Change.Generation is the index-wide sequence. `Pending == 0` never proves discovery completeness, nonexistence, unique binding, or a safe rename.

### Dependencies and semantic changes

- Every Reference also establishes a reverse dependency, even if its target has no definitions. Dependencies adds keys without navigable source positions, including caller-defined path/configuration dependencies. Empty target/dependency keys are rejected.
- Definitions and References return fresh locations; Dependents intentionally also includes old pending summaries so a target change can still schedule its callers. All URI sets are deduplicated and sorted. Definitions sort by URI, Span, and symbol ID; References sort by URI and Span.
- SemanticDigest must reflect meaning relevant to consumers but absent from the public facts, for example a template signature, domain ownership, or policy. The index additionally compares symbol identity/key/name/kind, reference target, and dependency multisets. Array order and spans do not affect semantic equality; duplicate reference/dependency counts do. Reusing a digest cannot hide a changed key or relation.
- A semantic or scope change returns the changed URI plus direct dependents of its old and new definition keys in Change.Affected. This includes previously unresolved uses and duplicate-candidate changes. A position-only change updates locations and revision but returns only the changed URI. Navigation caches must still use scope Generation/revision freshness rather than relying on Affected to detect moved locations. The caller schedules downstream recomputation and further propagation; the library does not run analysis.
- Symbols performs a case-sensitive substring search over fresh names. SymbolsWithPrefix additionally requires the opaque key to start with prefix, before applying the result limit; an empty prefix is equivalent to Symbols. Callers encode kind/namespace partitions themselves. A positive limit bounds retained result items, sorted deterministically; Truncated reports additional known matches. Search still visits known symbols in the scope and is not a filesystem search or a completeness proof. No caller predicate executes under the index lock.

### Cancellable symbol queries

- SymbolsWithPrefixContext has the same case-sensitive name/prefix filtering and URI/span/symbol-ID result order as SymbolsWithPrefix. It requires a nonnil context and positive limit and maxWork. Existing Symbols and SymbolsWithPrefix retain their atomic legacy behavior; interactive callers should use the context-aware API.
- Work charges one visit for each scope member file, including pending and zero-symbol files, plus one visit for each fresh symbol inspected, including nonmatching prefixes and names. It never exceeds maxWork. limit bounds retained result items independently of Work. Callers querying multiple scopes or prefixes must subtract returned Work from their overall request budget. Work measures scanning operations, not bytes, heap comparisons, milliseconds, or discovery completeness.
- Complete=true means all known index members of the returned scope generation were inspected successfully. Truncated=true independently means matching results exceeded limit; a complete traversal can have truncated results. Neither Complete nor Pending==0 proves that filesystem discovery or domain analysis is complete, that a binding is unique, or that an edit is safe.
- Cancellation returns the standard context error chain; work exhaustion returns ErrQueryBudget; mutation of the queried scope during traversal returns ErrQueryChanged. Every error has Complete=false and Items=nil, even if some matches were already found. Work still reports the work spent before failure. A caller must not replace previously valid diagnostics or prove absence/uniqueness using an unfinished query. No automatic restart silently consumes another full budget.
- A query holds the index read lock for bounded batches of at most 128 file/symbol visits, including a cursor within a large single file. Matching, result heap maintenance, and final ordering happen outside the lock. Waiting to acquire the lock is cancellable; cancellation is checked between batches and result processing steps. The implementation retains at most one bounded batch and limit results, plus a cursor into existing scope membership; it does not first copy every URI or keep a second symbol store. Native string operations and scheduler delays are not hard real-time guarantees.
- The internal cursor is fenced by the queried scope's generation, including remove/recreate and pending migration of old/new scopes. Mutation generations never wrap or repeat; exhaustion rejects further mutation. A mutation confined to another scope does not invalidate the query. Queries recheck the selected generation before returning; consumers still revalidate before delayed publication or combining multiple calls. Domain predicates never execute under the index lock, and no LSP worker, retry policy, namespace interpretation, or source text is introduced.

### Capacity

All Limits fields must be positive. Files counts registered URIs, including pending ones; Symbols counts retained definitions; References counts reference and explicit dependency entries, including duplicates. Bytes counts logical retained string lengths in facts, URIs, and scope metadata, including the old scope needed during a pending migration. It is not an RSS estimate. Admission checks are atomic, failed commits do not consume capacity, and removal releases capacity. Callers must retain an explicit limited/incomplete state and apply backpressure or retry on ErrLimit rather than silently drop inputs.

## workspace: authoritative inputs, queues, and resumable discovery

```go
var ErrStale error
var ErrLimit error
var ErrOverlay error

type DocumentLimits struct { Entries, FileBytes, OpenBytes int }
type ReadTicket struct { /* private */ }
type Documents struct { /* private */ }
func NewDocuments(limits DocumentLimits) (*Documents, error)
func (d *Documents) Open(uri source.URI, text string) (source.Snapshot, error)
func (d *Documents) Change(uri source.URI, base source.Revision, changes []source.Change) (source.Snapshot, error)
func (d *Documents) Snapshot(uri source.URI) (source.Snapshot, bool)
func (d *Documents) OpenBytes() int
func (d *Documents) BeginRead(uri source.URI) (ReadTicket, error)
func (d *Documents) PublishRead(ticket ReadTicket, text string) (source.Snapshot, error)
func (d *Documents) DiscardRead(ticket ReadTicket) bool
func (d *Documents) Invalidate(uri source.URI)
func (d *Documents) Close(uri source.URI) bool

type Priority uint8
const ( Current Priority = iota; Related; Background )
type Job struct { URI source.URI; Priority Priority }
type Queue struct { /* private */ }
func NewQueue(capacity int) (*Queue, error)
func (q *Queue) Put(job Job) error
func (q *Queue) Pop() (Job, bool)
func (q *Queue) Remove(uri source.URI)
func (q *Queue) Len() int

type WorkTicket struct { URI source.URI; Priority Priority; /* private */ }
type WorkQueue struct { /* private */ }
func NewWorkQueue(readyCapacity, outstandingCapacity int) (*WorkQueue, error)
func (q *WorkQueue) Put(job Job) error
func (q *WorkQueue) Pop() (WorkTicket, bool)
func (q *WorkQueue) Complete(ticket WorkTicket) bool
func (q *WorkQueue) Remove(uri source.URI)
func (q *WorkQueue) Pending(uri source.URI) bool
func (q *WorkQueue) Running(uri source.URI) bool
func (q *WorkQueue) Waiting(uri source.URI) bool
func (q *WorkQueue) Len() int
func (q *WorkQueue) Backlog() int
func (q *WorkQueue) Wake() <-chan struct{}

type Membership struct { /* private */ }
type Observation struct { /* private */ }
type ScanPass struct { /* private */ }
func (m *Membership) Start() ScanPass
func (m *Membership) Observe() Observation
func (m *Membership) Seen(observed Observation) bool
func (m *Membership) Finish(pass ScanPass, complete bool) bool
func (p ScanPass) Missing(observed Observation) bool

type ScanOptions struct {
    Extensions []string
    ExcludeDirs []string
    MaxDirectories int
}
type ScanIssue struct { Path string; Err error }
type Batch struct {
    Files []string
    Visited, Pending int
    Done, Complete bool
    Issues []ScanIssue
}
type Scanner struct { /* private */ }
func NewScanner(roots []string, options ScanOptions) (*Scanner, error)
func (s *Scanner) Next(ctx context.Context, maxWork int) (Batch, error)
func (s *Scanner) Prioritize(directory string) error
func (s *Scanner) Close() error
```

### Authoritative input lifecycle

- Documents and Queue are concurrently safe, constructor-initialized objects; neither may be copied after use. Documents requires nonempty URIs and positive limits. Entries bounds open buffers plus outstanding reads. FileBytes bounds one input and each intermediate sequential edit result; OpenBytes bounds the total current open-buffer text. Limits exclude caller-held snapshots, caller-owned incoming text, and snapshot line-index overhead.
- Documents.OpenBytes returns the total accepted open text under the same lock as Open/Change/Close. Protocol adapters may retain references to the returned immutable snapshots and editor versions; they should not copy updates into a second Documents store or maintain a second byte counter. A caller reserving a separate disk-read budget must also fence concurrent input changes before accepting the reads.
- Open establishes an authoritative buffer and revokes earlier disk reads. Opening an already open buffer returns ErrOverlay; use Change with the current source revision. Change is atomic, uses sequential coordinates, and preserves the old buffer on any failure. Snapshot only returns open-buffer content.
- Documents allocates monotonically increasing revisions across all its input lifetimes. These are independent of LSP versions. The protocol adapter must independently validate incoming editor versions; it must not substitute them for Documents revisions.
- BeginRead returns ErrOverlay while a buffer is open, enabling callers to skip disk I/O. Otherwise it reserves the newest read of that URI. Perform bounded disk I/O outside the object, then PublishRead. A superseded, cross-instance, replayed, invalidated, or close/reopen ticket returns ErrStale. Other files' updates do not invalidate this ticket.
- PublishRead returns a snapshot without retaining closed-file text. A valid ticket is consumed even if text is invalid UTF-8 or exceeds FileBytes. After failed/canceled I/O, call DiscardRead with that worker's ticket so abandoned entries do not consume capacity. It releases only a still-matching pending read, returning true; stale, cross-instance, replayed, or superseded tickets return false without touching the latest input or an open buffer. FileBytes is checked on publication; the caller must bound the read before allocating its text.
- Invalidate revokes a pending disk read after an authoritative disk change/deletion event. It is not worker cleanup: a canceled old worker must use DiscardRead so it cannot revoke a newer worker's read. An open buffer stays authoritative, even if the disk file has disappeared. Close releases the overlay and revokes reads, returning whether a buffer existed. It never restores an old cached disk version: the caller must read current disk content afresh, or remove effective facts if no input remains. Closing can also withdraw an input when its root is removed.
- Input lifecycle and index lifecycle are separate primitives. A consumer serializes the acceptance of disk results, open/change/close events, configuration changes, and corresponding Index.Begin/Remove operations in its coordinator. Analysis workers receive the accepted snapshot and ticket; they must not create a newer ticket after computing an obsolete result.

### Queue behavior

- Queue capacity must be positive. A Job is a notification that a URI needs work; it carries no input snapshot, ticket, or version. Put coalesces pending notifications for the same URI and preserves the highest requested priority. An existing URI can be coalesced or promoted at capacity; new URIs return ErrLimit when full.
- Pop follows an 8:4:1 weighted round robin for Current, Related, and Background, skipping empty lanes, with FIFO order within each lane. Under continuous load in all lanes, a background job receives a turn within 13 successful pops. The policy guarantees service opportunities, not elapsed-time latency.
- Pop removes pending membership immediately. An event for a running file can therefore enqueue another notification; there is no permanent URI deduplication. The consumer selects that URI's latest accepted snapshot and original index ticket when dispatching work. A notification with no accepted input can be ignored. Input freshness is enforced by Documents and Index tickets, not by queue order. The queue does not track completed work or cancel workers. Consumers bound worker concurrency and preserve events when applying backpressure. A priority queue cannot preempt a blocking syscall; foreground responsiveness also needs an available foreground execution slot.

### Work lifecycle and overflow

- WorkQueue adds running reservations and deferred notifications to the notification-only Queue contract. It is concurrently safe and owns no workers or timers. Ready capacity must be positive and no greater than outstanding capacity. Outstanding capacity counts distinct waiting/running URIs, including retired executions; consumers can budget their file-entry limit plus their fixed maximum worker count.
- Put retains one unstarted notification per URI at the highest requested priority, including when the ready queue is full. A notification during execution schedules another run after Complete. Only exhaustion of the distinct outstanding capacity returns ErrLimit; callers must surface this failure or retry, not report the file as complete.
- Pop returns a single execution reservation. No second worker receives the same URI until Complete, including after Remove and recreation. Weighted 8:4:1 service includes overflow as well as ready work, so background service remains bounded even at ready capacity one. Len counts ready work only; Backlog counts the other unstarted notifications, including a rerun requested during execution.
- Every successful Pop must be paired with Complete on every worker exit path. Complete consumes that exact reservation and automatically admits deferred work. Remove discards unstarted notifications, but an active execution remains reserved until its worker exits. Its Complete returns false while releasing the retired reservation; it cannot discard a newer recreation notification. Foreign/replayed completions do nothing. The boolean does not authorize publication: Documents/Index tickets and consumer configuration checks still govern input validity.
- Pending includes waiting and running reservations. Waiting includes ready, overflow, and a requested rerun; Running includes retired executions that have not exited. Wake is a coalesced hint channel, never closed by the library. Workers Pop first, then wait on Wake or their own cancellation when no job exists. Consumers do not need to periodically scan every file to refill the queue.

### Scan observations

- Membership has a usable zero value and is concurrently safe; do not copy it after use. It stores a current pass, not a second URI map. Consumers retain one Observation in each file's existing metadata.
- Start supersedes all older passes. Observe marks discovery or an authoritative watcher/input event as present in the current pass. Store the mark under the same consumer lock used for file registration and sweeping; otherwise a watcher and sweep could still race outside this mechanism.
- Finish consumes only the current active pass. Successful complete enumeration permits Missing to identify older observations; failed, incomplete, superseded, or repeated finishes do not authorize a new sweep. Starting another pass invalidates every previous pass's Missing result.
- Missing is false before successful completion and for an observation made during that pass, including a watcher event in a directory already scanned. The consumer additionally protects authoritative open buffers and applies its file-selection policy. Membership does not establish domain completeness or perform deletions.

### Discovery behavior

- NewScanner validates options and normalizes native absolute paths without opening the supplied roots, enumerating directories, or reading file contents. Relative paths may consult the current working directory. Extensions is an explicit case-insensitive extension allowlist (for example `.yaml`, `.yml`); empty means all regular files. ExcludeDirs lists exact, case-sensitive directory basenames. Neither YAML extensions nor `.git` exclusions are hardcoded. These filters are discovery policy, not resource recognition.
- Scanner calls are serial, including Prioritize and Close. Next requires a nonnil context and maxWork in 1..4096. Visited counts inspected entries, opened directories, and empty directory reads. Nonmatching files consume work too. This bounds work units, not milliseconds: OS operations themselves may block.
- The scanner retains one background cursor and at most one priority cursor, with at most two handles and 512 buffered directory entries. MaxDirectories is a positive bound on remembered directory identities, including completed ones; it is not a limit on the number of ordinary files inspected. Directory identities prevent repeated traversal with overlapping roots or a prioritized descendant.
- Prioritize can bring a not-yet-discovered directory inside a configured root forward, without waiting to enumerate its parents. It pauses an active background cursor without restarting it. An active priority directory is not preempted by another priority directory. Descendants inherit priority. Excluded paths cannot be reintroduced by Prioritize; an explicitly configured root inside an otherwise excluded ancestor remains an explicit root. Completed directories are not rescanned; create a new Scanner for another reconciliation pass.
- Child symlinks and nonregular files are skipped. A configured root that is itself a symlink, an unreadable/missing directory, or a file mistaken for a root produces a ScanIssue. Priority paths have their components checked for symlinks. These checks are traversal policy, not a security sandbox against hostile concurrent filesystem mutations.
- Files are native paths, not predecoded YAML resources. ScanIssue.Err preserves the underlying I/O/limit error where available. A directory error or exhausted MaxDirectories makes the traversal incomplete, while remaining admitted directories can still be processed. Per-call Issues must be accumulated by the caller; the final Complete flag remains false after any such error.
- Cancellation returns its error and any usable partial Batch; the caller must process those Files before handling the error. The cursor can resume without dropping already-read entries. Cancellation does not claim Complete, and restarting from the root is unnecessary. Close releases handles, is idempotent, and prevents further use.
- Done reports exhausted enumeration; Complete additionally requires no discovery errors/limits and an open scanner. A successful scan is not a transactional disk snapshot. It does not mean all candidates were read, recognized, indexed, or validated. Consumers combine discovery status, index pending state, queued/running work, and read/analysis failures before asserting a complete semantic scope. Never infer deletion from an incomplete scan or let an older scan delete inputs observed after that pass began.

## Consumption and evolution

The Go module is `github.com/lazysnail1024/k8s-crd-toolkit`. Consumers use an immutable source commit and its Go pseudo-version, without requiring a tag or release. Consumer locks record the contract ID and digest, module path, and actual implementation version or commit. Missing information means ready=false and must not be represented as readiness for independent release.

A new public capability requires the same semantic need in two real consumers and must not need to recognize any product name. Replacing the internal parser does not change the contract. Published tags are immutable. During v0/alpha development, later versions may make documented incompatible changes with an explicit contract revision and consumer migration; consumers must opt in to each upgrade. From v1 onward, incompatible changes to a stable released API require a new Go module major version and its corresponding major path (v2 and later), without forcing synchronized releases across all three repositories.

Consumer validation calls public APIs from an independent module and uses real process output to check coordinates, structure, edited bytes, and failures for known inputs. Expectations are written independently of the implementation. It requires neither an invented fourth CRD rule system nor access to this repository's development design or internal types.
