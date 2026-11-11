package analysis

import (
	"context"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlvalue"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
)

const maxProjectionNodes = 100000

type valueOrigin = yamlvalue.Origin
type projectionError = yamlvalue.Error

type projectedValue struct {
	Value         any
	Origin        *valueOrigin
	Nodes         int
	IdentityKnown bool
	Owned         bool
}

func projectValues(ctx context.Context, snapshot source.Snapshot, doc yamlview.Document) (projectedValue, error) {
	return projectValuesWithin(ctx, snapshot, doc, maxProjectionNodes)
}

func projectValuesWithin(ctx context.Context, snapshot source.Snapshot, doc yamlview.Document, allowance int) (projectedValue, error) {
	projected, err := yamlvalue.Project(ctx, snapshot, doc, yamlvalue.Options{MaxNodes: allowance, Fields: []string{"apiVersion", "kind"}})
	api, kind := projected.Fields["apiVersion"], projected.Fields["kind"]
	ownedKind := kind == "Application" || kind == "AppProject" || kind == "ApplicationSet"
	owned := api == "argoproj.io/v1alpha1" && ownedKind
	known := owned || (api != "" && api != "argoproj.io/v1alpha1") || (kind != "" && !ownedKind)
	return projectedValue{Value: projected.Value, Origin: projected.Origin, Nodes: projected.Nodes, IdentityKnown: known, Owned: owned}, err
}

func (doc Document) origin(parts []string) valueOrigin {
	if kind, ok := doc.compactOrigin(parts); ok {
		return valueOrigin{Kind: kind}
	}
	for length := len(parts); length >= 0; length-- {
		if node, ok := doc.OverrideOrigins[strings.Join(parts[:length], "/")]; ok {
			return valueOrigin{Node: node, Kind: "derived"}
		}
	}
	// Container diagnostics may depend on a replaced descendant.
	for path, node := range doc.OverrideOrigins {
		if strings.HasPrefix(path, strings.Join(parts, "/")+"/") {
			return valueOrigin{Node: node, Kind: "derived"}
		}
	}

	if doc.Origin == nil {
		return valueOrigin{Node: locate(doc.Root, parts), Kind: "exact"}
	}
	return doc.Origin.At(parts)
}
func (doc Document) exact(parts []string) bool             { return doc.origin(parts).Kind == "exact" }
func (doc Document) location(parts []string) yamlview.Node { return doc.origin(parts).Node }
func (doc Document) locationMessage(parts []string, message string) string {
	kind := doc.origin(parts).Kind
	if kind == "derived" {
		return message + " (derived YAML value; shown at alias/merge use)"
	}
	if kind == "ambiguous" {
		return message + " (shared anchor value; not an independent edit target)"
	}
	return message
}

func (doc Document) originBelow(parts []string) *valueOrigin {
	if doc.Origin == nil {
		return nil
	}
	origin := doc.origin(parts)
	return &origin
}
