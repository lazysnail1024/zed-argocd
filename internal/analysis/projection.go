package analysis

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	yamlv3 "gopkg.in/yaml.v3"
	"sigs.k8s.io/yaml"
)

const maxProjectionNodes = 100000

// CST spans can start at the first key after indentation. Include only actual
// whitespace on that same source line so subsequent lines keep their alignment.
func documentText(snapshot source.Snapshot, span source.Span) (string, error) {
	lineStart := strings.LastIndexByte(snapshot.Text()[:span.Start], '\n') + 1
	if strings.Trim(snapshot.Text()[lineStart:span.Start], " \t\r") == "" {
		span.Start = lineStart
	}
	return snapshot.Slice(span)
}

// Provenance follows value paths rather than a second synthetic syntax tree.
// Children retain immutable source facts; derived parents override their ranges.
type valueOrigin struct {
	Node     yamlview.Node
	Key      yamlview.Node
	Kind     string
	Children map[string]*valueOrigin
}
type projectedValue struct {
	Value         any
	Origin        *valueOrigin
	Nodes         int
	IdentityKnown bool
	Owned         bool
}
type projectionError struct{ Code, Message string }

// Match the pinned YAML decoder's merge-key recognition before alias decoding.
func isMergeKey(n *yamlv3.Node) bool {
	return n.Kind == yamlv3.ScalarNode && n.Value == "<<" && n.Tag == "!!merge"
}

func (e *projectionError) Error() string { return e.Message }
func projectionFailure(message string) error {
	return &projectionError{"YAML_PROJECTION_INCOMPLETE", message}
}

// projectValues never asks a YAML decoder to expand a graph. The parser only
// creates nodes and alias links; this bounded walk owns expansion and precedence.
func projectValues(ctx context.Context, raw string, syntax yamlview.Node) (projectedValue, error) {
	return projectValuesWithin(ctx, raw, syntax, maxProjectionNodes)
}

func projectValuesWithin(ctx context.Context, raw string, syntax yamlview.Node, allowance int) (out projectedValue, err error) {
	var tree yamlv3.Node
	if err := ctx.Err(); err != nil {
		return projectedValue{}, err
	}
	defer func() {
		if err == nil || ctx.Err() != nil {
			return
		}
		var root *yamlv3.Node
		if len(tree.Content) == 1 {
			root = tree.Content[0]
		} else {
			root = recoverProjectionIdentity(ctx, raw, syntax)
		}
		if root != nil {
			out.Owned, out.IdentityKnown = projectedOwnership(ctx, root)
		}
	}()
	if err := yamlv3.Unmarshal([]byte(raw), &tree); err != nil {
		return projectedValue{}, projectionFailure("YAML cannot be projected without ambiguity: " + err.Error())
	}
	if err := ctx.Err(); err != nil {
		return projectedValue{}, err
	}
	if len(tree.Content) != 1 {
		return projectedValue{}, projectionFailure("Expected one YAML document")
	}
	if oversizedScalar(syntax) {
		return projectedValue{}, &projectionError{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
	}
	nodes := map[*yamlv3.Node]yamlview.Node{}
	shared := map[*yamlv3.Node]bool{}
	var bind func(*yamlv3.Node, yamlview.Node)
	bind = func(n *yamlv3.Node, s yamlview.Node) {
		nodes[n] = s
		if n.Kind == yamlv3.AliasNode {
			shared[n.Alias] = true
			return
		}
		if n.Kind == yamlv3.MappingNode {
			members := s.Members()
			for i := 0; i+1 < len(n.Content) && i/2 < len(members); i += 2 {
				bind(n.Content[i], members[i/2].Key)
				bind(n.Content[i+1], members[i/2].Value)
			}
		} else if n.Kind == yamlv3.SequenceNode {
			elements := s.Elements()
			for i, child := range n.Content {
				if i < len(elements) {
					bind(child, elements[i])
				}
			}
		}
	}
	scalarValues := map[*yamlv3.Node]any{}
	scalar := func(n *yamlv3.Node) (any, error) {
		if value, ok := scalarValues[n]; ok {
			return value, nil
		}
		value, err := strictScalar(n)
		if err != nil {
			return nil, err
		}
		scalarValues[n] = value
		return value, nil
	}
	// Preserve the existing strict scalar/JSON path for ordinary documents.
	// Alias and merge graphs alone need value-path provenance and expansion.
	var graph bool
	var inspect func(*yamlv3.Node) error
	inspect = func(n *yamlv3.Node) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if n.Kind == yamlv3.AliasNode || n.Tag == "!!merge" {
			graph = true
		}
		if n.Tag != "" && !strings.HasPrefix(n.LongTag(), "tag:yaml.org,2002:") {
			return projectionFailure("Custom YAML tags cannot be projected safely")
		}
		if n.Kind == yamlv3.ScalarNode && len(n.Value) > 1024*1024 {
			return &projectionError{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
		}
		for i, child := range n.Content {
			if n.Kind == yamlv3.MappingNode && i%2 == 0 && child.Tag != "!!merge" && child.Kind != yamlv3.AliasNode {
				if child.Kind != yamlv3.ScalarNode {
					return projectionFailure("Only string mapping keys are supported")
				}
				key, err := scalar(child)
				if err != nil {
					return err
				}
				if _, ok := key.(string); !ok {
					return projectionFailure("Only string mapping keys are supported")
				}
			}
			if err := inspect(child); err != nil {
				return err
			}
		}
		return nil
	}
	if err := inspect(tree.Content[0]); err != nil {
		return projectedValue{}, err
	}
	if !graph {
		data, err := yaml.YAMLToJSONStrict([]byte(raw))
		if err != nil {
			return projectedValue{}, projectionFailure("YAML cannot be projected without ambiguity: " + err.Error())
		}
		var value any
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		if err = decoder.Decode(&value); err != nil {
			return projectedValue{}, projectionFailure(err.Error())
		}
		return projectedValue{Value: value}, ctx.Err()
	}
	bind(tree.Content[0], syntax)
	remaining := allowance
	active := map[*yamlv3.Node]bool{}
	var walk func(*yamlv3.Node, int) (any, *valueOrigin, error)
	walk = func(n *yamlv3.Node, depth int) (any, *valueOrigin, error) {
		if err := ctx.Err(); err != nil {
			return nil, nil, err
		}
		if n == nil {
			return nil, nil, projectionFailure("Unresolved YAML alias")
		}
		if depth > 256 {
			return nil, nil, &projectionError{"INPUT_BUDGET_EXCEEDED", "expanded YAML depth exceeds 256"}
		}
		if active[n] {
			return nil, nil, projectionFailure("Recursive YAML aliases cannot be projected")
		}
		active[n] = true
		defer delete(active, n)
		if n.Kind == yamlv3.AliasNode {
			value, origin, err := walk(n.Alias, depth)
			if err != nil {
				return nil, nil, err
			}
			copy := *origin
			copy.Node = nodes[n]
			copy.Kind = "derived"
			return value, &copy, nil
		}
		remaining--
		if remaining < 0 {
			return nil, nil, &projectionError{"INPUT_BUDGET_EXCEEDED", "expanded YAML exceeds 100,000 nodes"}
		}
		origin := &valueOrigin{Node: nodes[n], Kind: "exact"}
		if shared[n] {
			origin.Kind = "ambiguous"
		}
		switch n.Kind {
		case yamlv3.ScalarNode:
			value, err := scalar(n)
			return value, origin, err
		case yamlv3.SequenceNode:
			if n.Tag != "!!seq" {
				return nil, nil, projectionFailure("Unsupported sequence tag")
			}
			values := make([]any, 0, len(n.Content))
			origin.Children = map[string]*valueOrigin{}
			for i, child := range n.Content {
				v, o, err := walk(child, depth+1)
				if err != nil {
					return nil, nil, err
				}
				values = append(values, v)
				origin.Children[fmt.Sprint(i)] = o
			}
			return values, origin, nil
		case yamlv3.MappingNode:
			if n.Tag != "!!map" && n.Tag != "!!set" {
				return nil, nil, projectionFailure("Unsupported mapping tag")
			}
			values := map[string]any{}
			origin.Children = map[string]*valueOrigin{}
			var merge *yamlv3.Node
			for i := 0; i+1 < len(n.Content); i += 2 {
				key, value := n.Content[i], n.Content[i+1]
				if isMergeKey(key) {
					remaining--
					if remaining < 0 {
						return nil, nil, &projectionError{"INPUT_BUDGET_EXCEEDED", "expanded YAML exceeds 100,000 nodes"}
					}
					if merge != nil {
						return nil, nil, projectionFailure("Duplicate merge directives are ambiguous")
					}
					merge = value
					continue
				}
				k, ko, err := walk(key, depth+1)
				if err != nil {
					return nil, nil, err
				}
				name, ok := k.(string)
				if !ok {
					return nil, nil, projectionFailure("Only string mapping keys are supported")
				}
				if _, exists := values[name]; exists {
					return nil, nil, projectionFailure("Duplicate mapping keys are ambiguous")
				}
				v, o, err := walk(value, depth+1)
				if err != nil {
					return nil, nil, err
				}
				o.Key = nodes[key]
				if ko.Kind != "exact" {
					o.Kind = ko.Kind
					o.Node = ko.Node
				}
				values[name] = v
				origin.Children[name] = o
			}
			if merge != nil {
				v, o, err := walk(merge, depth+1)
				if err != nil {
					return nil, nil, err
				}
				var maps []map[string]any
				var origins []*valueOrigin
				var sites []yamlview.Node
				if m, ok := v.(map[string]any); ok {
					maps = append(maps, m)
					origins = append(origins, o)
					sites = append(sites, nodes[merge])
				} else if seq, ok := v.([]any); ok {
					for i, item := range seq {
						m, ok := item.(map[string]any)
						if !ok {
							return nil, nil, projectionFailure("Merge sequence members must be mappings")
						}
						maps = append(maps, m)
						origins = append(origins, o.Children[fmt.Sprint(i)])
						site := nodes[merge]
						if merge.Kind == yamlv3.SequenceNode {
							site = nodes[merge.Content[i]]
						}
						sites = append(sites, site)
					}
				} else {
					return nil, nil, projectionFailure("Merge value must be a mapping or mapping sequence")
				}
				// Explicit entries already exist. Earlier merge maps win over later maps.
				for i, m := range maps {
					for name, value := range m {
						if _, exists := values[name]; exists {
							continue
						}
						values[name] = value
						child := *origins[i].Children[name]
						child.Kind = "derived"
						child.Node = sites[i]
						child.Key = sites[i]
						origin.Children[name] = &child
					}
				}
			}
			return values, origin, nil
		}
		return nil, nil, projectionFailure("Unsupported YAML node")
	}
	value, origin, err := walk(tree.Content[0], 0)
	return projectedValue{Value: value, Origin: origin, Nodes: allowance - remaining}, err
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
	current := doc.Origin
	kind := current.Kind
	site := current.Node
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
			kind = "derived"
			site = current.Node
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

// Ownership is a bounded lookup of two unique scalar facts, never a partial
// resource projection. Unrelated recursive values do not erase known identity.
// strictScalar preserves the pinned strict YAML scalar semantics for values
// and the bounded ownership lookup. It does not expand aliases or collections.
func strictScalar(n *yamlv3.Node) (any, error) {
	if n == nil || n.Kind != yamlv3.ScalarNode || (n.Tag != "" && !strings.HasPrefix(n.LongTag(), "tag:yaml.org,2002:")) {
		return nil, projectionFailure("Only standard YAML scalars can establish values")
	}
	if len(n.Value) > 1024*1024 {
		return nil, &projectionError{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
	}
	var value any
	if n.Tag == "!!str" && (n.Style&(yamlv3.TaggedStyle|yamlv3.DoubleQuotedStyle|yamlv3.SingleQuotedStyle|yamlv3.LiteralStyle|yamlv3.FoldedStyle)) != 0 {
		value = n.Value
	} else {
		text := n.Value
		if n.Style&yamlv3.TaggedStyle != 0 {
			quoted, _ := json.Marshal(n.Value)
			text = n.ShortTag() + " " + string(quoted)
		}
		data, err := yaml.YAMLToJSONStrict([]byte(text))
		if err != nil {
			return nil, projectionFailure("Scalar cannot be projected safely: " + err.Error())
		}
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		if err = decoder.Decode(&value); err != nil {
			return nil, projectionFailure(err.Error())
		}
		switch value.(type) {
		case map[string]any, []any:
			return nil, projectionFailure("Scalar decoding changed its structural kind")
		}
	}
	return value, nil
}

func projectedOwnership(ctx context.Context, root *yamlv3.Node) (bool, bool) {
	remaining := maxProjectionNodes
	active := map[*yamlv3.Node]bool{}
	resolve := func(n *yamlv3.Node) *yamlv3.Node {
		seen := map[*yamlv3.Node]bool{}
		for n != nil && n.Kind == yamlv3.AliasNode {
			remaining--
			if remaining < 0 || seen[n] || ctx.Err() != nil {
				return nil
			}
			seen[n] = true
			n = n.Alias
		}
		return n
	}
	var selectField func(*yamlv3.Node, string, int) (*yamlv3.Node, bool)
	selectField = func(n *yamlv3.Node, name string, depth int) (*yamlv3.Node, bool) {
		remaining--
		if n == nil || remaining < 0 || depth > 256 || ctx.Err() != nil || active[n] {
			return nil, false
		}
		active[n] = true
		defer delete(active, n)
		if n.Kind == yamlv3.AliasNode {
			return selectField(n.Alias, name, depth+1)
		}
		if n.Kind != yamlv3.MappingNode || n.Tag != "!!map" {
			return nil, false
		}
		var found, merge *yamlv3.Node
		merges := 0
		for i := 0; i+1 < len(n.Content); i += 2 {
			remaining--
			if remaining < 0 || ctx.Err() != nil {
				return nil, false
			}
			if isMergeKey(n.Content[i]) {
				merge = n.Content[i+1]
				merges++
				continue
			}
			key := resolve(n.Content[i])
			if key == nil {
				return nil, false
			}
			decoded, err := strictScalar(key)
			keyName, ok := decoded.(string)
			if err != nil || !ok {
				return nil, false
			}
			if keyName == name {
				if found != nil {
					return nil, false
				}
				found = n.Content[i+1]
			}
		}
		if found != nil {
			return found, true
		}
		if merges == 0 {
			return nil, true
		}
		if merges != 1 {
			return nil, false
		}
		merge = resolve(merge)
		if merge == nil {
			return nil, false
		}
		if merge.Kind != yamlv3.SequenceNode {
			return selectField(merge, name, depth+1)
		}
		for _, child := range merge.Content {
			value, ok := selectField(child, name, depth+1)
			if !ok || value != nil {
				return value, ok
			}
		}
		return nil, true
	}
	scalar := func(name string) string {
		n, ok := selectField(root, name, 0)
		if !ok {
			return ""
		}
		n = resolve(n)
		value, err := strictScalar(n)
		if err != nil {
			return ""
		}
		text, _ := value.(string)
		return text
	}
	api, kind := scalar("apiVersion"), scalar("kind")
	ownedKind := kind == "Application" || kind == "AppProject" || kind == "ApplicationSet"
	owned := api == "argoproj.io/v1alpha1" && ownedKind
	known := owned || (api != "" && api != "argoproj.io/v1alpha1") || (kind != "" && !ownedKind)
	return owned, known
}

// A failed parse exposes no partial yaml.v3 tree. One bounded wrapper parse
// supplies null placeholders for aliases, preserving original definitions and
// their order. Only ownership is queried; recovered values are never admitted.
func recoverProjectionIdentity(ctx context.Context, raw string, syntax yamlview.Node) *yamlv3.Node {
	names := []string{}
	seen := map[string]bool{}
	nodes := 3 // document, wrapper sequence and placeholder sequence
	var collect func(yamlview.Node, int) bool
	collect = func(n yamlview.Node, depth int) bool {
		nodes++
		if nodes > maxProjectionNodes || depth+2 > 256 || ctx.Err() != nil {
			return false
		}
		if n.Kind() == yamlview.Alias {
			name := strings.TrimPrefix(n.Raw(), "*")
			if name == "" || name == n.Raw() {
				return false
			}
			if !seen[name] {
				seen[name] = true
				names = append(names, name)
				nodes++
			}
		}
		for _, m := range n.Members() {
			if !collect(m.Key, depth+1) || !collect(m.Value, depth+1) {
				return false
			}
		}
		for _, child := range n.Elements() {
			if !collect(child, depth+1) {
				return false
			}
		}
		return nodes <= maxProjectionNodes
	}
	if !collect(syntax, 0) || len(names) == 0 {
		return nil
	}
	var header, body strings.Builder
	started, marker, ended := false, false, false
	for rest := raw; rest != ""; {
		if ctx.Err() != nil {
			return nil
		}
		end := strings.IndexByte(rest, '\n')
		if end < 0 {
			end = len(rest) - 1
		}
		line := rest[:end+1]
		rest = rest[end+1:]
		trim := strings.TrimSpace(line)
		if strings.HasPrefix(line, "%") {
			if started || marker {
				return nil
			}
			header.WriteString(line)
			continue
		}
		isMarker := func(mark string) bool {
			return strings.HasPrefix(line, mark) && (len(line) == 3 || strings.ContainsRune(" \t\r\n", rune(line[3])))
		}
		if isMarker("---") {
			if started || marker || ended {
				return nil
			}
			marker = true
			line = strings.TrimLeft(line[3:], " \t")
			trim = strings.TrimSpace(line)
		} else if isMarker("...") {
			tail := strings.TrimSpace(line[3:])
			if tail != "" && !strings.HasPrefix(tail, "#") {
				return nil
			}
			ended = true
			continue
		}
		content := trim != "" && !strings.HasPrefix(trim, "#")
		if ended {
			if content {
				return nil
			}
			continue
		}
		started = started || content
		body.WriteString("  ")
		body.WriteString(line)
		if body.Len()+header.Len() > 5*1024*1024 {
			return nil
		}
	}
	var text strings.Builder
	text.WriteString(header.String())
	text.WriteString("---\n-\n")
	for _, name := range names {
		text.WriteString("  - &")
		text.WriteString(name)
		text.WriteString(" null\n")
	}
	text.WriteString("-\n")
	text.WriteString(body.String())
	if text.Len() > 5*1024*1024 || ctx.Err() != nil {
		return nil
	}
	var recovered yamlv3.Node
	if yamlv3.Unmarshal([]byte(text.String()), &recovered) != nil || ctx.Err() != nil || len(recovered.Content) != 1 {
		return nil
	}
	wrapper := recovered.Content[0]
	if wrapper.Kind != yamlv3.SequenceNode || len(wrapper.Content) != 2 {
		return nil
	}
	return wrapper.Content[1]
}
