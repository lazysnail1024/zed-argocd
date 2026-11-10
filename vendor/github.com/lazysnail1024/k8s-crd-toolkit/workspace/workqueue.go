package workspace

import (
	"container/list"
	"fmt"
	"sync"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// WorkTicket reserves one URI until Complete, including after Remove. Its zero
// value is invalid. Call Complete on every exit path after a successful Pop.
type WorkTicket struct {
	URI      source.URI
	Priority Priority
	owner    *WorkQueue
	run      *workRun
}

type workRun struct{ retired bool }
type workEntry struct {
	job     Job
	waiting bool
	run     *workRun
	element *list.Element
	ready   bool
}

// WorkQueue retains coalesced notifications, overflow and running reservations.
// It is safe for concurrent use, owns no workers, and must not be copied.
type WorkQueue struct {
	mu                       sync.Mutex
	readyCapacity, capacity  int
	readyCount, waitingCount int
	cursor                   int
	ready, overflow          [3]list.List
	entries                  map[source.URI]*workEntry
	wake                     chan struct{}
}

func NewWorkQueue(readyCapacity, outstandingCapacity int) (*WorkQueue, error) {
	if readyCapacity <= 0 || outstandingCapacity < readyCapacity {
		return nil, fmt.Errorf("workspace: positive ready capacity within outstanding capacity required")
	}
	return &WorkQueue{readyCapacity: readyCapacity, capacity: outstandingCapacity,
		entries: make(map[source.URI]*workEntry), wake: make(chan struct{}, 1)}, nil
}

// Put preserves the highest priority of unstarted notifications. A notification
// received during a run requests another run; it never permits concurrent work
// on that URI. Only the distinct outstanding URI limit returns ErrLimit.
func (q *WorkQueue) Put(job Job) error {
	if job.URI == "" || job.Priority > Background {
		return fmt.Errorf("workspace: invalid job")
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	e := q.entries[job.URI]
	if e == nil {
		if len(q.entries) == q.capacity {
			return ErrLimit
		}
		e = &workEntry{job: job}
		q.entries[job.URI] = e
	}
	if e.waiting && job.Priority >= e.job.Priority {
		return nil
	}
	if e.element != nil {
		q.detach(e)
	}
	if !e.waiting {
		e.waiting = true
		q.waitingCount++
	}
	e.job.Priority = job.Priority
	if e.run == nil {
		q.place(e, false, false)
	}
	q.refill()
	return nil
}

// Pop applies the same 8:4:1 service as Queue to all eligible notifications,
// including overflow. FIFO holds within a lane. A selected overflow job moves
// directly into execution, so even ready capacity one preserves fair service.
func (q *WorkQueue) Pop() (WorkTicket, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	priority, ok := nextPriority(&q.cursor, func(p Priority) bool {
		return q.ready[p].Len() != 0 || q.overflow[p].Len() != 0
	})
	if !ok {
		return WorkTicket{}, false
	}
	element := q.ready[priority].Front()
	if element == nil {
		element = q.overflow[priority].Front()
	}
	e := element.Value.(*workEntry)
	q.detach(e)
	e.waiting = false
	q.waitingCount--
	e.run = &workRun{}
	ticket := WorkTicket{URI: e.job.URI, Priority: priority, owner: q, run: e.run}
	q.refill()
	return ticket, true
}

// Complete releases exactly this execution reservation, preserving notifications
// accepted during it. A removed run returns false but still releases its retired
// reservation, allowing a recreated URI to run. Replayed/foreign tickets do
// nothing. The result is not a verdict on analysis or publication validity.
func (q *WorkQueue) Complete(ticket WorkTicket) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	e := q.entries[ticket.URI]
	if ticket.owner != q || ticket.run == nil || e == nil || e.run != ticket.run {
		return false
	}
	accepted := !e.run.retired
	e.run = nil
	if e.waiting {
		q.place(e, false, false)
	} else {
		delete(q.entries, ticket.URI)
	}
	q.refill()
	return accepted
}

// Remove discards unstarted notifications. A running URI remains reserved until
// its worker calls Complete; recreation cannot overlap that retired execution.
func (q *WorkQueue) Remove(uri source.URI) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if e := q.entries[uri]; e != nil {
		if e.element != nil {
			q.detach(e)
		}
		if e.waiting {
			e.waiting = false
			q.waitingCount--
		}
		if e.run != nil {
			e.run.retired = true
		} else {
			delete(q.entries, uri)
		}
		q.refill()
	}
}

// Pending includes waiting notifications and running reservations, even retired
// runs whose workers have not exited. Waiting distinguishes unstarted work.
func (q *WorkQueue) Pending(uri source.URI) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.entries[uri] != nil
}

func (q *WorkQueue) Running(uri source.URI) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.entries[uri] != nil && q.entries[uri].run != nil
}

func (q *WorkQueue) Waiting(uri source.URI) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.entries[uri] != nil && q.entries[uri].waiting
}

// Len counts ready notifications, bounded by readyCapacity. Backlog counts all
// other waiting notifications, including another notification for a running URI.
func (q *WorkQueue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.readyCount
}

func (q *WorkQueue) Backlog() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.waitingCount - q.readyCount
}

// Wake supplies coalesced hints, not jobs; it is never closed. Workers Pop first
// and wait on Wake or their own cancellation only when no job is available.
func (q *WorkQueue) Wake() <-chan struct{} { return q.wake }

func (q *WorkQueue) detach(e *workEntry) {
	if e.ready {
		q.ready[e.job.Priority].Remove(e.element)
		q.readyCount--
	} else {
		q.overflow[e.job.Priority].Remove(e.element)
	}
	e.element = nil
}

func (q *WorkQueue) place(e *workEntry, ready, front bool) {
	lane := &q.overflow[e.job.Priority]
	if ready {
		lane = &q.ready[e.job.Priority]
		q.readyCount++
	}
	e.ready = ready
	if front {
		e.element = lane.PushFront(e)
	} else {
		e.element = lane.PushBack(e)
	}
}

func (q *WorkQueue) refill() {
	for priority := Current; priority <= Background; priority++ {
		for q.overflow[priority].Len() != 0 {
			if q.readyCount == q.readyCapacity && !q.displaceLower(priority) {
				break
			}
			e := q.overflow[priority].Front().Value.(*workEntry)
			q.detach(e)
			q.place(e, true, false)
		}
	}
	if q.readyCount != 0 {
		select {
		case q.wake <- struct{}{}:
		default:
		}
	}
}

func (q *WorkQueue) displaceLower(priority Priority) bool {
	for lower := Background; lower > priority; lower-- {
		if element := q.ready[lower].Back(); element != nil {
			e := element.Value.(*workEntry)
			q.detach(e)
			q.place(e, false, true)
			return true
		}
	}
	return false
}
