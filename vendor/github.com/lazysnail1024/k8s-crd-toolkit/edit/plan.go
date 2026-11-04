// Package edit validates text replacement plans without reading or writing files.
// It does not decide whether a proposed edit is semantically appropriate.
package edit

import (
	"fmt"
	"sort"
	"unicode/utf8"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

type Replacement struct {
	Span         source.Span
	ExpectedText string
	NewText      string
}

type Plan struct {
	BaseRevision source.Revision
	Replacements []Replacement
}

// Apply validates every replacement against base, then applies replacements in
// reverse byte order. Caller-owned slices and base remain unchanged on success
// and failure. An insertion on a replacement boundary is allowed; an insertion
// inside a replaced range and multiple insertions at one offset are rejected.
func Apply(base source.Snapshot, next source.Revision, p Plan) (source.Snapshot, error) {
	if p.BaseRevision != base.Revision() {
		return source.Snapshot{}, fmt.Errorf("edit: base revision does not match")
	}
	if next <= base.Revision() {
		return source.Snapshot{}, fmt.Errorf("edit: next revision must increase")
	}
	replacements := append([]Replacement(nil), p.Replacements...)
	for i, r := range replacements {
		text, err := base.Slice(r.Span)
		if err != nil {
			return source.Snapshot{}, fmt.Errorf("edit: replacement %d: %w", i, err)
		}
		if text != r.ExpectedText {
			return source.Snapshot{}, fmt.Errorf("edit: replacement %d: original text does not match", i)
		}
		if !utf8.ValidString(r.NewText) {
			return source.Snapshot{}, fmt.Errorf("edit: replacement %d: invalid UTF-8", i)
		}
	}
	sort.Slice(replacements, func(i, j int) bool {
		a, b := replacements[i].Span, replacements[j].Span
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		return a.End < b.End
	})
	for i := 1; i < len(replacements); i++ {
		a, b := replacements[i-1].Span, replacements[i].Span
		if b.Start < a.End || (a.Start == a.End && b.Start == b.End && a.Start == b.Start) {
			return source.Snapshot{}, fmt.Errorf("edit: replacements overlap or insert at the same offset")
		}
	}
	changes := make([]source.Change, 0, len(replacements))
	for i := len(replacements) - 1; i >= 0; i-- {
		r := replacements[i]
		changes = append(changes, source.Change{Range: r.Span, Text: r.NewText})
	}
	return base.Apply(next, changes)
}
