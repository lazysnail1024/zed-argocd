package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/schemaquery"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"sigs.k8s.io/yaml"
)

// The plugin selects release catalogs. The common query only compares the
// supplied branches with existing field structure; it has no version policy.
func automaticQueryDocument(ctx context.Context, doc Document, active []string) (Document, error) {
	if doc.Target != "auto" || doc.CompiledSchema == nil || len(doc.CompiledSchema.AnyOf) < 2 {
		return doc, nil
	}
	possible, err := schemaquery.SelectShapes(ctx, doc.CompiledSchema.AnyOf, doc.Value, active, schemaquery.Limits{})
	if err != nil {
		return Document{}, err
	}
	// Keep repair suggestions for an inconsistent automatic-version document.
	if len(possible) == 0 || len(possible) == len(doc.CompiledSchema.AnyOf) {
		return doc, nil
	}
	selected := *doc.CompiledSchema
	selected.AnyOf = possible
	doc.CompiledSchema = &selected
	return doc, nil
}

func fieldCompletions(ctx context.Context, doc Document, parent []string, prefix string, r Range, suffix string, current string) []Completion {
	doc, err := automaticQueryDocument(ctx, doc, appendPath(parent, current))
	if err != nil {
		return nil
	}
	query, err := schemaquery.Lookup(ctx, doc.CompiledSchema, parent, doc.Value, schemaquery.Limits{})
	if err != nil {
		return nil
	}
	fields := map[string]bool{}
	existing := object(valueAt(doc.Value, parent))
	for _, node := range query.Nodes {
		for name := range node.Properties {
			if !strings.HasPrefix(name, prefix) {
				continue
			}
			if _, exists := existing[name]; exists && name != current {
				continue
			}
			fields[name] = true
		}
	}
	result := []Completion{}
	for name := range fields {
		path := appendPath(parent, name)
		allowed, err := schemaquery.AllowsKey(ctx, doc.CompiledSchema, path, doc.Value, schemaquery.Limits{})
		if err != nil {
			return nil
		}
		if !allowed {
			continue
		}
		kind, err := schemaquery.Type(ctx, doc.CompiledSchema, path, doc.Value, schemaquery.Limits{})
		if err != nil {
			return nil
		}
		detail := kind + "; Argo CD " + doc.Target
		if query.Ambiguous {
			detail += "; candidate (branch unresolved)"
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
			if items := Complete(ctx, doc, p); len(items) > 0 {
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
	// Later damage cannot invalidate the completed prefix containing this key.
	// Keep original prefix offsets; this transient tree never contributes symbols.
	damaged := false
	for _, d := range view.Documents() {
		if start+indent >= d.Span().Start && start+indent <= d.Span().End && !d.Complete() {
			damaged = true
		}
	}
	if damaged {
		cutoff := strings.IndexByte(patched[start+indent:], '\n')
		if cutoff >= 0 {
			snapshot, err = source.New(s.URI(), s.Revision(), patched[:start+indent+cutoff+1])
			if err != nil {
				return nil
			}
			view, err = yamlview.Parse(ctx, snapshot)
			if err != nil {
				return nil
			}
		}
	}
	for _, d := range view.Documents() {
		if !d.Complete() || start+indent < d.Span().Start || start+indent > d.Span().End {
			continue
		}
		root := d.Root()
		if oversizedScalar(root) {
			continue
		}
		projected, e := projectValues(ctx, snapshot, d)
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
		return fieldCompletions(ctx, doc, parts[:len(parts)-1], prefix, Range{Position{begin.Line, begin.UTF16Column}, p}, ": ", marker)
	}
	return nil
}

func schemaHoverText(ctx context.Context, doc Document, parts []string) string {
	doc, err := automaticQueryDocument(ctx, doc, parts)
	if err != nil {
		return ""
	}
	query, err := schemaquery.Lookup(ctx, doc.CompiledSchema, parts, doc.Value, schemaquery.Limits{})
	if err != nil || len(query.Nodes) == 0 {
		return ""
	}
	kind, err := schemaquery.Type(ctx, doc.CompiledSchema, parts, doc.Value, schemaquery.Limits{})
	if err != nil {
		return ""
	}
	descriptions := []string{}
	seen := map[string]bool{}
	for _, node := range query.Nodes {
		if d := node.Description; d != "" && !seen[d] {
			descriptions = append(descriptions, d)
			seen[d] = true
		}
	}
	text := strings.Join(descriptions, "\n\n") + fmt.Sprintf("\n\nType: %s\n\nTarget: %s\n\nSource: embedded Argo CD CRD", kind, doc.Target) + "\n\n" + assets.SchemaSource(doc.Target, doc.Kind)
	for _, node := range query.Nodes {
		if value := node.Default; value != nil {
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
