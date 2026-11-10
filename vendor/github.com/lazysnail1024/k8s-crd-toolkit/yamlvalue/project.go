// Package yamlvalue projects a lossless YAML view into bounded effective values
// and source provenance. Product identity, versions, schemas and editing policy
// remain with the caller.
package yamlvalue

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

// Options bounds graph work without conflating it with a caller's aggregate
// logical-result budget. MaxNodes applies to expanded non-alias nodes; zero
// admits ordinary documents but no alias/merge graph. MaxVisits, when positive,
// additionally counts alias visits and ordinary syntax nodes.
type Options struct {
	MaxNodes           int
	MaxVisits          int
	RejectExplicitTags bool
	// Fields requests independently known root string fields, including when
	// projection fails elsewhere. These partial hints never establish that the
	// complete document is valid and must not replace handling the returned error.
	Fields []string
}

type Result struct {
	Value  any
	Origin *Origin
	// Nodes charges expansion work, LogicalNodes charges the resulting effective
	// value; both are zero for ordinary documents. The caller owns aggregate limits.
	Nodes, LogicalNodes int
	Shared              map[source.Span]bool
	Fields              map[string]string
}

type Error struct{ Code, Message string }

func visitFailure(limit int) error {
	return &Error{"INPUT_BUDGET_EXCEEDED", fmt.Sprintf("alias expansion exceeds %d nodes", limit)}
}
func parseFailure(err error) error {
	message := err.Error()
	if _, tail, found := strings.Cut(message, "unknown anchor '"); found {
		if name, _, ok := strings.Cut(tail, "'"); ok {
			return projectionFailure("unknown alias " + name)
		}
	}
	return projectionFailure("YAML cannot be projected without ambiguity: " + message)
}

// CST spans can start at the first key after indentation. Include only actual
// whitespace on that same source line so subsequent lines keep their alignment.
func documentText(snapshot source.Snapshot, span source.Span) (string, error) {
	if _, err := snapshot.Slice(span); err != nil {
		return "", err
	}
	lineStart := strings.LastIndexByte(snapshot.Text()[:span.Start], '\n') + 1
	if strings.Trim(snapshot.Text()[lineStart:span.Start], " \t\r") == "" {
		span.Start = lineStart
	}
	return snapshot.Slice(span)
}

// Match the pinned YAML decoder's merge-key recognition before alias decoding.
func isMergeKey(n *yamlv3.Node) bool {
	return n.Kind == yamlv3.ScalarNode && n.Value == "<<" && n.Tag == "!!merge"
}

func (e *Error) Error() string { return e.Message }
func projectionFailure(message string) error {
	return &Error{"YAML_PROJECTION_INCOMPLETE", message}
}

// Project materializes one document with bounded alias/merge expansion. The
// short-lived YAML node graph is discarded before returning. No files, schemas,
// CRD identities or long-lived caches are owned by this package.
func Project(ctx context.Context, snapshot source.Snapshot, doc yamlview.Document, options Options) (out Result, err error) {
	if options.MaxNodes < 0 || options.MaxVisits < 0 {
		return Result{}, fmt.Errorf("yamlvalue: limits must not be negative")
	}
	if !doc.Complete() {
		return Result{}, projectionFailure("incomplete YAML document")
	}
	raw, err := documentText(snapshot, doc.Span())
	if err != nil {
		return Result{}, err
	}
	syntax := doc.Root()
	if syntax.Kind() != yamlview.Empty {
		text, sliceErr := snapshot.Slice(syntax.Span())
		if sliceErr != nil || text != syntax.Raw() {
			return Result{}, fmt.Errorf("yamlvalue: document does not belong to snapshot")
		}
	}
	allowance := options.MaxNodes
	var tree yamlv3.Node
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	defer func() {
		if ctx.Err() != nil || len(options.Fields) == 0 {
			return
		}
		if err == nil {
			out.Fields = map[string]string{}
			root, _ := out.Value.(map[string]any)
			for _, key := range options.Fields {
				if value, ok := root[key].(string); ok {
					out.Fields[key] = value
				}
			}
			return
		}
		var root *yamlv3.Node
		if len(tree.Content) == 1 {
			root = tree.Content[0]
		} else {
			root = recoverFields(ctx, raw, syntax)
		}
		if root != nil {
			out.Fields = selectedFields(ctx, root, options.Fields)
		}
	}()
	if err := yamlv3.Unmarshal([]byte(raw), &tree); err != nil {
		return Result{}, parseFailure(err)
	}
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if len(tree.Content) != 1 {
		return Result{}, projectionFailure("Expected one YAML document")
	}
	if oversizedScalar(syntax) {
		return Result{}, &Error{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
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
	syntaxNodes := 0
	var inspect func(*yamlv3.Node) error
	inspect = func(n *yamlv3.Node) error {
		syntaxNodes++
		if err := ctx.Err(); err != nil {
			return err
		}
		if options.RejectExplicitTags && n.Style&yamlv3.TaggedStyle != 0 {
			return projectionFailure("explicit YAML tag is not evaluated")
		}
		if n.Kind == yamlv3.AliasNode || n.Tag == "!!merge" {
			graph = true
		}
		if n.Tag != "" && !strings.HasPrefix(n.LongTag(), "tag:yaml.org,2002:") {
			return projectionFailure("Custom YAML tags cannot be projected safely")
		}
		if n.Kind == yamlv3.ScalarNode && len(n.Value) > 1024*1024 {
			return &Error{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
		}
		keys := map[string]bool{}
		for i, child := range n.Content {
			if n.Kind == yamlv3.MappingNode && i%2 == 0 && child.Tag != "!!merge" && child.Kind != yamlv3.AliasNode {
				if child.Kind != yamlv3.ScalarNode {
					return projectionFailure("Only string mapping keys are supported")
				}
				key, err := scalar(child)
				if err != nil {
					return err
				}
				name, ok := key.(string)
				if !ok {
					return projectionFailure("Only string mapping keys are supported")
				}
				if keys[name] {
					return projectionFailure("duplicate mapping key " + name)
				}
				keys[name] = true
			}
			if err := inspect(child); err != nil {
				return err
			}
		}
		return nil
	}
	if err := inspect(tree.Content[0]); err != nil {
		return Result{}, err
	}
	if !graph {
		if options.MaxVisits > 0 && syntaxNodes > options.MaxVisits {
			return Result{}, visitFailure(options.MaxVisits)
		}
		data, err := yaml.YAMLToJSONStrict([]byte(raw))
		if err != nil {
			return Result{}, projectionFailure("YAML cannot be projected without ambiguity: " + err.Error())
		}
		var value any
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		if err = decoder.Decode(&value); err != nil {
			return Result{}, projectionFailure(err.Error())
		}
		return Result{Value: value}, ctx.Err()
	}
	bind(tree.Content[0], syntax)
	sharedSpans := map[source.Span]bool{}
	for node := range shared {
		sharedSpans[nodes[node].Span()] = true
	}
	remaining := allowance
	visits := 0
	visit := func() error {
		visits++
		if options.MaxVisits > 0 && visits > options.MaxVisits {
			return visitFailure(options.MaxVisits)
		}
		return nil
	}
	active := map[*yamlv3.Node]bool{}
	var walk func(*yamlv3.Node, int) (any, *Origin, error)
	walk = func(n *yamlv3.Node, depth int) (any, *Origin, error) {
		if err := ctx.Err(); err != nil {
			return nil, nil, err
		}
		if n == nil {
			return nil, nil, projectionFailure("Unresolved YAML alias")
		}
		if depth > 256 {
			return nil, nil, &Error{"INPUT_BUDGET_EXCEEDED", "expanded YAML depth exceeds 256"}
		}
		if active[n] {
			return nil, nil, projectionFailure("Recursive YAML aliases cannot be projected")
		}
		active[n] = true
		defer delete(active, n)
		if err := visit(); err != nil {
			return nil, nil, err
		}
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
			return nil, nil, &Error{"INPUT_BUDGET_EXCEEDED", "expanded YAML exceeds 100,000 nodes"}
		}
		origin := &Origin{Node: nodes[n], Kind: "exact"}
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
			origin.Children = map[string]*Origin{}
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
			origin.Children = map[string]*Origin{}
			var merge *yamlv3.Node
			for i := 0; i+1 < len(n.Content); i += 2 {
				key, value := n.Content[i], n.Content[i+1]
				if isMergeKey(key) {
					if err := visit(); err != nil {
						return nil, nil, err
					}
					remaining--
					if remaining < 0 {
						return nil, nil, &Error{"INPUT_BUDGET_EXCEEDED", "expanded YAML exceeds 100,000 nodes"}
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
					return nil, nil, projectionFailure("duplicate mapping key " + name)
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
				var origins []*Origin
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
	return Result{Value: value, Origin: origin, Nodes: allowance - remaining, LogicalNodes: logicalNodes(value), Shared: sharedSpans}, err
}
