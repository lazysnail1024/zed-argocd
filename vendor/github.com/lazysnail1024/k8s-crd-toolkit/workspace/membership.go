package workspace

import "sync"

// Membership coordinates scan passes without retaining a second file registry.
// Keep one Observation in each consumer-owned file entry. The zero value is
// usable; methods are concurrent-safe. Do not copy Membership after use.
type Membership struct {
	mu      sync.RWMutex
	current *membershipPass
}

type membershipPass struct {
	owner            *Membership
	active, complete bool
}

type ScanPass struct{ state *membershipPass }
type Observation struct{ state *membershipPass }

// Start supersedes every earlier pass, including its deletion candidates.
func (m *Membership) Start() ScanPass {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.current = &membershipPass{owner: m, active: true}
	return ScanPass{m.current}
}

// Observe marks an input as present in the current pass. Call it both for scan
// discoveries and authoritative watcher/input events, including newly added
// files in an already-scanned directory. Before Start it returns a zero mark.
// Store the mark under the same consumer lock used to sweep the file registry.
func (m *Membership) Observe() Observation {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return Observation{m.current}
}

func (m *Membership) Seen(observed Observation) bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.current != nil && observed.state == m.current
}

// Finish consumes the current active pass. Only successful complete enumeration
// enables Missing; failed, canceled, superseded and repeated finishes do not.
// Completeness of domain analysis remains the consumer's responsibility.
func (m *Membership) Finish(pass ScanPass, complete bool) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if pass.state == nil || m.current != pass.state || !pass.state.active {
		return false
	}
	pass.state.active, pass.state.complete = false, complete
	return complete
}

// Missing permits a deletion candidate only after this pass finished completely
// and while it is still current. It never treats a newer watcher observation as
// missing. Consumers additionally protect open buffers and apply their policy.
func (pass ScanPass) Missing(observed Observation) bool {
	if pass.state == nil {
		return false
	}
	m := pass.state.owner
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.current == pass.state && !pass.state.active && pass.state.complete && observed.state != pass.state
}
