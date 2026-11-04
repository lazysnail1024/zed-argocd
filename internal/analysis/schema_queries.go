package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"sigs.k8s.io/yaml"
)

// Query traversal reads the invocation's compiled metadata. Reference resolution
// and leaf constraint validation belong to the pinned validator, not a second
// map-based schema interpreter. Compiled nodes are never changed by queries.
type queryFacet struct {
	schema    *schema.Schema
	candidate bool
}

func branchPossible(s *schema.Schema, value any, ignore string, depth int) bool {
	if s == nil || depth > 64 {
		return depth <= 64
	}
	for name, child := range s.Properties {
		v, exists := object(value)[name]
		if exists && name != ignore && !discriminatorAccepts(child, v, depth+1) {
			return false
		}
	}
	if !branchPossible(s.Ref, value, ignore, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !branchPossible(child, value, ignore, depth+1) {
			return false
		}
	}
	for _, alternatives := range [][]*schema.Schema{s.AnyOf, s.OneOf} {
		if len(alternatives) > 0 {
			possible := false
			for _, child := range alternatives {
				possible = possible || branchPossible(child, value, ignore, depth+1)
			}
			if !possible {
				return false
			}
		}
	}
	return true
}
func discriminatorAccepts(s *schema.Schema, value any, depth int) bool {
	if s == nil {
		return true
	}
	if depth > 64 {
		return false
	}
	// Only explicit enum/const facts select branches. Missing fields and unrelated
	// required/type errors must not suppress suggestions while editing.
	if s.Const != nil || s.Enum != nil {
		if s.Validate(value) != nil {
			return false
		}
	}
	if !discriminatorAccepts(s.Ref, value, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !discriminatorAccepts(child, value, depth+1) {
			return false
		}
	}
	return true
}
func queryFacets(s *schema.Schema, value any, ignore string, candidate bool, depth int, budget *int) []queryFacet {
	*budget--
	if s == nil || depth > 64 || *budget < 0 {
		return nil
	}
	result := []queryFacet{{s, candidate}}
	result = append(result, queryFacets(s.Ref, value, ignore, candidate, depth+1, budget)...)
	for _, child := range s.AllOf {
		result = append(result, queryFacets(child, value, ignore, candidate, depth+1, budget)...)
	}
	for _, alternatives := range [][]*schema.Schema{s.AnyOf, s.OneOf} {
		selected := []*schema.Schema{}
		for _, child := range alternatives {
			if branchPossible(child, value, ignore, 0) {
				selected = append(selected, child)
			}
		}
		for _, child := range selected {
			result = append(result, queryFacets(child, value, ignore, candidate || len(selected) > 1, depth+1, budget)...)
		}
	}
	return result
}
func childSchema(s *schema.Schema, part string, value any) *schema.Schema {
	if _, ok := value.([]any); ok {
		return s.Items2020
	}
	if child := s.Properties[part]; child != nil {
		return child
	}
	child, _ := s.AdditionalProperties.(*schema.Schema)
	return child
}
func queryAt(doc Document, parts []string) []queryFacet {
	budget := 4096
	facets := []queryFacet{{doc.CompiledSchema, false}}
	var value any = doc.Value
	for _, part := range parts {
		next := []queryFacet{}
		for _, f := range facets {
			for _, expanded := range queryFacets(f.schema, value, part, f.candidate, 0, &budget) {
				if child := childSchema(expanded.schema, part, value); child != nil {
					next = append(next, queryFacet{child, expanded.candidate})
				}
			}
		}
		facets = next
		value = valueAt(value, []string{part})
	}
	result := []queryFacet{}
	for _, f := range facets {
		result = append(result, queryFacets(f.schema, value, "", f.candidate, 0, &budget)...)
	}
	if budget < 0 {
		return nil
	}
	return result
}

// Keep every ancestor's AND/OR membership when evaluating a proposed leaf.
// The queried field is excluded from sibling discrimination, but its proposed
// value must satisfy the complete selected branch constraints at that leaf.
func queryAccepts(s *schema.Schema, parts []string, value, candidate any, depth int) bool {
	if s == nil {
		return true
	}
	if depth > 64 {
		return false
	}
	if len(parts) == 0 {
		return s.Validate(candidate) == nil
	}
	part := parts[0]
	childValue := valueAt(value, []string{part})
	if child := childSchema(s, part, value); child != nil && !queryAccepts(child, parts[1:], childValue, candidate, depth+1) {
		return false
	}
	if !queryAccepts(s.Ref, parts, value, candidate, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !queryAccepts(child, parts, value, candidate, depth+1) {
			return false
		}
	}
	for kind, alternatives := range [][]*schema.Schema{s.AnyOf, s.OneOf} {
		if len(alternatives) > 0 {
			matched := 0
			for _, child := range alternatives {
				if branchPossible(child, value, part, 0) && queryAccepts(child, parts, value, candidate, depth+1) {
					matched++
				}
			}
			if matched == 0 || (kind == 1 && matched != 1) {
				return false
			}
		}
	}
	return true
}
func queryType(facets []queryFacet) string {
	types := map[string]bool{}
	for _, f := range facets {
		if f.schema.Types != nil {
			for _, name := range f.schema.Types.ToStrings() {
				types[name] = true
			}
		}
	}
	names := []string{}
	for name := range types {
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) == 0 {
		return "unspecified"
	}
	return strings.Join(names, " | ")
}
func fieldCompletions(doc Document, parent []string, prefix string, r Range, suffix string, current string) []Completion {
	result := []Completion{}
	fields := map[string][]queryFacet{}
	existing := object(valueAt(doc.Value, parent))
	for _, f := range queryAt(doc, parent) {
		for name, raw := range f.schema.Properties {
			if !strings.HasPrefix(name, prefix) {
				continue
			}
			if _, ok := existing[name]; ok && name != current {
				continue
			}
			budget := 4096
			fields[name] = append(fields[name], queryFacets(raw, nil, "", f.candidate, 0, &budget)...)
		}
	}
	for name, facets := range fields {
		detail := queryType(facets) + "; Argo CD " + doc.Target
		for _, f := range facets {
			if f.candidate {
				detail += "; candidate (branch unresolved)"
				break
			}
		}
		result = append(result, Completion{Label: name, Kind: 10, Detail: detail, TextEdit: TextEdit{Range: r, NewText: name + suffix}})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Label < result[j].Label })
	return result
}

// FieldCompletions only repairs a single blank or partial block-key line for a
// read-only query. The repaired tree never enters analysis, indexes or edit fixes.
func FieldCompletions(ctx context.Context, s source.Snapshot, target string, documents []Document, p Position) []Completion {
	for _, doc := range documents {
		if doc.Snapshot.URI() == s.URI() {
			if items := Complete(doc, p); len(items) > 0 {
				return items
			}
		}
	}
	offset, err := s.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	if err != nil {
		return nil
	}
	text := s.Text()
	start := strings.LastIndex(text[:offset], "\n") + 1
	end := offset
	for end < len(text) && text[end] != '\n' {
		end++
	}
	before := text[start:offset]
	prefix := strings.TrimLeft(before, " ")
	indent := len(before) - len(prefix)
	if strings.HasPrefix(prefix, "- ") {
		prefix = strings.TrimLeft(prefix[2:], " ")
		indent = len(before) - len(prefix)
	}
	if indent == 0 || strings.TrimSpace(text[offset:end]) != "" {
		return nil
	}
	for _, c := range prefix {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
			return nil
		}
	}
	const marker = "argocdQueryFieldPlaceholder"
	patched := text[:start+indent] + marker + ": null" + text[offset:]
	snapshot, err := source.New(s.URI(), s.Revision(), patched)
	if err != nil {
		return nil
	}
	view, err := yamlview.Parse(ctx, snapshot)
	if err != nil {
		return nil
	}
	for _, d := range view.Documents() {
		if !d.Complete() || start+indent < d.Span().Start || start+indent > d.Span().End {
			continue
		}
		root := d.Root()
		if oversizedScalar(root) {
			continue
		}
		raw, e := documentText(snapshot, d.Span())
		if e != nil {
			continue
		}
		projected, e := projectValues(ctx, raw, root)
		if e != nil {
			continue
		}
		value := object(projected.Value)
		api, kind := literal(value["apiVersion"]), literal(value["kind"])
		if api != "argoproj.io/v1alpha1" || (kind != "Application" && kind != "ApplicationSet" && kind != "AppProject") {
			continue
		}
		parts, node, isKey, ok := at(root, start+indent+len(marker), nil)
		if !ok || !isKey || node.Raw() != marker || len(parts) == 0 {
			continue
		}
		if err := ctx.Err(); err != nil {
			return nil
		}
		var compiled *schema.Schema
		for _, original := range documents {
			if original.Target == target && original.Kind == kind {
				compiled = original.CompiledSchema
				break
			}
		}
		if compiled == nil {
			data, e := assets.SchemaData(target, api, kind)
			if e != nil {
				return nil
			}
			compiled, e = assets.CompileSchema(data)
			if e != nil {
				return nil
			}
		}
		doc := Document{Snapshot: snapshot, Root: root, Value: value, Origin: projected.Origin, Kind: kind, Target: target, CompiledSchema: compiled}
		begin, e := s.Position(start + indent)
		if e != nil {
			return nil
		}
		if !doc.exact(parts) {
			continue
		}
		return fieldCompletions(doc, parts[:len(parts)-1], prefix, Range{Position{begin.Line, begin.UTF16Column}, p}, ": ", marker)
	}
	return nil
}

func schemaHoverText(doc Document, parts []string) string {
	facets := queryAt(doc, parts)
	if len(facets) == 0 {
		return ""
	}
	descriptions := []string{}
	seen := map[string]bool{}
	for _, f := range facets {
		d := f.schema.Description
		if d != "" && !seen[d] {
			descriptions = append(descriptions, d)
			seen[d] = true
		}
	}
	text := strings.Join(descriptions, "\n\n") + fmt.Sprintf("\n\nType: %s\n\nTarget: %s\n\nSource: embedded Argo CD CRD", queryType(facets), doc.Target) + "\n\n" + assets.SchemaSource(doc.Target, doc.Kind)
	for _, f := range facets {
		if value := f.schema.Default; value != nil {
			b, _ := json.Marshal(*value)
			text += "\n\nDefault: " + string(b)
			break
		}
	}
	return text
}

// An absent scalar has no CST token. Accept only the whitespace after this
// member's colon on its own physical line; never extend into a comment.
func emptyValueAt(doc Document, node yamlview.Node, offset int, parts []string) ([]string, bool) {
	for _, member := range node.Members() {
		var key string
		if yaml.UnmarshalStrict([]byte(member.Key.Raw()), &key) != nil {
			continue
		}
		path := appendPath(parts, key)
		end := member.Key.Span().End
		if member.Value.Kind() == yamlview.Empty && offset > end {
			between := doc.Snapshot.Text()[end:offset]
			if strings.HasPrefix(between, ":") && strings.Trim(between[1:], " \t") == "" {
				return path, true
			}
		}
		if path, ok := emptyValueAt(doc, member.Value, offset, path); ok {
			return path, true
		}
	}
	for i, child := range node.Elements() {
		if path, ok := emptyValueAt(doc, child, offset, appendPath(parts, fmt.Sprint(i))); ok {
			return path, true
		}
	}
	return nil, false
}
