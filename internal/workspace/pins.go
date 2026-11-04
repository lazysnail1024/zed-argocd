package workspace

import (
	"context"
	"encoding/json"
	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	yaml "gopkg.in/yaml.v3"
	"strings"
)

type Pin struct {
	Root           bool
	Scope, Version string
	Selection      source.Span
	Plan           edit.Plan
}

// Pins persists an already resolved target; it never selects a default or release.
// Only exact scalar replacements and unambiguous block-map insertions are offered.
func Pins(ctx context.Context, input Input, snapshot source.Snapshot) []Pin {
	out := []Pin{}
	if !input.Complete || ctx.Err() != nil {
		return out
	}
	var tree yaml.Node
	if yaml.Unmarshal([]byte(snapshot.Text()), &tree) != nil || len(tree.Content) != 1 {
		return out
	}
	var safe func(*yaml.Node) bool
	safe = func(n *yaml.Node) bool {
		if n.Kind == yaml.AliasNode || n.Anchor != "" || n.Tag == "!!merge" {
			return false
		}
		for _, c := range n.Content {
			if !safe(c) {
				return false
			}
		}
		return true
	}
	if !safe(&tree) {
		return out
	}
	root := tree.Content[0]
	field := func(m *yaml.Node, key string) *yaml.Node {
		for i := 0; i+1 < len(m.Content); i += 2 {
			if m.Content[i].Value == key {
				return m.Content[i+1]
			}
		}
		return nil
	}
	lines := strings.SplitAfter(snapshot.Text(), "\n")
	offset := func(n *yaml.Node) int {
		pos := 0
		for i := 0; i < n.Line-1 && i < len(lines); i++ {
			pos += len(lines[i])
		}
		if n.Line > len(lines) {
			return -1
		}
		runes := []rune(lines[n.Line-1])
		if n.Column-1 > len(runes) {
			return -1
		}
		return pos + len(string(runes[:n.Column-1]))
	}
	add := func(isRoot bool, scope string, m, anchor *yaml.Node) {
		if m == nil || m.Kind != yaml.MappingNode || ctx.Err() != nil {
			return
		}
		version := ""
		for _, t := range input.Targets {
			if t.Scope == scope {
				version = t.Version
			}
		}
		if version == "" {
			return
		}
		existing := field(m, "targetVersion")
		if existing != nil && existing.Value == version {
			return
		}
		var span, selection source.Span
		old, newText := "", ""
		if existing != nil {
			if existing.Kind != yaml.ScalarNode || existing.Tag != "!!str" || strings.ContainsAny(existing.Value, "\r\n") {
				return
			}
			start := offset(existing)
			if start < 0 {
				return
			}
			encoded, _ := json.Marshal(version)
			newText = string(encoded)
			raw := snapshot.Text()[start:]
			length := singleLineScalarLength(existing, raw)
			if length == 0 {
				return
			}
			if existing.Style == yaml.SingleQuotedStyle {
				newText = "'" + version + "'"
			} else if existing.Style == 0 {
				newText = version
			}

			span = source.Span{Start: start, End: start + length}
			old = raw[:length]
			selection = span
		} else {
			if m.Style&yaml.FlowStyle != 0 || anchor == nil || anchor.Line < 1 || anchor.Line > len(lines) {
				return
			}
			// Anchor must occupy a single physical line; append after its complete comment.
			anchorStart := offset(anchor)
			if anchorStart < 0 || singleLineScalarLength(anchor, snapshot.Text()[anchorStart:]) == 0 {
				return
			}
			end := 0
			for i := 0; i < anchor.Line; i++ {
				end += len(lines[i])
			}
			newline := "\n"
			if strings.HasSuffix(lines[anchor.Line-1], "\r\n") {
				newline = "\r\n"
			}
			indent := m.Column - 1
			encoded, _ := json.Marshal(version)
			newText = strings.Repeat(" ", indent) + "targetVersion: " + string(encoded) + newline
			if !strings.HasSuffix(lines[anchor.Line-1], "\n") {
				newText = newline + newText
			}
			span = source.Span{Start: end, End: end}
			selection = source.Span{Start: offset(anchor), End: end}
		}
		out = append(out, Pin{isRoot, scope, version, selection, edit.Plan{BaseRevision: snapshot.Revision(), Replacements: []edit.Replacement{{Span: span, ExpectedText: old, NewText: newText}}}})
	}
	implicit := "root"
	if len(input.Config.Scopes) > 0 {
		implicit = "unassigned"
	}
	add(true, implicit, root, field(root, "schemaVersion"))
	if scopes := field(root, "scopes"); scopes != nil && scopes.Kind == yaml.SequenceNode {
		for _, scope := range scopes.Content {
			name := field(scope, "name")
			if name != nil {
				add(false, name.Value, scope, name)
			}
		}
	}
	return out
}

// Decoded YAML values may fold physical lines; edits require the original token
// to fit on one line. Apply the same rule to insertion anchors and replacements.
func singleLineScalarLength(node *yaml.Node, raw string) int {
	if node.Kind != yaml.ScalarNode || len(raw) == 0 {
		return 0
	}
	length := 0
	switch node.Style {
	case yaml.SingleQuotedStyle:
		for i := 1; i < len(raw); i++ {
			if raw[i] == '\r' || raw[i] == '\n' {
				return 0
			}
			if raw[i] == '\'' {
				if i+1 < len(raw) && raw[i+1] == '\'' {
					i++
					continue
				}
				length = i + 1
				break
			}
		}
	case yaml.DoubleQuotedStyle:
		escaped := false
		for i := 1; i < len(raw); i++ {
			if raw[i] == '\r' || raw[i] == '\n' {
				return 0
			}
			if raw[i] == '"' && !escaped {
				length = i + 1
				break
			}
			if raw[i] == '\\' && !escaped {
				escaped = true
			} else {
				escaped = false
			}
		}
	case 0:
		length = len(node.Value)
	default:
		return 0
	}
	if length == 0 || length > len(raw) || strings.ContainsAny(raw[:length], "\r\n") {
		return 0
	}
	return length
}
