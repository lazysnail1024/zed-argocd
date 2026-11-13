package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/schemaquery"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"sigs.k8s.io/yaml"
	"sort"
	"strconv"
	"strings"
)

type Location struct {
	URI   string `json:"uri"`
	Range Range  `json:"range"`
}
type Symbol struct {
	Name           string `json:"name"`
	Kind           int    `json:"kind"`
	Range          Range  `json:"range"`
	SelectionRange Range  `json:"selectionRange"`
}
type TextEdit struct {
	Range   Range  `json:"range"`
	NewText string `json:"newText"`
}
type Completion struct {
	Label    string   `json:"label"`
	Kind     int      `json:"kind"`
	Detail   string   `json:"detail"`
	TextEdit TextEdit `json:"textEdit"`
}
type Hover struct {
	Contents struct {
		Kind  string `json:"kind"`
		Value string `json:"value"`
	} `json:"contents"`
	Range Range `json:"range"`
}

func Symbols(documents []Document, uri string) []Symbol {
	result := []Symbol{}
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) != uri {
			continue
		}
		name := literal(object(doc.Value["metadata"])["name"])
		if name == "" || !doc.exact([]string{"metadata", "name"}) {
			continue
		}
		result = append(result, Symbol{Name: name, Kind: 19, Range: doc.rangeAt(nil), SelectionRange: doc.rangeAt([]string{"metadata", "name"})})
	}
	return result
}
func Definition(documents []Document, options DomainOptions, uri string, p Position) []Location {
	result := []Location{}
	documents = definitionDocuments(documents)
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) == uri {
			if name, definitions, _, found := sourceReferences(doc, p); found {
				return definitions[name]
			}
		}
	}
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) != uri || doc.Kind != "Application" {
			continue
		}
		if !doc.exact([]string{"spec", "project"}) {
			continue
		}
		node := doc.location([]string{"spec", "project"})
		if !contains(doc, node, p) {
			continue
		}
		project := literal(object(doc.Value["spec"])["project"])
		if project == "" {
			continue
		}
		for _, candidate := range documents {
			meta := object(candidate.Value["metadata"])
			if candidate.Kind == "AppProject" && candidate.exact([]string{"metadata", "name"}) && literal(meta["name"]) == project && (options.ControlPlaneNamespace == "" || literal(meta["namespace"]) == options.ControlPlaneNamespace) {
				result = append(result, Location{string(candidate.Snapshot.URI()), candidate.rangeAt([]string{"metadata", "name"})})
			}
		}
	}
	return result
}

func References(documents []Document, options DomainOptions, uri string, p Position, includeDeclaration bool) []Location {
	result := []Location{}
	documents = queryDocuments(documents)
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) == uri {
			if name, definitions, uses, found := sourceReferences(doc, p); found {
				if len(definitions[name]) != 1 {
					return result
				}
				result = append(result, uses[name]...)
				if includeDeclaration {
					result = append(result, definitions[name]...)
				}
				return result
			}
		}
	}
	project := ""
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) != uri {
			continue
		}
		if doc.Kind == "AppProject" && doc.exact([]string{"metadata", "name"}) && contains(doc, locate(doc.Root, []string{"metadata", "name"}), p) && literal(object(doc.Value["metadata"])["namespace"]) == options.ControlPlaneNamespace {
			project = literal(object(doc.Value["metadata"])["name"])
		} else if doc.Kind == "Application" && doc.exact([]string{"spec", "project"}) && contains(doc, locate(doc.Root, []string{"spec", "project"}), p) {
			project = literal(object(doc.Value["spec"])["project"])
		}
	}
	if project == "" || options.ControlPlaneNamespace == "" {
		return result
	}
	matches := 0
	for _, doc := range documents {
		meta := object(doc.Value["metadata"])
		if doc.Kind == "AppProject" && literal(meta["name"]) == project && literal(meta["namespace"]) == options.ControlPlaneNamespace {
			matches++
		}
	}
	if matches != 1 {
		return result
	}
	for _, doc := range documents {
		if doc.Kind == "Application" && literal(object(doc.Value["spec"])["project"]) == project {
			if doc.exact([]string{"spec", "project"}) {
				result = append(result, Location{string(doc.Snapshot.URI()), doc.rangeAt([]string{"spec", "project"})})
			}
		}
		if includeDeclaration && doc.Kind == "AppProject" && literal(object(doc.Value["metadata"])["name"]) == project && literal(object(doc.Value["metadata"])["namespace"]) == options.ControlPlaneNamespace {
			if doc.exact([]string{"metadata", "name"}) {
				result = append(result, Location{string(doc.Snapshot.URI()), doc.rangeAt([]string{"metadata", "name"})})
			}
		}
	}
	return result
}
func SchemaHover(ctx context.Context, doc Document, p Position) *Hover {
	offset, e := doc.Snapshot.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	if e != nil {
		return nil
	}
	parts, node, isKey, ok := at(doc.Root, offset, nil)
	if !ok || (!isKey && node.Kind() != yamlview.Scalar && node.Kind() != yamlview.Alias) {
		return nil
	}
	text := schemaHoverText(ctx, doc, parts)
	if text == "" {
		return nil
	}
	hover := &Hover{Range: nodeRange(doc, node)}
	hover.Contents.Kind = "markdown"
	hover.Contents.Value = text
	if origin := doc.origin(parts); origin.Kind != "exact" {
		hover.Contents.Value += "\n\nValue source: " + origin.Kind
	}
	return hover
}
func Complete(ctx context.Context, doc Document, p Position) []Completion {
	result := []Completion{}
	offset, e := doc.Snapshot.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	if e != nil {
		return result
	}
	parts, node, isKey, ok := at(doc.Root, offset, nil)
	editRange := nodeRange(doc, node)
	separator, suffix := "", ""
	if empty, found := emptyValueAt(doc, doc.Root, offset, nil); found {
		parts = empty
		isKey = false
		ok = true
		editRange = Range{p, p}
		if offset < len(doc.Snapshot.Text()) && doc.Snapshot.Text()[offset] == '#' {
			suffix = " "
		}
		if offset > 0 && doc.Snapshot.Text()[offset-1] == ':' {
			separator = " "
		}
	}
	if !ok || !doc.exact(parts) {
		return result
	}
	if isKey && len(parts) > 0 {
		raw := node.Raw()
		start, end := node.Span().Start, node.Span().End
		if len(raw) >= 2 && (raw[0] == '"' || raw[0] == '\'') {
			if raw[len(raw)-1] != raw[0] || strings.ContainsAny(raw[1:len(raw)-1], "\\\"'") {
				return result
			}
			start++
			end--
		}
		if offset != end {
			return result
		}
		prefix := doc.Snapshot.Text()[start:end]
		a, e1 := doc.Snapshot.Position(start)
		b, e2 := doc.Snapshot.Position(end)
		if e1 != nil || e2 != nil {
			return result
		}
		return fieldCompletions(ctx, doc, parts[:len(parts)-1], prefix, Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}, "", parts[len(parts)-1])
	} else {
		doc, err := automaticQueryDocument(ctx, doc, parts)
		if err != nil {
			return nil
		}
		values, err := schemaquery.Values(ctx, doc.CompiledSchema, parts, doc.Value, schemaquery.Limits{})
		if err != nil {
			return nil
		}

		for _, value := range values {
			b, e := json.Marshal(value)
			if e != nil {
				continue
			}
			result = append(result, Completion{Label: fmt.Sprint(value), Kind: 20, Detail: "Argo CD " + doc.Target, TextEdit: TextEdit{Range: editRange, NewText: separator + string(b) + suffix}})
		}
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Label < result[j].Label })
	return result
}
func nodeRange(doc Document, node yamlview.Node) Range {
	a, _ := doc.Snapshot.Position(node.Span().Start)
	b, _ := doc.Snapshot.Position(node.Span().End)
	return Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}
}
func contains(doc Document, node yamlview.Node, p Position) bool {
	offset, e := doc.Snapshot.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	return e == nil && offset >= node.Span().Start && offset <= node.Span().End
}
func at(node yamlview.Node, offset int, parts []string) ([]string, yamlview.Node, bool, bool) {
	if offset < node.Span().Start || offset > node.Span().End {
		return nil, node, false, false
	}
	for _, m := range node.Members() {
		var key string
		if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &key) != nil {
			continue
		}
		path := appendPath(parts, key)
		if offset >= m.Key.Span().Start && offset <= m.Key.Span().End {
			return path, m.Key, true, true
		}
		if p, n, k, ok := at(m.Value, offset, path); ok {
			return p, n, k, ok
		}
	}
	for i, child := range node.Elements() {
		if p, n, k, ok := at(child, offset, appendPath(parts, fmt.Sprint(i))); ok {
			return p, n, k, ok
		}
	}
	return parts, node, false, true
}
func valueAt(value any, parts []string) any {
	for _, p := range parts {
		switch v := value.(type) {
		case map[string]any:
			value = v[p]
		case []any:
			i, err := strconv.Atoi(p)
			if err != nil || i < 0 || i >= len(v) {
				return nil
			}
			value = v[i]
		default:
			return nil
		}
	}
	return value
}

func queryDocuments(documents []Document) []Document {
	result := []Document{}
	for _, doc := range documents {
		if doc.Kind == "ApplicationSet" {
			embedded, _ := appSetVariants(doc)
			result = append(result, embedded...)
		} else {
			result = append(result, doc)
		}
	}
	return result
}

// Definition inspects physical generator template leaves without claiming that
// their partial template is a fully merged controller-generated Application.
// Policy and reverse references continue using the authoritative projection.
func definitionDocuments(documents []Document) []Document {
	result := queryDocuments(documents)
	for _, doc := range documents {
		if doc.Kind != "ApplicationSet" {
			continue
		}
		for i, generator := range objectSlice(object(doc.Value["spec"])["generators"]) {
			for kind, body := range object(generator) {
				if kind == "selector" {
					continue
				}
				parts := []string{"spec", "generators", fmt.Sprint(i), kind, "template"}
				value := object(object(body)["template"])
				if value == nil {
					continue
				}
				result = append(result, Document{Snapshot: doc.Snapshot, Root: locate(doc.Root, parts), Value: value, Origin: doc.originBelow(parts), Kind: "Application", Target: doc.Target})
			}
		}
	}
	return result
}

func sourceReferences(doc Document, p Position) (string, map[string][]Location, map[string][]Location, bool) {
	definitions, uses := map[string][]Location{}, map[string][]Location{}
	selected := ""
	declarations := map[string]int{}
	if doc.Kind != "Application" {
		return "", definitions, uses, false
	}
	if !sourceRefsActive(doc.Target, len(objectSlice(object(doc.Value["spec"])["sources"]))) {
		return "", definitions, uses, false
	}
	for i, value := range objectSlice(object(doc.Value["spec"])["sources"]) {
		source := object(value)
		prefix := []string{"spec", "sources", fmt.Sprint(i)}
		if name := literal(source["ref"]); name != "" {
			declarations[name]++
			if doc.exact(appendPath(prefix, "ref")) {
				node := locate(doc.Root, appendPath(prefix, "ref"))
				definitions[name] = append(definitions[name], Location{string(doc.Snapshot.URI()), nodeRange(doc, node)})
				if contains(doc, node, p) {
					selected = name
				}
			}
		}
		for _, use := range helmValuePaths(object(source["helm"])) {
			file := literal(use.value)
			if !strings.HasPrefix(file, "$") {
				continue
			}
			name := strings.SplitN(file[1:], "/", 2)[0]
			if !doc.exact(appendPath(prefix, append([]string{"helm"}, use.path...)...)) {
				continue
			}
			node := locate(doc.Root, appendPath(prefix, append([]string{"helm"}, use.path...)...))
			uses[name] = append(uses[name], Location{string(doc.Snapshot.URI()), nodeRange(doc, node)})
			if contains(doc, node, p) {
				selected = name
			}
		}
	}
	// Non-exact declarations still participate in semantic ambiguity. A partial
	// location list must never masquerade as the complete declaration population.
	for name, count := range declarations {
		if count != len(definitions[name]) {
			definitions[name] = []Location{}
		}
	}
	return selected, definitions, uses, selected != ""
}

// RecoverProjectUse proves both a complete physical use and unambiguous
// membership in the current document. It never contributes declarations,
// symbols, diagnostics or edit plans.
func RecoverProjectUse(ctx context.Context, snapshot source.Snapshot, target string, p Position) *Document {
	offset, err := snapshot.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	if err != nil {
		return nil
	}
	recovered := recoverReferencePrefix(ctx, snapshot, target, offset)
	parts := []string{"spec", "project"}
	if recovered == nil || recovered.document.Kind != "Application" || !recovered.permits(ctx, parts) {
		return nil
	}
	doc := recovered.document
	node := doc.location(parts)
	if _, ok := referenceLiteral(node); ok && contains(doc, node, p) {
		return &doc
	}
	return nil
}
