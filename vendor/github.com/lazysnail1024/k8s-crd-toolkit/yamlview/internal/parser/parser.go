// Package parser owns every Tree-sitter object. Only copied Go values escape.
package parser

import (
	"context"
	"fmt"
	yaml "github.com/lazysnail1024/k8s-crd-toolkit/yamlview/internal/parser/grammar"
	sitter "github.com/tree-sitter/go-tree-sitter"
	"runtime"
)

type Node struct {
	Kind             string
	Start, End       int
	Damaged, Missing bool
	Children         []Node
	Fields           []string
}

func Parse(ctx context.Context, text string) (Node, error) {
	if e := ctx.Err(); e != nil {
		return Node{}, e
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	yaml.Begin()
	p := sitter.NewParser()
	defer p.Close()
	if e := p.SetLanguage(sitter.NewLanguage(yaml.Language())); e != nil {
		return Node{}, e
	}
	data := []byte(text)
	// v0.25.0 leaks the progress-options handle. Parse with nil options instead:
	// its input handle has an explicit Unref. Native timeouts pause (not reset)
	// this parser, allowing cancellation checks without retaining caller context.
	// The duration is a scheduling slice, never a total input budget.
	p.SetTimeoutMicros(10000)
	var t *sitter.Tree
	for {
		t = p.Parse(data, nil)
		if e := ctx.Err(); e != nil {
			if t != nil {
				t.Close()
			}
			return Node{}, e
		}
		if yaml.Exceeded() {
			if t != nil {
				t.Close()
			}
			return Node{}, fmt.Errorf("yamlview: scanner state budget exceeded")
		}
		if t != nil {
			break
		}
		// Resume the same input and parser state after its native timeout.
	}
	defer t.Close()
	return copyNode(ctx, t.RootNode(), 0)
}
func copyNode(ctx context.Context, n *sitter.Node, depth int) (Node, error) {
	if e := ctx.Err(); e != nil {
		return Node{}, e
	}
	k := n.Kind()
	if k == "block_mapping" || k == "flow_mapping" || k == "block_sequence" || k == "flow_sequence" {
		depth++
	}
	if depth > 256 {
		return Node{}, fmt.Errorf("yamlview: depth exceeds 256")
	}
	r := Node{Kind: k, Start: int(n.StartByte()), End: int(n.EndByte()), Damaged: n.HasError() || n.IsError() || n.IsMissing(), Missing: n.IsMissing()}
	for i := uint(0); i < n.NamedChildCount(); i++ {
		c, e := copyNode(ctx, n.NamedChild(i), depth)
		if e != nil {
			return Node{}, e
		}
		r.Children = append(r.Children, c)
		r.Fields = append(r.Fields, n.FieldNameForNamedChild(uint32(i)))
	}
	// Missing punctuation is anonymous in Tree-sitter, but still represents
	// observable syntax damage and must survive the copied tree for Issues.
	for i := uint(0); i < n.ChildCount(); i++ {
		child := n.Child(i)
		if child.IsMissing() && !child.IsNamed() {
			r.Children = append(r.Children, Node{Kind: child.Kind(), Start: int(child.StartByte()), End: int(child.EndByte()), Damaged: true, Missing: true})
			r.Fields = append(r.Fields, "")
		}
	}
	return r, nil
}
