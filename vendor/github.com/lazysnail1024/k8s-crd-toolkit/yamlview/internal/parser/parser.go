// Package parser owns every Tree-sitter object. Only copied Go values escape.
package parser

import (
	"context"
	"fmt"
	yaml "github.com/lazysnail1024/k8s-crd-toolkit/yamlview/internal/parser/grammar"
	sitter "github.com/tree-sitter/go-tree-sitter"
	"runtime"
	"sync"
)

type Node struct {
	Kind             string
	Start, End       int
	Damaged, Missing bool
	Children         []Node
	Fields           []string
}

// A call owns its parser and input across native timeout slices. Only the
// worker receiving the job may access them; the result hands ownership back.
type parseCall struct {
	ctx    context.Context
	text   string
	data   []byte
	parser *sitter.Parser
}

type parseJob struct {
	call   *parseCall
	result chan parseResult
}

type parseResult struct {
	tree *sitter.Tree
	err  error
}

var parseJobs = make(chan parseJob)
var startParsers sync.Once

// A separate frame releases each job before the worker waits again. Each
// native slice yields to other callers, including small foreground parses.
func runJob(job parseJob) {
	tree, err := job.call.slice()
	job.result <- parseResult{tree, err}
}

func Parse(ctx context.Context, text string) (Node, error) {
	if err := ctx.Err(); err != nil {
		return Node{}, err
	}
	startParsers.Do(func() {
		for i := 0; i < 2; i++ {
			go func() {
				runtime.LockOSThread()
				for {
					runJob(<-parseJobs)
				}
			}()
		}
	})
	result := make(chan parseResult, 1)
	job := parseJob{&parseCall{ctx: ctx, text: text}, result}
	select {
	case parseJobs <- job:
	case <-ctx.Done():
		return Node{}, ctx.Err()
	}
	for {
		// An admitted call must await cleanup even after cancellation. A nil
		// result means native parsing paused; enqueue its next slice behind
		// callers already waiting instead of occupying a worker until complete.
		value := <-result
		if value.err != nil {
			return Node{}, value.err
		}
		if value.tree != nil {
			defer value.tree.Close()
			return copyNode(ctx, value.tree.RootNode(), 0)
		}
		parseJobs <- job
	}
}

func (call *parseCall) closeParser() {
	if call.parser != nil {
		call.parser.Close()
		call.parser = nil
	}
	call.data = nil
	call.text = ""
}

func (call *parseCall) slice() (*sitter.Tree, error) {
	if err := call.ctx.Err(); err != nil {
		call.closeParser()
		return nil, err
	}
	// The scanner's budget flag is thread-local; parser/scanner state belongs
	// to this call. Reset and inspect the flag within each pinned native slice.
	yaml.Begin()
	if call.parser == nil {
		call.parser = sitter.NewParser()
		if err := call.parser.SetLanguage(sitter.NewLanguage(yaml.Language())); err != nil {
			call.closeParser()
			return nil, err
		}
		call.data = []byte(call.text)
		call.text = ""
		// v0.25.0 leaks the progress-options handle. Nil options release the
		// input handle. Native timeouts pause the same parser and input.
		call.parser.SetTimeoutMicros(10000)
	}
	tree := call.parser.Parse(call.data, nil)
	err := call.ctx.Err()
	if err == nil && yaml.Exceeded() {
		err = fmt.Errorf("yamlview: scanner state budget exceeded")
	}
	if err != nil {
		if tree != nil {
			tree.Close()
		}
		call.closeParser()
		return nil, err
	}
	if tree != nil {
		call.closeParser()
	}
	return tree, nil
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
