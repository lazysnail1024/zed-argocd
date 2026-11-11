// Package yamlview provides an immutable, lossless structural YAML view.
package yamlview

import (
	"context"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview/internal/parser"
	"strings"
)

type NodeKind uint8

const (
	Empty NodeKind = iota
	Mapping
	Sequence
	Scalar
	Alias
	Error
)

type Node struct {
	kind     NodeKind
	span     source.Span
	raw      string
	damaged  bool
	members  []Member
	elements []Node
}
type Member struct{ Key, Value Node }
type ParseIssue struct {
	Span    source.Span
	Message string
}
type Document struct {
	root       Node
	span       source.Span
	raw        string
	contentEnd int
	complete   bool
}
type View struct {
	documents []Document
	issues    []ParseIssue
}

func (v View) Documents() []Document { return append([]Document(nil), v.documents...) }
func (v View) Issues() []ParseIssue  { return append([]ParseIssue(nil), v.issues...) }
func (d Document) Root() Node        { return d.root }
func (d Document) Span() source.Span { return d.span }
func (d Document) Complete() bool    { return d.complete }

// ContentSpan preserves semantic bytes while omitting a suffix proven to be
// ASCII comments and whitespace. The full original document must still match
// the supplied snapshot; Span and all node ranges remain lossless.
func (d Document) ContentSpan(s source.Snapshot) (source.Span, error) {
	raw, err := s.Slice(d.span)
	if err != nil {
		return source.Span{}, err
	}
	if raw != d.raw {
		return source.Span{}, fmt.Errorf("yamlview: document does not belong to snapshot")
	}
	return source.Span{Start: d.span.Start, End: d.contentEnd}, nil
}

func (n Node) Kind() NodeKind    { return n.kind }
func (n Node) Span() source.Span { return n.span }
func (n Node) Members() []Member { return append([]Member(nil), n.members...) }
func (n Node) Elements() []Node  { return append([]Node(nil), n.elements...) }
func (n Node) Raw() string       { return n.raw }
func (n Node) Damaged() bool     { return n.damaged }
func Parse(ctx context.Context, s source.Snapshot) (View, error) {
	if e := ctx.Err(); e != nil {
		return View{}, e
	}
	if len(s.Text()) > 5*1024*1024 {
		return View{}, fmt.Errorf("yamlview: input exceeds 5 MiB")
	}
	// Explicit document markers at column zero are synchronization boundaries
	// even when an earlier flow collection is unclosed. Block scalar content is
	// indented, and therefore never participates in this boundary scan.
	text := s.Text()
	starts := []int{0}
	for i := 0; i < len(text); {
		end := strings.IndexByte(text[i:], '\n')
		if end < 0 {
			end = len(text) - i
		}
		line := text[i : i+end]
		if i > 0 && strings.HasPrefix(line, "---") && (len(line) == 3 || line[3] == ' ' || line[3] == '\t' || line[3] == '\r') {
			starts = append(starts, i)
		}
		i += end + 1
	}
	if len(starts) > 2001 {
		return View{}, fmt.Errorf("yamlview: stream exceeds 2000 documents")
	}
	starts = append(starts, len(text))
	v := View{}
	for i := 0; i < len(starts)-1; i++ {
		offset := starts[i]
		tree, e := parser.Parse(ctx, text[offset:starts[i+1]])
		if e != nil {
			return View{}, e
		}
		var relocate func(*parser.Node)
		relocate = func(n *parser.Node) {
			n.Start += offset
			n.End += offset
			for j := range n.Children {
				relocate(&n.Children[j])
			}
		}
		relocate(&tree)
		var issues func(parser.Node)
		issues = func(n parser.Node) {
			if n.Kind == "ERROR" || n.Missing {
				v.issues = append(v.issues, ParseIssue{source.Span{Start: n.Start, End: n.End}, "invalid YAML syntax"})
			}
			for _, c := range n.Children {
				issues(c)
			}
		}
		issues(tree)
		if tree.Kind == "ERROR" {
			v.documents = append(v.documents, document(tree, text, false))
			continue
		}
		for _, c := range tree.Children {
			if c.Kind == "document" {
				v.documents = append(v.documents, document(c, text, !c.Damaged))
			} else if c.Damaged {
				v.documents = append(v.documents, document(c, text, false))
			}
		}
	}
	if len(v.documents) > 2000 {
		return View{}, fmt.Errorf("yamlview: stream exceeds 2000 documents")
	}
	if e := ctx.Err(); e != nil {
		return View{}, e
	}
	return v, nil
}

func document(p parser.Node, text string, complete bool) Document {
	d := Document{root: convert(p, text), span: source.Span{Start: p.Start, End: p.End}, raw: text[p.Start:p.End], contentEnd: p.End, complete: complete}
	// Tabs can be invalid indentation even when the CST accepts them. Keep
	// the whole document so truncation cannot turn a rejected line into EOF.
	if !complete || strings.IndexByte(d.raw, '\t') >= 0 {
		return d
	}
	// Walk real CST comments backwards. Source gaps must contain only YAML
	// whitespace; documents containing tabs were already excluded above.
	// Closing punctuation, null-value colons, tags, anchors and
	// document markers must never be removed as a side effect of this bound.
	var trim func(parser.Node) bool
	trim = func(n parser.Node) bool {
		if n.Kind == "comment" {
			if n.End > d.contentEnd || strings.Trim(text[n.End:d.contentEnd], " \t\r\n") != "" {
				return false
			}
			// The pinned CST and strict YAML decoders disagree about some
			// Unicode line separators and invalid control characters inside
			// comments. Keep those bytes for the strict decoder to interpret.
			for i := n.Start; i < n.End; i++ {
				if text[i] < ' ' || text[i] > '~' {
					return false
				}
			}
			d.contentEnd = n.Start
			return true
		}
		for i := len(n.Children) - 1; i >= 0; i-- {
			if !trim(n.Children[i]) {
				return false
			}
		}
		return len(n.Children) != 0
	}
	trim(p)
	return d
}

func convert(p parser.Node, text string) Node {
	n := Node{span: source.Span{Start: p.Start, End: p.End}, raw: text[p.Start:p.End], damaged: p.Damaged}
	switch p.Kind {
	case "ERROR":
		n.kind = Error
		return n
	case "block_mapping", "flow_mapping":
		n.kind = Mapping
		for _, c := range p.Children {
			if c.Kind == "block_mapping_pair" || c.Kind == "flow_pair" {
				m := Member{}
				for i, f := range c.Fields {
					if f == "key" {
						m.Key = convert(c.Children[i], text)
					}
					if f == "value" {
						m.Value = convert(c.Children[i], text)
					}
				}
				n.members = append(n.members, m)
			}
		}
		return n
	case "block_sequence", "flow_sequence":
		n.kind = Sequence
		for _, c := range p.Children {
			if c.Kind != "comment" {
				n.elements = append(n.elements, convert(c, text))
			}
		}
		return n
	case "alias":
		n.kind = Alias
		return n
	case "block_scalar", "plain_scalar", "double_quote_scalar", "single_quote_scalar":
		n.kind = Scalar
		return n
	case "document", "block_node", "flow_node", "block_sequence_item":
		for _, c := range p.Children {
			if c.Kind == "comment" || c.Kind == "anchor" || c.Kind == "tag" || strings.HasSuffix(c.Kind, "directive") {
				continue
			}
			child := convert(c, text)
			child.damaged = child.damaged || p.Damaged
			return child
		}
		n.kind = Empty
		n.raw = ""
		n.span = source.Span{Start: p.End, End: p.End}
		return n
	}
	if p.Missing || p.Damaged {
		n.kind = Error
	} else {
		n.kind = Scalar
	}
	return n
}
