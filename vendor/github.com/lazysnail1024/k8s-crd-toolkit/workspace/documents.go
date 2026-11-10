// Package workspace provides bounded input and scheduling mechanisms. It does
// not recognize document contents, interpret URIs, run workers, or implement LSP.
package workspace

import (
	"errors"
	"fmt"
	"sync"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

var (
	ErrStale   = errors.New("workspace: stale input")
	ErrLimit   = errors.New("workspace: capacity exceeded")
	ErrOverlay = errors.New("workspace: open buffer is authoritative")
)

// DocumentLimits bounds retained entries, one input, and all open buffers.
// Entries includes outstanding disk reads. Closed-file text is never retained.
type DocumentLimits struct{ Entries, FileBytes, OpenBytes int }

type document struct {
	read uint64
	open *source.Snapshot
}

// ReadTicket authorizes one disk result. Its zero value is invalid.
type ReadTicket struct {
	owner *Documents
	uri   source.URI
	read  uint64
}

// Documents is safe for concurrent use and must not be copied after use.
// It assigns revisions independently of editor versions, including reopen.
type Documents struct {
	mu      sync.RWMutex
	limits  DocumentLimits
	serial  uint64
	bytes   int
	entries map[source.URI]document
}

func NewDocuments(limits DocumentLimits) (*Documents, error) {
	if limits.Entries <= 0 || limits.FileBytes <= 0 || limits.OpenBytes <= 0 {
		return nil, fmt.Errorf("workspace: document limits must be positive")
	}
	return &Documents{limits: limits, entries: make(map[source.URI]document)}, nil
}

func (d *Documents) next() (uint64, error) {
	if d.serial == ^uint64(0) {
		return 0, ErrLimit
	}
	d.serial++
	return d.serial, nil
}

func (d *Documents) admit(uri source.URI) error {
	if uri == "" {
		return fmt.Errorf("workspace: empty URI")
	}
	if _, exists := d.entries[uri]; !exists && len(d.entries) >= d.limits.Entries {
		return ErrLimit
	}
	return nil
}

// Open establishes an authoritative buffer and invalidates earlier disk reads.
// An already open buffer must be updated with Change instead.
func (d *Documents) Open(uri source.URI, text string) (source.Snapshot, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if err := d.admit(uri); err != nil {
		return source.Snapshot{}, err
	}
	if d.entries[uri].open != nil {
		return source.Snapshot{}, ErrOverlay
	}
	if len(text) > d.limits.FileBytes || len(text) > d.limits.OpenBytes-d.bytes {
		return source.Snapshot{}, ErrLimit
	}
	next, err := d.next()
	if err != nil {
		return source.Snapshot{}, err
	}
	s, err := source.New(uri, source.Revision(next), text)
	if err != nil {
		return source.Snapshot{}, err
	}
	d.entries[uri] = document{open: &s}
	d.bytes += len(text)
	return s, nil
}

// Change uses sequential source.Change coordinates. All preconditions and
// intermediate sizes are checked before the authoritative buffer is replaced.
func (d *Documents) Change(uri source.URI, base source.Revision, changes []source.Change) (source.Snapshot, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	current := d.entries[uri].open
	if current == nil || current.Revision() != base {
		return source.Snapshot{}, ErrStale
	}
	available := d.limits.OpenBytes - (d.bytes - len(current.Text()))
	if available > d.limits.FileBytes {
		available = d.limits.FileBytes
	}
	size := len(current.Text())
	for _, change := range changes {
		if change.Range.Start < 0 || change.Range.End < change.Range.Start || change.Range.End > size {
			return source.Snapshot{}, fmt.Errorf("workspace: invalid change range")
		}
		size -= change.Range.End - change.Range.Start
		if len(change.Text) > available-size {
			return source.Snapshot{}, ErrLimit
		}
		size += len(change.Text)
	}
	next, err := d.next()
	if err != nil {
		return source.Snapshot{}, err
	}
	s, err := current.Apply(source.Revision(next), changes)
	if err != nil {
		return source.Snapshot{}, err
	}
	d.bytes += len(s.Text()) - len(current.Text())
	d.entries[uri] = document{open: &s}
	return s, nil
}

// OpenBytes returns the bytes retained by the authoritative open buffers.
// A caller using it to reserve a separate read budget must still fence changes
// to those buffers while accepting its read results.
func (d *Documents) OpenBytes() int {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return d.bytes
}

func (d *Documents) Snapshot(uri source.URI) (source.Snapshot, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if s := d.entries[uri].open; s != nil {
		return *s, true
	}
	return source.Snapshot{}, false
}

// BeginRead reserves the newest disk read. ErrOverlay tells a worker to use
// Snapshot instead of reading disk. Reads happen outside Documents' lock.
func (d *Documents) BeginRead(uri source.URI) (ReadTicket, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if err := d.admit(uri); err != nil {
		return ReadTicket{}, err
	}
	if d.entries[uri].open != nil {
		return ReadTicket{}, ErrOverlay
	}
	next, err := d.next()
	if err != nil {
		return ReadTicket{}, err
	}
	d.entries[uri] = document{read: next}
	return ReadTicket{owner: d, uri: uri, read: next}, nil
}

// PublishRead consumes a valid ticket and returns a snapshot without caching its
// text. Invalid input also consumes the ticket; retry with a fresh BeginRead.
// The reader must bound its I/O before allocating text, not only at this call.
func (d *Documents) PublishRead(ticket ReadTicket, text string) (source.Snapshot, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	entry, exists := d.entries[ticket.uri]
	if ticket.owner != d || !exists || entry.open != nil || ticket.read == 0 || entry.read != ticket.read {
		return source.Snapshot{}, ErrStale
	}
	delete(d.entries, ticket.uri)
	if len(text) > d.limits.FileBytes {
		return source.Snapshot{}, ErrLimit
	}
	return source.New(ticket.uri, source.Revision(ticket.read), text)
}

// DiscardRead abandons only the read represented by ticket. A failed or canceled
// older worker cannot revoke a newer read or an authoritative open buffer.
// It reports whether a pending read was actually released.
func (d *Documents) DiscardRead(ticket ReadTicket) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	entry, exists := d.entries[ticket.uri]
	if ticket.owner != d || !exists || entry.open != nil || ticket.read == 0 || entry.read != ticket.read {
		return false
	}
	delete(d.entries, ticket.uri)
	return true
}

// Invalidate revokes an in-flight disk read after an authoritative disk change
// or deletion. Workers abandoning a read must use DiscardRead instead.
// An open buffer remains authoritative even when its disk file was deleted.
func (d *Documents) Invalidate(uri source.URI) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.entries[uri].open == nil {
		delete(d.entries, uri)
	}
}

// Close releases the buffer and revokes reads. It never restores cached disk
// text: a consumer must obtain a fresh read, or remove facts if the file is gone.
// The result reports whether a buffer was open. This also handles root removal.
func (d *Documents) Close(uri source.URI) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	entry := d.entries[uri]
	if entry.open != nil {
		d.bytes -= len(entry.open.Text())
	}
	delete(d.entries, uri)
	return entry.open != nil
}
