package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// SourceRenameTarget is a document-private multi-source reference name.
type SourceRenameTarget struct {
	Range       Range  `json:"range"`
	Placeholder string `json:"placeholder"`
}

type sourceOccurrence struct {
	path       []string
	value      string
	definition bool
	name       string
	span       source.Span
	precise    bool
}

func sourceOccurrences(ctx context.Context, doc Document) ([]sourceOccurrence, error) {
	sources := objectSlice(object(doc.Value["spec"])["sources"])
	if doc.Kind != "Application" || !sourceRefsActive(doc.Target, len(sources)) {
		return nil, fmt.Errorf("rename is available for Application source ref names")
	}
	result := []sourceOccurrence{}
	add := func(path []string, value string, definition bool) error {
		name := value
		if !definition {
			if !strings.HasPrefix(value, "$") {
				return nil
			}
			name = strings.SplitN(value[1:], "/", 2)[0]
		}
		if strings.ContainsAny(name, "${}") {
			return fmt.Errorf("dynamic source names prevent a complete rename")
		}
		if name == "" {
			return nil
		}
		node := doc.location(path)
		raw, span := node.Raw(), node.Span()
		if len(raw) >= 2 && (raw[0] == '"' || raw[0] == '\'') && raw[len(raw)-1] == raw[0] {
			raw = raw[1 : len(raw)-1]
			span.Start++
			span.End--
		}
		precise := doc.exact(path) && !node.Damaged() && raw == value && !strings.ContainsAny(raw, "\r\n\\")
		if !definition {
			span.Start++
		}
		span.End = span.Start + len(name)
		result = append(result, sourceOccurrence{path, value, definition, name, span, precise})
		return nil
	}
	for i, value := range sources {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		src := object(value)
		path := []string{"spec", "sources", fmt.Sprint(i)}
		if err := add(appendPath(path, "ref"), literal(src["ref"]), true); err != nil {
			return nil, err
		}
		for _, use := range helmValuePaths(object(src["helm"])) {
			if err := add(appendPath(path, append([]string{"helm"}, use.path...)...), literal(use.value), false); err != nil {
				return nil, err
			}
		}
	}
	return result, nil
}

func sourceRenameSelection(ctx context.Context, doc Document, pos Position) (*SourceRenameTarget, []sourceOccurrence, error) {
	items, err := sourceOccurrences(ctx, doc)
	if err != nil {
		return nil, nil, err
	}
	offset, err := doc.Snapshot.Offset(source.Position{Line: pos.Line, UTF16Column: pos.Character})
	if err != nil {
		return nil, nil, err
	}
	var selected *sourceOccurrence
	for i := range items {
		item := &items[i]
		if item.precise && offset >= item.span.Start && offset <= item.span.End {
			selected = item
			break
		}
	}
	if selected == nil {
		return nil, nil, fmt.Errorf("position is not an independently editable source ref name")
	}
	definitions := 0
	for _, item := range items {
		if item.name != selected.name {
			continue
		}
		if !item.precise {
			return nil, nil, fmt.Errorf("a source reference has a shared or derived location")
		}
		if item.definition {
			definitions++
		}
	}
	if definitions != 1 {
		return nil, nil, fmt.Errorf("source ref definition is missing or ambiguous")
	}
	start, err := doc.Snapshot.Position(selected.span.Start)
	if err != nil {
		return nil, nil, err
	}
	end, err := doc.Snapshot.Position(selected.span.End)
	if err != nil {
		return nil, nil, err
	}
	return &SourceRenameTarget{Range{Position{start.Line, start.UTF16Column}, Position{end.Line, end.UTF16Column}}, selected.name}, items, nil
}

func PrepareSourceRefRename(ctx context.Context, doc Document, pos Position) (*SourceRenameTarget, error) {
	target, _, err := sourceRenameSelection(ctx, doc, pos)
	return target, err
}

func RenameSourceRef(ctx context.Context, doc Document, pos Position, newName string) (edit.Plan, error) {
	target, items, err := sourceRenameSelection(ctx, doc, pos)
	if err != nil {
		return edit.Plan{}, err
	}
	if !sourceRefName.MatchString(newName) {
		return edit.Plan{}, fmt.Errorf("source ref names contain only letters, digits, underscores and hyphens")
	}
	plan := edit.Plan{BaseRevision: doc.Snapshot.Revision()}
	if newName == target.Placeholder {
		return plan, nil
	}
	for _, item := range items {
		if item.name == newName {
			return edit.Plan{}, fmt.Errorf("new source ref name is already declared or referenced")
		}
		if item.name != target.Placeholder {
			continue
		}
		value := newName
		if !item.definition {
			value = "$" + newName + item.value[len(target.Placeholder)+1:]
		}
		encoded, err := json.Marshal(value)
		if err != nil {
			return edit.Plan{}, err
		}
		node := doc.location(item.path)
		plan.Replacements = append(plan.Replacements, edit.Replacement{Span: node.Span(), ExpectedText: node.Raw(), NewText: string(encoded)})
	}
	if !safeScopePlan(ctx, doc, []Document{doc}, DomainOptions{Complete: false}, plan, "") {
		return edit.Plan{}, fmt.Errorf("rename introduces a schema or local semantic error")
	}
	return plan, nil
}
