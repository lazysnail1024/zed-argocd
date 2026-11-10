package analysis

import (
	"context"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"sigs.k8s.io/yaml"
)

// A prefix can establish a scalar's bytes, but never its uniqueness in the
// current document. Every recovered use must also pass permits against the
// entire current document, including the text after the queried line.
type referenceRecovery struct {
	document Document
	span     source.Span
}

func recoverReferencePrefix(ctx context.Context, snapshot source.Snapshot, target string, offset int) *referenceRecovery {
	original, err := yamlview.Parse(ctx, snapshot)
	if err != nil {
		return nil
	}
	var damaged *yamlview.Document
	for _, document := range original.Documents() {
		if !document.Complete() && offset >= document.Span().Start && offset <= document.Span().End {
			damaged = &document
			break
		}
	}
	if damaged == nil {
		return nil
	}
	end := strings.IndexByte(snapshot.Text()[offset:], '\n')
	if end < 0 {
		end = len(snapshot.Text())
	} else {
		end += offset + 1
	}
	prefix, err := source.New(snapshot.URI(), snapshot.Revision(), snapshot.Text()[:end])
	if err != nil {
		return nil
	}
	view, err := yamlview.Parse(ctx, prefix)
	if err != nil {
		return nil
	}
	for _, document := range view.Documents() {
		root := document.Root()
		if !document.Complete() || offset < root.Span().Start || offset > root.Span().End || root.Span().Start < damaged.Span().Start || root.Span().End > damaged.Span().End || oversizedScalar(root) {
			continue
		}
		projected, err := projectValues(ctx, prefix, document)
		if err != nil {
			continue
		}
		value := object(projected.Value)
		kind := literal(value["kind"])
		if literal(value["apiVersion"]) != "argoproj.io/v1alpha1" || (kind != "Application" && kind != "ApplicationSet") {
			continue
		}
		return &referenceRecovery{Document{Snapshot: snapshot, Root: root, Kind: kind, Target: target, Value: value, Origin: projected.Origin}, damaged.Span()}
	}
	return nil
}

func (r referenceRecovery) permits(ctx context.Context, parts []string) bool {
	doc := r.document
	if !doc.exact(parts) || !exactReferencePath(doc.Root, parts) {
		return false
	}
	text := doc.Snapshot.Text()
	// A suffix alias can make a previously independent anchor shared. Prefix
	// projection has no evidence for it; graph/tag syntax is a hard boundary.
	for i := r.span.Start; i < r.span.End; i++ {
		if i%4096 == 0 && ctx.Err() != nil {
			return false
		}
		if strings.ContainsRune("*&!", rune(text[i])) && (i == r.span.Start || strings.ContainsRune(" \t\r\n[{,:?-", rune(text[i-1]))) {
			return false
		}
	}
	if !r.identityKnown(ctx) {
		return false
	}
	return r.permitsPath(ctx, parts)
}

func (r referenceRecovery) permitsPath(ctx context.Context, parts []string) bool {
	doc := r.document
	node, parentIndent := doc.Root, -1
	for i, part := range parts {
		if node.Kind() == yamlview.Sequence {
			// Source-ref completion may use a fully present block sequence even
			// when an unrelated later field is damaged. Never treat a prefix of
			// that sequence as its full declaration population.
			return r.sequenceComplete(node, parentIndent) && exactReferencePath(node, parts[i:])
		}
		keys, indent, ok := r.mappingKeys(ctx, node, parentIndent)
		if !ok {
			return false
		}
		found := false
		for _, member := range node.Members() {
			var key string
			if yaml.UnmarshalStrict([]byte(member.Key.Raw()), &key) == nil && key == part {
				if at, exists := keys[key]; !exists || at != member.Key.Span().Start {
					return false
				}
				if i == len(parts)-1 {
					return r.scalarComplete(ctx, member, key, indent, valueAt(doc.Value, parts))
				}
				node, parentIndent, found = member.Value, indent, true
				break
			}
		}
		if !found {
			return false
		}
	}
	return ctx.Err() == nil
}

// A plain scalar may continue after the queried physical line. Prove its whole
// current member value, not just its first-line bytes. This is validation only:
// the full member is never substituted into the read-only prefix projection.
func (r referenceRecovery) scalarComplete(ctx context.Context, member yamlview.Member, key string, indent int, expected any) bool {
	if member.Value.Kind() != yamlview.Scalar && member.Value.Kind() != yamlview.Empty {
		return false
	}
	text := r.document.Snapshot.Text()
	start := strings.LastIndexByte(text[:member.Key.Span().Start], '\n') + 1
	end := r.span.End
	for at := start; at < r.span.End; {
		if ctx.Err() != nil {
			return false
		}
		length := strings.IndexByte(text[at:r.span.End], '\n')
		if length < 0 {
			length = r.span.End - at
		}
		line := text[at : at+length]
		body := strings.TrimLeft(line, " \t")
		if at > start && len(line)-len(body) <= indent {
			if _, fragment, header := recoveryLineKey(body); header && !fragment {
				end = at
				break
			}
		}
		at += length + 1
	}
	var decoded map[string]any
	if yaml.UnmarshalStrict([]byte(text[start:end]), &decoded) != nil || len(decoded) != 1 || ctx.Err() != nil {
		return false
	}
	actual, exists := decoded[key]
	if !exists {
		return false
	}
	if expected == nil {
		return actual == nil
	}
	a, actualString := actual.(string)
	b, expectedString := expected.(string)
	return actualString && expectedString && a == b
}

func (r referenceRecovery) mappingKeys(ctx context.Context, node yamlview.Node, parentIndent int) (map[string]int, int, bool) {
	if node.Kind() != yamlview.Mapping || strings.HasPrefix(strings.TrimSpace(node.Raw()), "{") {
		return nil, 0, false
	}
	members := node.Members()
	if len(members) == 0 {
		return nil, 0, false
	}
	text := r.document.Snapshot.Text()
	start := members[0].Key.Span().Start
	lineStart := strings.LastIndexByte(text[:start], '\n') + 1
	indent := start - lineStart
	if strings.Trim(text[lineStart:start], " ") != "" || indent <= parentIndent {
		return nil, 0, false
	}
	keys, ok := recoveryMappingKeys(ctx, text, lineStart, r.span.End, indent, parentIndent, members)
	return keys, indent, ok
}

// Metadata may follow spec, and therefore be absent from the recovered prefix.
// Validate its entire physical block separately, without admitting any of its
// values into the recovery projection. Strict decoding catches duplicate name
// or namespace keys, including quoted keys and inline flow mappings.
func (r referenceRecovery) identityKnown(ctx context.Context) bool {
	doc := r.document
	keys, indent, ok := r.mappingKeys(ctx, doc.Root, -1)
	if !ok || !doc.exact([]string{"apiVersion"}) || !doc.exact([]string{"kind"}) {
		return false
	}
	start, exists := keys["metadata"]
	if !exists {
		return true
	}
	text := doc.Snapshot.Text()
	lineStart := strings.LastIndexByte(text[:start], '\n') + 1
	end := recoveryBlockEnd(text, lineStart, r.span.End, indent)
	var identity map[string]any
	if yaml.UnmarshalStrict([]byte(text[lineStart:end]), &identity) != nil {
		return false
	}
	metadata, ok := identity["metadata"].(map[string]any)
	if !ok {
		return false
	}
	for _, name := range []string{"name", "namespace"} {
		if value, exists := metadata[name]; exists {
			if _, ok := value.(string); !ok {
				return false
			}
		}
	}
	return ctx.Err() == nil
}

func (r referenceRecovery) sequenceComplete(node yamlview.Node, parentIndent int) bool {
	text := r.document.Snapshot.Text()
	start := strings.LastIndexByte(text[:node.Span().Start], '\n') + 1
	end := recoveryBlockEnd(text, start, r.span.End, parentIndent)
	if node.Span().End > end {
		return false
	}
	for _, line := range strings.Split(text[node.Span().End:end], "\n") {
		if rest := strings.TrimSpace(line); rest != "" && !strings.HasPrefix(rest, "#") {
			return false
		}
	}
	return true
}

func recoveryBlockEnd(text string, start, end, parentIndent int) int {
	for at := start; at < end; {
		length := strings.IndexByte(text[at:end], '\n')
		if length < 0 {
			length = end - at
		}
		line := text[at : at+length]
		body := strings.TrimLeft(line, " \t")
		if at > start && strings.TrimSpace(body) != "" && !strings.HasPrefix(body, "#") && len(line)-len(body) <= parentIndent {
			return at
		}
		at += length + 1
	}
	return end
}

// yamlview may expose an unclosed flow suffix as one opaque Error node. For
// that case, admit only block-mapping ancestry whose physical member headers
// can all be accounted for. Values are not repaired or projected here. Quoted
// keys use the ordinary strict decoder; complex keys, merges, flow ancestry,
// inconsistent indentation and uncertain headers fail closed.
func recoveryMappingKeys(ctx context.Context, text string, start, end, indent, parentIndent int, members []yamlview.Member) (map[string]int, bool) {
	keys := map[string]int{}
	for at := start; at < end; {
		if ctx.Err() != nil {
			return nil, false
		}
		length := strings.IndexByte(text[at:end], '\n')
		if length < 0 {
			length = end - at
		}
		line, lineStart := text[at:at+length], at
		at += length + 1
		body := strings.TrimLeft(line, " \t")
		if strings.TrimSpace(body) == "" || strings.HasPrefix(body, "#") {
			continue
		}
		column := len(line) - len(body)
		if column <= parentIndent {
			break
		}
		if strings.Contains(line[:column], "\t") || column < indent {
			return nil, false
		}
		if column > indent {
			continue
		}
		key, fragment, ok := recoveryLineKey(body)
		if !ok || key == "<<" {
			return nil, false
		}
		if fragment {
			if parentIndent < 0 || !unrelatedKeyFragment(key, members) {
				return nil, false
			}
			continue
		}
		if _, duplicate := keys[key]; duplicate {
			return nil, false
		}
		keys[key] = lineStart + column
	}
	return keys, true
}

func unrelatedKeyFragment(fragment string, members []yamlview.Member) bool {
	for _, member := range members {
		var key string
		if yaml.UnmarshalStrict([]byte(member.Key.Raw()), &key) != nil || strings.HasPrefix(key, fragment) {
			return false
		}
	}
	return true
}

func recoveryLineKey(line string) (string, bool, bool) {
	var quote byte
	for i := 0; i < len(line); i++ {
		char := line[i]
		if quote != 0 {
			if quote == '"' && char == '\\' {
				i++
			} else if char == quote {
				if quote == '\'' && i+1 < len(line) && line[i+1] == quote {
					i++
				} else {
					quote = 0
				}
			}
			continue
		}
		if char == '\'' || char == '"' {
			quote = char
			continue
		}
		if char == '#' && (i == 0 || line[i-1] == ' ' || line[i-1] == '\t') {
			line = line[:i]
			break
		}
		if char == ':' && (i+1 == len(line) || strings.ContainsRune(" \t\r", rune(line[i+1]))) {
			var value any
			if yaml.UnmarshalStrict([]byte(line[:i]), &value) != nil {
				return "", false, false
			}
			key, ok := value.(string)
			return key, false, ok && key != ""
		}
	}
	fragment := strings.TrimSpace(line)
	if quote != 0 || fragment == "" {
		return "", false, false
	}
	for _, char := range fragment {
		if !(char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' || char >= '0' && char <= '9' || char == '_' || char == '-') {
			return "", false, false
		}
	}
	return fragment, true, true
}
