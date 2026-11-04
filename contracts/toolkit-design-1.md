# Toolkit public API contract

Contract ID: `toolkit-design-1`. This is a frozen public behavior contract. The local implementation has passed independent consumer E2E tests on Linux amd64; **there is no formal release yet**. The provider is k8s-crd-toolkit. Consumers depend only on this contract and their pinned implementation, without reading provider internals.

This file is the sole editable source of the API definition. Consumers retain a byte-identical snapshot and its SHA-256. The contract ID describes the contract revision; Go module SemVer selects an implementation. Upgrades must explicitly update both the snapshot and implementation lock. Changing only a digest does not establish compatibility.

## Dependencies and inputs

The only public packages are `source`, `yamlview`, and `edit`, with dependencies `yamlview → source` and `edit → source`. They contain no Argo GVKs or fields, schemas, LSP, filesystem scanning, namespaces, or product versions. A URI is an opaque caller-supplied string; callers own path normalization and file identity.

The following Go declarations define the public API. See README for usage and validation. Consumers must not import internal packages or third-party ASTs. The API does not introduce `any` configuration bags or domain callbacks.

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
func (d Document) Complete() bool
func (n Node) Kind() NodeKind
func (n Node) Span() source.Span
func (n Node) Members() []Member
func (n Node) Elements() []Node
func (n Node) Raw() string
func (n Node) Damaged() bool
```

- Input must be a valid Snapshot. Output exposes no underlying parser or C pointers. Parse completes all necessary copying and releases underlying resources before returning; consumers need not call Close.
- Documents follow source order. A `---` inside a block scalar is not a document separator. Duplicate keys remain separate Members rather than overwriting one another in a map. Members and Elements return empty slices when not applicable.
- Span and Raw refer to the original bytes of the same input snapshot. They do not decode quotes or escapes, infer YAML types, or expand aliases or merges. Strict YAML-to-JSON conversion and decoded-position mapping belong to consumers.
- An empty document has an Empty root. Damaged nodes have kind Error or Damaged=true and must not masquerade as ordinary scalars. Empty documents have Complete=true; damaged documents have Complete=false. Adjacent complete documents that can be reliably recovered remain usable.
- Ordinary syntax damage is recorded in Issues and may return a partial View. Infrastructure failures, cancellation, and budget overruns return errors; their results must not be treated as complete views. Context cancellation uses the standard cancellation error chain.
- Fixed supported limits are 5 MiB of input, 2,000 documents per stream, and syntax depth 256. Exceeding a limit explicitly returns an error. There is no budget policy interface. Future increases require resource and cancellation validation first.
- Documents, Issues, Members, and Elements return copies that cannot mutate internal state. The same snapshot produces the same observable structure. Nodes belong only to that input and must not be used to edit another revision.
- A complete syntax view does not establish that an object is valid Kubernetes input or passes domain checks.

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

## Consumption and evolution

The Go module is `github.com/lazysnail1024/k8s-crd-toolkit`; a release version has not yet been selected. Consumer locks record the contract ID and digest, module path, and actual implementation version or commit. Missing information means ready=false and must not be represented as readiness for independent release.

A new public capability requires the same semantic need in two real consumers and must not need to recognize any product name. Replacing the internal parser does not change the contract. Incompatible changes to a released API require a Go module major path and consumer migration, without forcing synchronized releases across all three repositories.

Consumer validation calls public APIs from an independent module and uses real process output to check coordinates, structure, edited bytes, and failures for known inputs. Expectations are written independently of the implementation. It requires neither an invented fourth CRD rule system nor access to this repository's development design or internal types.
