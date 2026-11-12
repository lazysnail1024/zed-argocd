package index

import (
	"container/heap"
	"context"
	"fmt"
	"runtime"
	"strings"
	"time"
)

const symbolQueryBatch = 128

type symbolCursor struct {
	member *scopeMember
	symbol int // -1 means this file has not yet consumed its file visit.
}

// SymbolsWithPrefixContext visits known fresh symbols using bounded lock-held
// batches. It does not copy all scope URIs, read source text, or call consumer
// code under the index lock. limit bounds results; maxWork separately bounds
// visited files plus symbols, whether or not they match. Both must be positive.
//
// Success means Complete=true for this scope generation, not complete discovery.
// Result truncation still allows success. Cancellation, budget exhaustion, or a
// mutation of the queried scope returns no Items and Complete=false; unrelated
// scopes do not invalidate the traversal. Callers must still revalidate before
// publishing or using a result as part of a larger correctness-sensitive action.
func (i *Index) SymbolsWithPrefixContext(ctx context.Context, scope Scope, prefix Key, query string, limit, maxWork int) (SymbolQueryResult, error) {
	result := SymbolQueryResult{}
	if ctx == nil || limit <= 0 || maxWork <= 0 {
		return result, fmt.Errorf("index: context and positive result/work limits required")
	}
	if err := i.lockSymbolQuery(ctx); err != nil {
		return result, err
	}
	cursor := symbolCursor{symbol: -1}
	if state := i.scopes[scope]; state != nil {
		result.State = State{Generation: state.generation, Pending: state.pending}
		cursor.member = state.first
	}
	i.mu.RUnlock()
	kept := &symbolHeap{&SymbolResult{Items: []Definition{}}}
	var storage [symbolQueryBatch]Definition
	for cursor.member != nil {
		items, used, err := i.symbolBatch(ctx, scope, result.State.Generation, &cursor, maxWork-result.Work, storage[:0])
		result.Work += used
		if err != nil {
			return result, err
		}
		if err := keepQuerySymbols(ctx, kept, items, prefix, query, limit); err != nil {
			return result, err
		}
		clear(storage[:])
		runtime.Gosched()
	}
	items, err := finishQuerySymbols(ctx, kept)
	if err != nil {
		return result, err
	}
	if err := i.lockSymbolQuery(ctx); err != nil {
		return result, err
	}
	valid := i.symbolScopeUnchanged(scope, result.State.Generation)
	i.mu.RUnlock()
	if !valid {
		return result, ErrQueryChanged
	}
	if err := ctx.Err(); err != nil {
		return result, err
	}
	result.Items, result.Truncated, result.Complete = items, kept.Truncated, true
	return result, nil
}

// Waiting for an index writer must not turn a canceled request into an
// uncancelable RWMutex wait. The timer only backs off failed acquisition; it
// does not run a worker or hold any index lock while waiting.
func (i *Index) lockSymbolQuery(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if i.mu.TryRLock() {
		return nil
	}
	timer := time.NewTicker(time.Millisecond)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
			if err := ctx.Err(); err != nil {
				return err
			}
			if i.mu.TryRLock() {
				return nil
			}
		}
	}
}

// Caller holds the read lock. Generations never wrap or repeat, including when
// a scope is removed/recreated; an absent scope has generation zero.
func (i *Index) symbolScopeUnchanged(scope Scope, generation uint64) bool {
	current := i.scopes[scope]
	return current == nil && generation == 0 || current != nil && current.generation == generation
}

func (i *Index) symbolBatch(ctx context.Context, scope Scope, generation uint64, cursor *symbolCursor, budget int, items []Definition) ([]Definition, int, error) {
	if err := i.lockSymbolQuery(ctx); err != nil {
		return nil, 0, err
	}
	defer i.mu.RUnlock()
	if !i.symbolScopeUnchanged(scope, generation) {
		return nil, 0, ErrQueryChanged
	}
	used := 0
	for cursor.member != nil && used < symbolQueryBatch {
		if used == budget {
			return nil, used, ErrQueryBudget
		}
		file := cursor.member.file
		if cursor.symbol == -1 {
			used++ // A pending or symbol-free file still costs one visit.
			cursor.symbol = 0
		} else {
			used++ // Every symbol costs one visit, even a prefix/name miss.
			items = append(items, Definition{file.uri, file.revision, file.data.facts.Symbols[cursor.symbol]})
			cursor.symbol++
		}
		if file == nil || file.serial != 0 || cursor.symbol == len(file.data.facts.Symbols) {
			cursor.member, cursor.symbol = cursor.member.next, -1
		}
	}
	return items, used, nil
}

func keepQuerySymbols(ctx context.Context, kept *symbolHeap, items []Definition, prefix Key, query string, limit int) error {
	for _, item := range items {
		if err := ctx.Err(); err != nil {
			return err
		}
		if !strings.HasPrefix(string(item.Symbol.Key), string(prefix)) || !strings.Contains(item.Symbol.Name, query) {
			continue
		}
		if kept.Len() < limit {
			heap.Push(kept, item)
			continue
		}
		kept.Truncated = true
		if definitionLess(item, kept.Items[0]) {
			kept.Items[0] = item
			heap.Fix(kept, 0)
		}
	}
	return ctx.Err()
}

// Reuse the result heap's backing array. Each ordering step remains outside
// locks and checks cancellation; no second full result array is allocated.
func finishQuerySymbols(ctx context.Context, kept *symbolHeap) ([]Definition, error) {
	items := kept.Items
	for kept.Len() > 0 {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		n := kept.Len() - 1
		item := heap.Pop(kept).(Definition)
		items[n] = item
	}
	return items, ctx.Err()
}
