package analysis

import (
	"context"
	"strconv"
	"strings"

	yamlv3 "gopkg.in/yaml.v3"
)

// ApplicationSet inspection copies an effective template for each top-level
// generator. Bound that work before constructing the copies, not after the
// compact index summary has already discarded their large valuesObject fields.
// These are logical expansion limits; they are not a process RSS measurement.
const maxApplicationSetExpansionBytes = 64 * 1024 * 1024

type applicationSetCost struct {
	nodes int
	bytes int
}

func applicationSetExpansion(ctx context.Context, value map[string]any, remaining, remainingBytes int) (applicationSetCost, error) {
	if err := ctx.Err(); err != nil {
		return applicationSetCost{}, err
	}
	spec := object(value["spec"])
	goTemplate, _ := spec["goTemplate"].(bool)
	base, err := applicationSetValueCost(ctx, spec["template"], false, goTemplate, false, remaining)
	if err != nil {
		return applicationSetCost{}, err
	}
	patchCost := applicationSetCost{}
	if patch, ok := spec["templatePatch"].(string); ok {
		patchCost.bytes = len(patch)
		dynamic := goTemplate && strings.Contains(patch, "{{") || !goTemplate && fastTemplateMarker.MatchString(patch)
		if !dynamic {
			// Inspect the syntax graph without expanding aliases or converting
			// it to JSON. Even the first compatibility decode must be admitted.
			cost, err := applicationSetPatchCost(ctx, patch, remaining)
			if err != nil {
				return applicationSetCost{}, err
			}
			patchCost.nodes += cost.nodes
			patchCost.bytes += cost.bytes
		}
	}

	total := applicationSetCost{}
	variants := 0
	var cloned *applicationSetCost
	admit := func(override map[string]any) error {
		cost := applicationSetCost{base.nodes + patchCost.nodes, base.bytes + patchCost.bytes}
		if len(override) != 0 {
			if cloned == nil {
				// cloneObject copies maps recursively but shares slice values.
				copyCost, err := applicationSetValueCost(ctx, value, true, goTemplate, false, remaining)
				if err != nil {
					return err
				}
				cloned = &copyCost
			}
			replacement, err := applicationSetValueCost(ctx, override, false, goTemplate, true, remaining)
			if err != nil {
				return err
			}
			cost.nodes += cloned.nodes + replacement.nodes
			cost.bytes += replacement.bytes
		}
		if cost.nodes > remaining-total.nodes || cost.bytes > remainingBytes-total.bytes {
			return applicationSetBudgetError()
		}
		total.nodes += cost.nodes
		total.bytes += cost.bytes
		variants++
		return ctx.Err()
	}
	for _, generator := range objectSlice(spec["generators"]) {
		for kind, body := range object(generator) {
			if kind == "selector" {
				continue
			}
			if err := admit(object(object(body)["template"])); err != nil {
				return applicationSetCost{}, err
			}
		}
	}
	if variants == 0 {
		if err := admit(nil); err != nil {
			return applicationSetCost{}, err
		}
	}
	return total, nil
}

func applicationSetBudgetError() error {
	return &projectionError{"INPUT_BUDGET_EXCEEDED", "ApplicationSet template expansion exceeds the node, string, or depth budget (100,000 nodes; 64 MiB strings; depth 256)"}
}

func applicationSetValueCost(ctx context.Context, value any, mapsOnly, goTemplate, originPaths bool, remaining int) (applicationSetCost, error) {
	cost := applicationSetCost{}
	var visit func(any, int, int, bool) error
	visit = func(value any, depth, pathBytes int, origin bool) error {
		cost.nodes++
		if depth > 256 || cost.nodes > remaining || cost.bytes > maxApplicationSetExpansionBytes {
			return applicationSetBudgetError()
		}
		if cost.nodes%256 == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		switch value := value.(type) {
		case map[string]any:
			for key, child := range value {
				cost.nodes++ // Mapping keys also occupy expansion work.
				if !mapsOnly {
					cost.bytes += len(key)
				}
				nextPath := pathBytes + len(key)
				if pathBytes != 0 {
					nextPath++
				}
				if err := visit(child, depth+1, nextPath, origin); err != nil {
					return err
				}
			}
		case []any:
			if !mapsOnly {
				if origin {
					cost.bytes += pathBytes // mergeTemplate records a list replacement once.
				}
				for i, child := range value {
					if err := visit(child, depth+1, pathBytes+1+len(strconv.Itoa(i)), false); err != nil {
						return err
					}
				}
			}
		case string:
			if !mapsOnly {
				cost.bytes += len(value)
				if origin {
					cost.bytes += pathBytes
				}
				dynamic := goTemplate && strings.Contains(value, "{{") || !goTemplate && fastTemplateMarker.MatchString(value)
				if dynamic {
					// walk records an UnknownPaths key and detach clones it.
					// Charge repeated prefixes without ever constructing them.
					cost.bytes += 2 * pathBytes
					cost.nodes += depth
				}
			}
		default:
			if !mapsOnly && origin {
				cost.bytes += pathBytes
			}
		}
		if cost.nodes > remaining || cost.bytes > maxApplicationSetExpansionBytes {
			return applicationSetBudgetError()
		}
		return nil
	}
	err := visit(value, 0, 0, originPaths)
	if err == nil {
		err = ctx.Err()
	}
	return cost, err
}

// A yaml.Node tree retains aliases as edges. Traversing those edges with the
// same work/string bounds accounts for the eventual expansion without doing it.
// Invalid syntax is still diagnosed by the existing templatePatch consumer.
func applicationSetPatchCost(ctx context.Context, patch string, remaining int) (applicationSetCost, error) {
	var tree yamlv3.Node
	if err := yamlv3.Unmarshal([]byte(patch), &tree); err != nil {
		return applicationSetCost{}, ctx.Err()
	}
	cost := applicationSetCost{}
	active := map[*yamlv3.Node]bool{}
	var visit func(*yamlv3.Node, int, int, bool) error
	visit = func(node *yamlv3.Node, depth, pathBytes int, origin bool) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if node == nil || active[node] || depth > 256 {
			return applicationSetBudgetError()
		}
		cost.nodes++
		if cost.nodes > remaining || cost.bytes > maxApplicationSetExpansionBytes {
			return applicationSetBudgetError()
		}
		active[node] = true
		defer delete(active, node)
		switch node.Kind {
		case yamlv3.DocumentNode:
			for _, child := range node.Content {
				if err := visit(child, depth+1, pathBytes, origin); err != nil {
					return err
				}
			}
		case yamlv3.AliasNode:
			if err := visit(node.Alias, depth+1, pathBytes, origin); err != nil {
				return err
			}
		case yamlv3.MappingNode:
			for i := 0; i+1 < len(node.Content); i += 2 {
				before := cost.bytes
				if err := visit(node.Content[i], depth+1, 0, false); err != nil {
					return err
				}
				nextPath := pathBytes + cost.bytes - before
				if pathBytes != 0 {
					nextPath++
				}
				if err := visit(node.Content[i+1], depth+1, nextPath, origin); err != nil {
					return err
				}
			}
		case yamlv3.SequenceNode:
			if origin {
				cost.bytes += pathBytes
			}
			for i, child := range node.Content {
				if err := visit(child, depth+1, pathBytes+1+len(strconv.Itoa(i)), false); err != nil {
					return err
				}
			}
		case yamlv3.ScalarNode:
			cost.bytes += len(node.Value)
			if origin {
				cost.bytes += pathBytes
			}
		}
		if cost.nodes > remaining || cost.bytes > maxApplicationSetExpansionBytes {
			return applicationSetBudgetError()
		}
		return nil
	}
	err := visit(&tree, 0, 0, true)
	return cost, err
}
