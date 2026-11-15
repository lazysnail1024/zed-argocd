// Package worktrace records performed product work only when explicitly enabled.
// A recorder belongs to one request or background invocation. It does not derive
// attribution from process-global counters and never retains source/config text.
package worktrace

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"
)

type key struct{}

// This switch is only a disabled fast path, never an attribution counter.
// A normal process that never creates a recorder skips context traversal.
var armed atomic.Bool

var common = []string{"readAttempts", "readFiles", "readBytes", "readFailures", "parseCalls", "parseBytes", "projectionCalls", "configDecodes", "schemaLoads", "schemaCompiles", "schemaQueries", "domainPasses", "domainResources", "factsRestored"}

// Writer serializes detailed worker records with the existing owner trace lines.
// The server installs it only for opt-in tracing; protocol output is unrelated.
type Writer struct {
	mu     sync.Mutex
	Out    io.Writer
	closed bool
}

func (w *Writer) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return len(p), nil
	}
	return w.Out.Write(p)
}

func (w *Writer) Close() {
	w.mu.Lock()
	w.closed = true
	w.mu.Unlock()
}

type Record struct {
	SchemaVersion int              `json:"schemaVersion"`
	Sequence      uint64           `json:"sequence"`
	Origin        string           `json:"origin"`
	Method        string           `json:"method,omitempty"`
	RequestID     json.RawMessage  `json:"requestId,omitempty"`
	Event         string           `json:"event"`
	Status        string           `json:"status,omitempty"`
	Work          map[string]int64 `json:"work,omitempty"`
	Timing        map[string]int64 `json:"timing"`
}

type Recorder struct {
	mu                                                                  sync.Mutex
	base                                                                Record
	writer                                                              io.Writer
	admitted, received, started, finished, cancelled, replied, released time.Time
	work                                                                map[string]int64
}

// New is called only when tracing is enabled. IDs are optional and bounded;
// sequence always identifies the work span, including clients reusing IDs.
func New(ctx context.Context, writer io.Writer, sequence uint64, origin, method string, id json.RawMessage, received time.Time) (context.Context, *Recorder) {
	if writer == nil {
		return ctx, nil
	}
	armed.Store(true)
	if len(id) > 128 {
		id = nil
	}
	r := &Recorder{writer: writer, admitted: time.Now(), received: received,
		base: Record{SchemaVersion: 1, Sequence: sequence, Origin: origin, Method: method, RequestID: append(json.RawMessage(nil), id...)}, work: map[string]int64{}}
	for _, name := range common {
		r.work[name] = 0
	}
	return context.WithValue(ctx, key{}, r), r
}

func From(ctx context.Context) *Recorder {
	if !armed.Load() || ctx == nil {
		return nil
	}
	r, _ := ctx.Value(key{}).(*Recorder)
	return r
}

// Carry attaches measurement ownership without changing the recipient's
// cancellation/deadline semantics. A legacy nil recipient still means a
// Background context to its caller; only an enabled recorder adds a value.
func Carry(recipient, owner context.Context) context.Context {
	r := From(owner)
	if r == nil {
		return recipient
	}
	if recipient == nil {
		recipient = context.Background()
	}
	return context.WithValue(recipient, key{}, r)
}

func Add(ctx context.Context, name string, n int64) {
	if r := From(ctx); r != nil {
		r.mu.Lock()
		r.work[name] += n
		r.mu.Unlock()
	}
}

var noop = func() {}

// Measure counts entry even when the operation later fails or is cancelled.
// Durations are inclusive elapsed time at a product boundary, not CPU time.
func Measure(ctx context.Context, count, duration string) func() {
	r := From(ctx)
	if r == nil {
		return noop
	}
	r.mu.Lock()
	r.work[count]++
	r.mu.Unlock()
	start := time.Now()
	return func() {
		r.mu.Lock()
		r.work[duration] += time.Since(start).Nanoseconds()
		r.mu.Unlock()
	}
}

// Read records one bounded acquisition invocation, including preflight failure.
// Physical bytes and successful ReadAll calls are recorded where they happen.
func Read(ctx context.Context) func(error) {
	if From(ctx) == nil {
		return func(error) {}
	}
	done := Measure(ctx, "readAttempts", "readNs")
	return func(err error) {
		done()
		if err != nil {
			Add(ctx, "readFailures", 1)
		}
	}
}

func ReadBytes(ctx context.Context, bytes int, err error) {
	Add(ctx, "readBytes", int64(bytes))
	if err == nil {
		Add(ctx, "readFiles", 1)
	}
}

func Queue(ctx context.Context, elapsed time.Duration) {
	if r := From(ctx); r != nil {
		r.mu.Lock()
		r.work["queueJobs"]++
		r.work["queueWaitNs"] += elapsed.Nanoseconds()
		r.work["queueWaitMaxNs"] = max(r.work["queueWaitMaxNs"], elapsed.Nanoseconds())
		r.mu.Unlock()
	}
}

func (r *Recorder) Start() {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.started = time.Now()
	r.mu.Unlock()
	r.emit("started", "", false)
}

func (r *Recorder) Finish() {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.finished = time.Now()
	r.mu.Unlock()
}

func (r *Recorder) Cancel() {
	if r == nil {
		return
	}
	r.mu.Lock()
	if r.cancelled.IsZero() {
		r.cancelled = time.Now()
	}
	r.mu.Unlock()
}

func (r *Recorder) Reply(status string) {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.replied = time.Now()
	r.mu.Unlock()
	r.emit("reply", status, false)
}

// Release is called after removing the actual slot reservation. The reply may
// precede or follow it; separate events preserve that ordering without guessing.
func (r *Recorder) Release(status string) {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.released = time.Now()
	r.mu.Unlock()
	r.emit("worker-released", status, true)
}

func (r *Recorder) Complete(status string) {
	if r == nil {
		return
	}
	r.emit("completed", status, true)
}

func (r *Recorder) Publish(status string) {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.replied = time.Now()
	r.mu.Unlock()
	r.emit("publication", status, false)
}

func (r *Recorder) emit(event, status string, withWork bool) {
	r.mu.Lock()
	record := r.base
	record.Event, record.Status = event, status
	record.Timing = map[string]int64{}
	add := func(name string, start, end time.Time) {
		if !start.IsZero() && !end.IsZero() {
			record.Timing[name] = max(int64(0), end.Sub(start).Nanoseconds())
		}
	}
	add("ownerWaitNs", r.received, r.admitted)
	add("dispatchNs", r.admitted, r.started)
	add("serviceNs", r.started, r.finished)
	add("admitToReplyNs", r.admitted, r.replied)
	add("admitToReleaseNs", r.admitted, r.released)
	add("cancelToReplyNs", r.cancelled, r.replied)
	add("cancelToReleaseNs", r.cancelled, r.released)
	add("deliveryNs", r.finished, r.replied)
	if withWork {
		record.Work = make(map[string]int64, len(r.work))
		for name, count := range r.work {
			record.Work[name] = count
		}
	}
	r.mu.Unlock()
	data, err := json.Marshal(record)
	if err == nil {
		_, _ = fmt.Fprintf(r.writer, "argocd-work %s\n", data)
	}
}
