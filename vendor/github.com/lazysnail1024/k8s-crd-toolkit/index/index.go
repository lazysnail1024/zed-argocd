package index

import (
	"fmt"
	"sort"
	"strings"
	"sync"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

type identity struct{ marker byte }
type lookup struct {
	scope Scope
	key   Key
}
type uriSet map[source.URI]struct{}
type stored struct {
	scope Scope
	facts Facts
	bytes int
}
type entry struct {
	uri      source.URI
	scope    Scope
	revision source.Revision
	serial   uint64 // Nonzero while pending; zero after a successful commit.
	data     *stored
}
type scopeState struct {
	generation  uint64
	files       map[source.URI]*scopeMember
	first, last *scopeMember
	pending     int // Number of true values; State remains O(1).
}

// Links borrow the existing file entry, not another URI or symbol snapshot. A
// query retains one position between short locks; scope mutation invalidates
// that position before the borrowed entry can be dereferenced again.
type scopeMember struct {
	file           *entry
	pending        bool
	previous, next *scopeMember
}

// Index supports concurrent calls. Construct it with New and do not copy it.
// Input slices must not be concurrently modified during a call. Returned slices
// are independent copies; no method retains caller-owned mutable containers.
type Index struct {
	mu                     sync.RWMutex
	owner                  *identity
	limits                 Limits
	generation             uint64
	files                  map[source.URI]*entry
	scopes                 map[Scope]*scopeState
	definitions            map[lookup]uriSet
	dependencies           map[lookup]uriSet
	symbols, uses, strings int
}

func New(limits Limits) (*Index, error) {
	if limits.Files <= 0 || limits.Symbols <= 0 || limits.References <= 0 || limits.Bytes <= 0 {
		return nil, fmt.Errorf("index: limits must be positive")
	}
	return &Index{owner: &identity{}, limits: limits, files: map[source.URI]*entry{},
		scopes: map[Scope]*scopeState{}, definitions: map[lookup]uriSet{}, dependencies: map[lookup]uriSet{}}, nil
}

// Begin invalidates locations immediately but retains old summaries for change
// comparison and dependency scheduling. Repeated Begin does not add pending
// files. Equal revisions allow configuration recomputation; lower ones fail.
func (i *Index) Begin(uri source.URI, scope Scope, revision source.Revision) (Ticket, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.owner == nil || uri == "" {
		return Ticket{}, fmt.Errorf("index: initialized index and nonempty URI required")
	}
	e := i.files[uri]
	if e != nil && revision < e.revision {
		return Ticket{}, fmt.Errorf("%w: revision decreased", ErrStaleTicket)
	}
	bytes := i.strings
	if e == nil {
		if len(i.files) >= i.limits.Files || len(uri) > i.limits.Bytes-bytes {
			return Ticket{}, ErrLimit
		}
		bytes += len(uri)
	} else {
		bytes -= len(e.scope)
	}
	if len(scope) > i.limits.Bytes-bytes {
		return Ticket{}, ErrLimit
	}
	if err := i.advance(); err != nil {
		return Ticket{}, err
	}
	if e == nil {
		e = &entry{uri: source.URI(strings.Clone(string(uri)))}
		i.files[e.uri] = e
	} else if e.scope != scope && (e.data == nil || e.data.scope != e.scope) {
		i.detach(e.scope, e.uri)
	}
	e.scope = Scope(strings.Clone(string(scope)))
	e.revision, e.serial = revision, i.generation
	i.strings = bytes + len(scope)
	i.attach(e.scope, e.uri, true)
	if e.data != nil && e.data.scope != e.scope {
		i.attach(e.data.scope, e.uri, true)
	}
	return Ticket{owner: i.owner, uri: e.uri, serial: e.serial}, nil
}

// Commit atomically replaces one file. Invalid input or a capacity failure
// leaves it pending and the ticket retryable. A successful ticket is consumed.
func (i *Index) Commit(ticket Ticket, facts Facts) (Change, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	e, err := i.ticketEntry(ticket)
	if err != nil {
		return Change{}, err
	}
	bytes, err := i.measure(facts)
	if err != nil {
		return Change{}, err
	}
	if err := validate(facts); err != nil {
		return Change{}, err
	}
	if len(e.scope) > i.limits.Bytes-bytes {
		return Change{}, ErrLimit
	}
	bytes += len(e.scope)
	if !i.fits(e.data, facts, bytes) {
		return Change{}, ErrLimit
	}
	if err := i.advance(); err != nil {
		return Change{}, err
	}
	changed := e.data == nil || e.data.scope != e.scope || !sameMeaning(e.data.facts, facts)
	affected := uriSet{e.uri: {}}
	if changed {
		i.affected(e.data, affected)
		i.affected(&stored{scope: e.scope, facts: facts}, affected)
	}
	i.forget(e)
	e.data = &stored{scope: e.scope, facts: cloneFacts(facts), bytes: bytes}
	e.serial = 0
	i.symbols += len(facts.Symbols)
	i.uses += useCount(facts)
	i.strings += bytes
	i.link(e)
	i.attach(e.scope, e.uri, false)
	return Change{i.generation, changed, sortedURIs(affected)}, nil
}

// Reuse restores the retained commit without copying its facts. The caller must
// first verify that the input bytes and all interpretation settings are unchanged.
// The original ticket must still be current, and the committed scope must match.
// ErrNoPrevious leaves the ticket available for a normal Commit after analysis.
func (i *Index) Reuse(ticket Ticket) (Change, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	e, err := i.ticketEntry(ticket)
	if err != nil {
		return Change{}, err
	}
	if e.data == nil || e.data.scope != e.scope {
		return Change{}, ErrNoPrevious
	}
	if err := i.advance(); err != nil {
		return Change{}, err
	}
	e.serial = 0
	i.attach(e.scope, e.uri, false)
	return Change{Generation: i.generation, Affected: []source.URI{e.uri}}, nil
}

// Remove releases all retained facts and budgets and invalidates pending
// tickets. Removing an unknown URI is a no-op and does not change generations.
func (i *Index) Remove(uri source.URI) (Change, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	e := i.files[uri]
	if e == nil {
		return Change{Generation: i.generation}, nil
	}
	if err := i.advance(); err != nil {
		return Change{}, err
	}
	affected := uriSet{e.uri: {}}
	i.affected(e.data, affected)
	changed := e.data != nil
	i.forget(e)
	i.detach(e.scope, e.uri)
	i.strings -= len(e.uri) + len(e.scope)
	delete(i.files, e.uri)
	return Change{i.generation, changed, sortedURIs(affected)}, nil
}

func (i *Index) ticketEntry(ticket Ticket) (*entry, error) {
	if ticket.owner == nil || ticket.owner != i.owner {
		return nil, ErrStaleTicket
	}
	e := i.files[ticket.uri]
	if e == nil || e.serial != ticket.serial {
		return nil, ErrStaleTicket
	}
	return e, nil
}

func (i *Index) advance() error {
	if i.generation == ^uint64(0) {
		return fmt.Errorf("index: generation exhausted")
	}
	i.generation++
	return nil
}

func (i *Index) attach(scope Scope, uri source.URI, pending bool) {
	s := i.scopes[scope]
	if s == nil {
		s = &scopeState{files: map[source.URI]*scopeMember{}}
		i.scopes[scope] = s
	}
	member := s.files[uri]
	if member == nil {
		member = &scopeMember{file: i.files[uri], previous: s.last}
		if s.last != nil {
			s.last.next = member
		} else {
			s.first = member
		}
		s.last = member
		s.files[uri] = member
	}
	if member.pending != pending {
		if pending {
			s.pending++
		} else {
			s.pending--
		}
	}
	member.pending = pending
	s.generation = i.generation
}

func (i *Index) detach(scope Scope, uri source.URI) {
	s := i.scopes[scope]
	if s == nil {
		return
	}
	member := s.files[uri]
	if member == nil {
		return
	}
	if member.pending {
		s.pending--
	}
	if member.previous != nil {
		member.previous.next = member.next
	} else {
		s.first = member.next
	}
	if member.next != nil {
		member.next.previous = member.previous
	} else {
		s.last = member.previous
	}
	member.previous, member.next = nil, nil
	delete(s.files, uri)
	s.generation = i.generation
	if len(s.files) == 0 {
		delete(i.scopes, scope)
	}
}

func add(table map[lookup]uriSet, key lookup, uri source.URI) {
	if table[key] == nil {
		table[key] = uriSet{}
	}
	table[key][uri] = struct{}{}
}

func drop(table map[lookup]uriSet, key lookup, uri source.URI) {
	delete(table[key], uri)
	if len(table[key]) == 0 {
		delete(table, key)
	}
}

func (i *Index) link(e *entry) {
	for _, symbol := range e.data.facts.Symbols {
		add(i.definitions, lookup{e.data.scope, symbol.Key}, e.uri)
	}
	for _, ref := range e.data.facts.References {
		add(i.dependencies, lookup{e.data.scope, ref.Target}, e.uri)
	}
	for _, key := range e.data.facts.Dependencies {
		add(i.dependencies, lookup{e.data.scope, key}, e.uri)
	}
}

func (i *Index) forget(e *entry) {
	old := e.data
	if old == nil {
		return
	}
	for _, symbol := range old.facts.Symbols {
		drop(i.definitions, lookup{old.scope, symbol.Key}, e.uri)
	}
	for _, ref := range old.facts.References {
		drop(i.dependencies, lookup{old.scope, ref.Target}, e.uri)
	}
	for _, key := range old.facts.Dependencies {
		drop(i.dependencies, lookup{old.scope, key}, e.uri)
	}
	i.symbols -= len(old.facts.Symbols)
	i.uses -= useCount(old.facts)
	i.strings -= old.bytes
	if old.scope != e.scope {
		i.detach(old.scope, e.uri)
	}
	e.data = nil
}

func (i *Index) affected(data *stored, result uriSet) {
	if data == nil {
		return
	}
	keys := make(map[Key]struct{}, len(data.facts.Symbols))
	for _, symbol := range data.facts.Symbols {
		keys[symbol.Key] = struct{}{}
	}
	for key := range keys {
		for uri := range i.dependencies[lookup{data.scope, key}] {
			result[uri] = struct{}{}
		}
	}
}

func sortedURIs(set uriSet) []source.URI {
	result := make([]source.URI, 0, len(set))
	for uri := range set {
		result = append(result, uri)
	}
	sort.Slice(result, func(a, b int) bool { return result[a] < result[b] })
	return result
}
