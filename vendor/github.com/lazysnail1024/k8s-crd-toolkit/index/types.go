// Package index stores bounded, caller-defined symbol and dependency summaries.
// It does not read files, retain source text, infer domain identities, or prove
// that a workspace has been completely discovered.
package index

import (
	"errors"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// Scope is an opaque discovery/binding partition chosen by the caller. It may
// be empty and does not imply a namespace, repository, or directory boundary.
type Scope string

// Key is a nonempty opaque binding identity within a scope.
type Key string

// SymbolID is a nonempty file-local identity. Stable IDs across position-only
// edits allow the index to recognize unchanged meaning.
type SymbolID string

// Symbol IDs are local to one file. Keys identify definitions in a
// caller-defined scope; duplicate keys are retained as distinct candidates.
type Symbol struct {
	ID              SymbolID
	Key             Key
	Name, Kind      string
	Span, Selection source.Span
}

// Reference locates a use of a key. A file may contain references without
// declaring symbols; domain ownership and call graphs are not required.
type Reference struct {
	Target Key
	Span   source.Span
}

// Facts contains summaries only. SemanticDigest must change when relevant
// meaning not represented by these fields changes. Dependencies records reads
// without a navigation location; references also establish dependencies.
type Facts struct {
	SemanticDigest [32]byte
	Symbols        []Symbol
	References     []Reference
	Dependencies   []Key
}

// Limits bounds retained files, symbols, reference/dependency entries, and
// string bytes. References counts both References and Dependencies, including
// duplicates. Bytes is a logical string budget, not a process memory limit.
// Bytes includes the URI, current scope, retained committed scope, and every
// string field in facts. Every limit must be positive; callers choose policy.
type Limits struct {
	Files, Symbols, References, Bytes int
}

var (
	ErrStaleTicket = errors.New("index: stale ticket")
	ErrLimit       = errors.New("index: capacity exceeded")
	ErrNoPrevious  = errors.New("index: no retained facts for this scope")
)

// Ticket is an opaque, single-use permission to replace one file's facts.
// Only the latest Begin for that file may commit, including after remove/reopen.
type Ticket struct {
	owner  *identity
	uri    source.URI
	serial uint64
}

// State describes only known files. Generation is the last mutation touching
// this scope; an unknown scope uses the current index generation. Pending counts
// known files awaiting replacement. Pending == 0 does not prove completeness,
// target absence, uniqueness, or that downstream analysis is up to date.
type State struct {
	Generation uint64
	Pending    int
}

type File struct {
	URI      source.URI
	Scope    Scope
	Revision source.Revision
	Fresh    bool
	Facts    Facts
}

type Definition struct {
	URI      source.URI
	Revision source.Revision
	Symbol   Symbol
}

type Use struct {
	URI       source.URI
	Revision  source.Revision
	Reference Reference
}

type SymbolResult struct {
	Items     []Definition
	State     State
	Truncated bool
}

// Change lists the changed URI and its direct dependent URIs, sorted and unique.
// Consumers schedule any further propagation. Position-only changes affect the
// changed URI alone. Generation is the index-wide mutation sequence.
type Change struct {
	Generation      uint64
	SemanticChanged bool
	Affected        []source.URI
}
