package workspace

import (
	"container/list"
	"fmt"
	"sync"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

type Priority uint8

const (
	Current Priority = iota
	Related
	Background
)

// Job requests work for a URI. The consumer selects its latest accepted input
// when taking the job and validates the original index ticket on completion.
type Job struct {
	URI      source.URI
	Priority Priority
}

// Queue bounds pending work, coalescing notifications for each URI.
// It owns neither goroutines nor running jobs and is safe for concurrent use.
type Queue struct {
	mu       sync.Mutex
	capacity int
	cursor   int
	lanes    [3]list.List
	pending  map[source.URI]*list.Element
}

var turns = [...]Priority{Current, Current, Current, Current, Current, Current, Current, Current, Related, Related, Related, Related, Background}

func NewQueue(capacity int) (*Queue, error) {
	if capacity <= 0 {
		return nil, fmt.Errorf("workspace: queue capacity must be positive")
	}
	return &Queue{capacity: capacity, pending: make(map[source.URI]*list.Element)}, nil
}

// Put preserves the highest requested priority for each pending URI.
// Existing jobs can be promoted at capacity. ErrLimit must cause backpressure or
// retry in the caller; it is not permission to discard a filesystem event.
func (q *Queue) Put(job Job) error {
	if job.URI == "" || job.Priority > Background {
		return fmt.Errorf("workspace: invalid job")
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if element, exists := q.pending[job.URI]; exists {
		previous := element.Value.(Job)
		if job.Priority >= previous.Priority {
			return nil
		}
		q.lanes[previous.Priority].Remove(element)
	} else if len(q.pending) >= q.capacity {
		return ErrLimit
	}
	q.pending[job.URI] = q.lanes[job.Priority].PushBack(job)
	return nil
}

// Pop uses an 8:4:1 weighted round robin, skipping empty lanes. Continuous
// foreground work cannot starve background work. FIFO holds within each lane.
// Taking a job releases its pending slot, so a newer running-file update can
// immediately be queued. The queue cannot preempt a blocking worker or syscall.
func (q *Queue) Pop() (Job, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	priority, ok := nextPriority(&q.cursor, func(priority Priority) bool { return q.lanes[priority].Len() != 0 })
	if !ok {
		return Job{}, false
	}
	element := q.lanes[priority].Front()
	job := element.Value.(Job)
	q.lanes[priority].Remove(element)
	delete(q.pending, job.URI)
	return job, true
}

// Both queue APIs share the service policy. Availability includes overflow in
// WorkQueue, so a small ready capacity cannot starve a lower-priority lane.
func nextPriority(cursor *int, available func(Priority) bool) (Priority, bool) {
	for range turns {
		priority := turns[*cursor]
		*cursor = (*cursor + 1) % len(turns)
		if available(priority) {
			return priority, true
		}
	}
	return 0, false
}

func (q *Queue) Remove(uri source.URI) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if element, exists := q.pending[uri]; exists {
		q.lanes[element.Value.(Job).Priority].Remove(element)
		delete(q.pending, uri)
	}
}

func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.pending)
}
