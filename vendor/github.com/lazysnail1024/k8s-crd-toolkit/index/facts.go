package index

import (
	"fmt"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

func useCount(f Facts) int { return len(f.References) + len(f.Dependencies) }

func (i *Index) measure(f Facts) (int, error) {
	if len(f.Symbols) > i.limits.Symbols || len(f.References) > i.limits.References || len(f.Dependencies) > i.limits.References-len(f.References) {
		return 0, ErrLimit
	}
	bytes := 0
	add := func(text string) bool {
		if len(text) > i.limits.Bytes-bytes {
			return false
		}
		bytes += len(text)
		return true
	}
	for _, s := range f.Symbols {
		if !add(string(s.ID)) || !add(string(s.Key)) || !add(s.Name) || !add(s.Kind) {
			return 0, ErrLimit
		}
	}
	for _, r := range f.References {
		if !add(string(r.Target)) {
			return 0, ErrLimit
		}
	}
	for _, key := range f.Dependencies {
		if !add(string(key)) {
			return 0, ErrLimit
		}
	}
	return bytes, nil
}

func (i *Index) fits(old *stored, f Facts, bytes int) bool {
	symbols, uses, strings := i.symbols, i.uses, i.strings
	if old != nil {
		symbols -= len(old.facts.Symbols)
		uses -= useCount(old.facts)
		strings -= old.bytes
	}
	return len(f.Symbols) <= i.limits.Symbols-symbols && useCount(f) <= i.limits.References-uses && bytes <= i.limits.Bytes-strings
}

func validSpan(span source.Span) bool { return span.Start >= 0 && span.End >= span.Start }

func validate(f Facts) error {
	ids := make(map[SymbolID]struct{}, len(f.Symbols))
	for _, s := range f.Symbols {
		if s.ID == "" || s.Key == "" || !validSpan(s.Span) || !validSpan(s.Selection) || s.Selection.Start < s.Span.Start || s.Selection.End > s.Span.End {
			return fmt.Errorf("index: invalid symbol identity or span")
		}
		if _, exists := ids[s.ID]; exists {
			return fmt.Errorf("index: duplicate symbol ID")
		}
		ids[s.ID] = struct{}{}
	}
	for _, r := range f.References {
		if r.Target == "" || !validSpan(r.Span) {
			return fmt.Errorf("index: invalid reference target or span")
		}
	}
	for _, key := range f.Dependencies {
		if key == "" {
			return fmt.Errorf("index: empty dependency key")
		}
	}
	return nil
}

func cloneFacts(f Facts) Facts {
	next := Facts{SemanticDigest: f.SemanticDigest, Symbols: append([]Symbol(nil), f.Symbols...), References: append([]Reference(nil), f.References...), Dependencies: append([]Key(nil), f.Dependencies...)}
	for n, s := range next.Symbols {
		s.ID, s.Key = SymbolID(strings.Clone(string(s.ID))), Key(strings.Clone(string(s.Key)))
		s.Name, s.Kind = strings.Clone(s.Name), strings.Clone(s.Kind)
		next.Symbols[n] = s
	}
	for n, r := range next.References {
		r.Target = Key(strings.Clone(string(r.Target)))
		next.References[n] = r
	}
	for n, key := range next.Dependencies {
		next.Dependencies[n] = Key(strings.Clone(string(key)))
	}
	return next
}

// Comparison is order independent and deliberately excludes source positions.
func sameMeaning(a, b Facts) bool {
	if a.SemanticDigest != b.SemanticDigest || len(a.Symbols) != len(b.Symbols) || len(a.References) != len(b.References) || len(a.Dependencies) != len(b.Dependencies) {
		return false
	}
	symbols := make(map[SymbolID]Symbol, len(a.Symbols))
	for _, s := range a.Symbols {
		s.Span, s.Selection = source.Span{}, source.Span{}
		symbols[s.ID] = s
	}
	for _, s := range b.Symbols {
		s.Span, s.Selection = source.Span{}, source.Span{}
		if symbols[s.ID] != s {
			return false
		}
	}
	refs := make(map[Reference]int, len(a.References))
	for _, r := range a.References {
		r.Span = source.Span{}
		refs[r]++
	}
	for _, r := range b.References {
		r.Span = source.Span{}
		if refs[r] == 0 {
			return false
		}
		refs[r]--
	}
	deps := make(map[Key]int, len(a.Dependencies))
	for _, key := range a.Dependencies {
		deps[key]++
	}
	for _, key := range b.Dependencies {
		if deps[key] == 0 {
			return false
		}
		deps[key]--
	}
	return true
}
