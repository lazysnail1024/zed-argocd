package analysis

import (
	"context"
	"strconv"
	"strings"
	"text/template/parse"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"sigs.k8s.io/yaml"
)

// HelmSourceDocuments is a query-only source projection. It never executes a
// template, enters validation or supplies declarations to the workspace index.
// Template actions must occupy scalar values in an otherwise strict YAML tree.
// Byte-preserving placeholders let untouched tokens retain original positions.
func HelmSourceDocuments(ctx context.Context, snapshot source.Snapshot, target string) []Document {
	text := snapshot.Text()
	tree := parse.New("source")
	tree.Mode = parse.SkipFuncCheck
	if _, err := tree.Parse(text, "{{", "}}", map[string]*parse.Tree{}); err != nil {
		return nil
	}
	for _, node := range tree.Root.Nodes {
		switch n := node.(type) {
		case *parse.TextNode:
		case *parse.ActionNode:
			if len(n.Pipe.Decl) != 0 || len(n.Pipe.Cmds) != 1 || len(n.Pipe.Cmds[0].Args) != 1 {
				return nil
			}
			switch n.Pipe.Cmds[0].Args[0].(type) {
			case *parse.FieldNode, *parse.StringNode, *parse.NumberNode, *parse.BoolNode:
			default:
				return nil
			}
		default:
			return nil // if/range/with/template/include may change YAML structure.
		}
	}
	masked := []byte(text)
	spans := []source.Span{}
	for cursor := 0; cursor < len(text); {
		relative := strings.Index(text[cursor:], "{{")
		if relative < 0 {
			break
		}
		start := cursor + relative
		quote := byte(0)
		end := -1
		for i := start + 2; i < len(text); i++ {
			ch := text[i]
			if ch == '\n' || ch == '\r' {
				return nil
			}
			if quote != 0 {
				if ch == '\\' && quote != '`' {
					i++
					continue
				}
				if ch == quote {
					quote = 0
				}
				continue
			}
			if ch == '"' || ch == '\'' || ch == '`' {
				quote = ch
				continue
			}
			if ch == '}' && i+1 < len(text) && text[i+1] == '}' {
				end = i + 2
				break
			}
		}
		if end < 0 || strings.HasPrefix(text[start:], "{{-") || strings.HasSuffix(text[start:end], "-}}") {
			return nil
		}
		for i := start; i < end; i++ {
			masked[i] = 'x'
		}
		spans = append(spans, source.Span{Start: start, End: end})
		cursor = end
	}
	projected, err := source.New(snapshot.URI(), snapshot.Revision(), string(masked))
	if err != nil {
		return nil
	}
	view, err := yamlview.Parse(ctx, projected)
	if err != nil || len(view.Issues()) != 0 {
		return nil
	}
	result := []Document{}
	for _, syntax := range view.Documents() {
		value, err := projectValues(ctx, projected, syntax)
		if err != nil {
			return nil
		}
		doc := Document{Snapshot: snapshot, Root: syntax.Root(), Value: object(value.Value), Origin: value.Origin, Target: target, UnknownPaths: map[string]bool{}}
		for _, span := range spans {
			if span.Start < syntax.Span().Start || span.Start >= syntax.Span().End {
				continue
			}
			parts, node, key, ok := at(syntax.Root(), span.Start, nil)
			if !ok || len(parts) == 0 || key || node.Kind() != yamlview.Scalar || span.End > node.Span().End || !doc.exact(parts) {
				return nil
			}
			// The entire scalar is unknown, including a literal prefix or suffix.
			parent := valueAt(doc.Value, parts[:len(parts)-1])
			switch p := parent.(type) {
			case map[string]any:
				p[parts[len(parts)-1]] = nil
			case []any:
				i, e := strconv.Atoi(parts[len(parts)-1])
				if e != nil || i < 0 || i >= len(p) {
					return nil
				}
				p[i] = nil
			default:
				return nil
			}
			doc.UnknownPaths[strings.Join(parts, "/")] = true
		}
		if literal(doc.Value["apiVersion"]) != "argoproj.io/v1alpha1" {
			continue
		}
		doc.Kind = literal(doc.Value["kind"])
		if doc.Kind == "Application" || doc.Kind == "ApplicationSet" || doc.Kind == "AppProject" {
			result = append(result, doc)
		}
	}
	return result
}

// HelmSourceOwned discovers a potential owned resource in literal template text.
// It controls only notices and the explicit render action, never queries or
// validation. A conditional resource is still a useful render-action context.
func HelmSourceOwned(ctx context.Context, snapshot source.Snapshot) bool {
	tree := parse.New("ownership")
	tree.Mode = parse.SkipFuncCheck
	if _, err := tree.Parse(snapshot.Text(), "{{", "}}", map[string]*parse.Tree{}); err != nil {
		return false
	}
	var owned func(parse.Node) bool
	owned = func(node parse.Node) bool {
		if ctx.Err() != nil || node == nil {
			return false
		}
		switch n := node.(type) {
		case *parse.ListNode:
			if n == nil {
				return false
			}
			for _, child := range n.Nodes {
				if owned(child) {
					return true
				}
			}
		case *parse.IfNode:
			return owned(n.List) || owned(n.ElseList)
		case *parse.RangeNode:
			return owned(n.List) || owned(n.ElseList)
		case *parse.WithNode:
			return owned(n.List) || owned(n.ElseList)
		case *parse.TextNode:
			text := string(n.Text)
			// A template action may interrupt a quoted/flow scalar. Only the
			// preceding complete source lines can establish literal root keys.
			if end := strings.LastIndexByte(text, '\n'); end >= 0 {
				text = text[:end+1]
			} else {
				return false
			}
			// A fragment beginning midway through a scalar cannot establish root keys.
			if n.Pos != 0 && !strings.HasPrefix(text, "\n") && !strings.HasPrefix(text, "\r\n") {
				return false
			}
			part, err := source.New(snapshot.URI(), snapshot.Revision(), text)
			if err != nil {
				return false
			}
			view, err := yamlview.Parse(ctx, part)
			if err != nil {
				return false
			}
			for _, doc := range view.Documents() {
				fields := map[string]string{}
				counts := map[string]int{}
				for _, m := range doc.Root().Members() {
					var key, value string
					if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &key) != nil {
						continue
					}
					if key != "apiVersion" && key != "kind" {
						continue
					}
					counts[key]++
					if m.Value.Kind() == yamlview.Scalar && yaml.UnmarshalStrict([]byte(m.Value.Raw()), &value) == nil {
						fields[key] = value
					}
				}
				kind := fields["kind"]
				if counts["apiVersion"] == 1 && counts["kind"] == 1 && fields["apiVersion"] == "argoproj.io/v1alpha1" && (kind == "Application" || kind == "ApplicationSet" || kind == "AppProject") {
					return true
				}
			}
		}
		return false
	}
	return owned(tree.Root)
}
