package yamlvalue

import (
	"bytes"
	"context"
	"encoding/json"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	yamlv3 "gopkg.in/yaml.v3"
	"sigs.k8s.io/yaml"
	"strings"
)

// Selected root fields are bounded scalar facts, never a partial value tree.
// Unrelated recursive values do not erase independently known fields.
// strictScalar preserves the pinned strict YAML scalar semantics for values
// and the bounded ownership lookup. It does not expand aliases or collections.
func strictScalar(n *yamlv3.Node) (any, error) {
	if n == nil || n.Kind != yamlv3.ScalarNode || (n.Tag != "" && !strings.HasPrefix(n.LongTag(), "tag:yaml.org,2002:")) {
		return nil, projectionFailure("Only standard YAML scalars can establish values")
	}
	if len(n.Value) > 1024*1024 {
		return nil, &Error{"INPUT_BUDGET_EXCEEDED", "scalar exceeds 1 MiB limit"}
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

func selectedFields(ctx context.Context, root *yamlv3.Node, names []string) map[string]string {
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
	result := map[string]string{}
	for _, name := range names {
		if value := scalar(name); value != "" {
			result[name] = value
		}
	}
	return result
}

// A failed parse exposes no partial yaml.v3 tree. One bounded wrapper parse
// supplies null placeholders for aliases, preserving original definitions and
// their order. Only selected root fields are queried; recovered values are never admitted.
func recoverFields(ctx context.Context, raw string, syntax yamlview.Node) *yamlv3.Node {
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
