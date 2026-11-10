package yamlvalue

import (
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
)

// Origin follows effective value paths rather than creating a synthetic CST.
// Exact values have independent source locations. Derived values point to the
// alias/merge use; ambiguous values share an anchor with another occurrence.
type Origin struct {
	Node     yamlview.Node
	Key      yamlview.Node
	Kind     string
	Children map[string]*Origin
}

func (origin *Origin) At(parts []string) Origin {
	if origin == nil {
		return Origin{}
	}
	current := origin
	kind, site := current.Kind, current.Node
	for _, part := range parts {
		child, ok := current.Children[part]
		if !ok {
			break
		}
		current = child
		if kind == "derived" {
			continue
		}
		if current.Kind == "derived" {
			kind, site = "derived", current.Node
		} else if current.Kind == "ambiguous" {
			kind = "ambiguous"
		} else if kind == "exact" {
			site = current.Node
		}
	}
	result := *current
	result.Kind = kind
	if kind == "derived" {
		result.Node = site
	}
	return result
}

// Shared reports whether a source span belongs to an anchor used elsewhere.
func Shared(anchors map[source.Span]bool, span source.Span) bool {
	for parent := range anchors {
		if span.Start >= parent.Start && span.End <= parent.End {
			return true
		}
	}
	return false
}
