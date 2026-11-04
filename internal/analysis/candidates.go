package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"sigs.k8s.io/yaml"
	"sort"
	"strconv"
	"strings"
)

// ReferenceCandidates never adds recovery fragments to the declaration index.
func ReferenceCandidates(ctx context.Context, snapshot source.Snapshot, target string, documents []Document, options DomainOptions, p Position) []Completion {
	result := []Completion{}
	offset, err := snapshot.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})
	if err != nil {
		return result
	}
	selected := []Document{}
	for _, doc := range documents {
		if doc.Snapshot.URI() == snapshot.URI() && offset >= doc.Root.Span().Start && offset <= doc.Root.Span().End {
			selected = append(selected, doc)
		}
	}
	partial := false
	if len(selected) == 0 {
		original, e := yamlview.Parse(ctx, snapshot)
		if e != nil {
			return result
		}
		damaged := false
		for _, d := range original.Documents() {
			if offset >= d.Span().Start && offset <= d.Span().End && !d.Complete() {
				damaged = true
			}
		}
		if !damaged {
			return result
		}
		// Only a complete physical-line prefix is recovered. Damage before or on
		// the selected line remains a hard boundary; no tokens are invented.
		end := strings.IndexByte(snapshot.Text()[offset:], '\n')
		if end < 0 {
			end = len(snapshot.Text())
		} else {
			end += offset + 1
		}
		prefix, e := source.New(snapshot.URI(), snapshot.Revision(), snapshot.Text()[:end])
		if e != nil {
			return result
		}
		view, e := yamlview.Parse(ctx, prefix)
		if e != nil {
			return result
		}
		for _, d := range view.Documents() {
			root := d.Root()
			if !d.Complete() || offset < root.Span().Start || offset > root.Span().End || oversizedScalar(root) {
				continue
			}
			raw, e := documentText(prefix, d.Span())
			if e != nil {
				continue
			}
			projected, e := projectValues(ctx, raw, root)
			if e != nil {
				continue
			}
			value := object(projected.Value)
			if literal(value["apiVersion"]) != "argoproj.io/v1alpha1" {
				continue
			}
			kind := literal(value["kind"])
			if kind != "Application" && kind != "ApplicationSet" {
				continue
			}
			selected = append(selected, Document{Snapshot: snapshot, Root: root, Kind: kind, Target: target, Value: value, Origin: projected.Origin})
			partial = true
		}
	}
	for _, doc := range selected {
		if ctx.Err() != nil {
			return []Completion{}
		}
		if doc.Kind == "ApplicationSet" {
			doc.Origin = doc.originBelow([]string{"spec", "template"})
			doc.Value = object(object(doc.Value["spec"])["template"])
			doc.Root = locate(doc.Root, []string{"spec", "template"})
			doc.Kind = "Application"
		}
		if doc.Kind != "Application" {
			continue
		}
		parts, node, key, ok := at(doc.Root, offset, nil)
		empty := emptyProjectAt(doc.Root, snapshot, offset)
		if empty {
			parts = []string{"spec", "project"}
			key = false
			ok = true
		}
		if !ok || key || !doc.exact(parts) || !exactReferencePath(doc.Root, parts) || (!empty && (node.Kind() != yamlview.Scalar || node.Damaged() || strings.ContainsAny(node.Raw(), "\r\n") || strings.Contains(node.Raw(), "{{") || strings.Contains(node.Raw(), "}}"))) {
			continue
		}
		raw := node.Raw()
		start := node.Span().Start
		if empty {
			raw = ""
			start = offset
		}
		quoted := len(raw) >= 2 && (raw[0] == '"' || raw[0] == '\'')
		if quoted {
			start++
			raw = raw[1 : len(raw)-1]
		}
		// Escaped tokens do not have a direct byte-to-value correspondence.
		if strings.ContainsAny(raw, "\\") || strings.Contains(raw, "''") || offset < start || offset > start+len(raw) {
			continue
		}
		prefix := raw[:offset-start]
		add := func(name, detail string, span source.Span) {
			text := name
			if quoted {
				if node.Raw()[0] == '\'' {
					text = strings.ReplaceAll(name, "'", "''")
				} else {
					encoded, _ := json.Marshal(name)
					text = string(encoded[1 : len(encoded)-1])
				}
			} else if len(parts) > 1 && parts[1] == "sources" {
				if strings.ContainsAny(name, " \t\r\n\"'\\:{}[],#") {
					return
				}
			} else {
				var value any
				if yaml.UnmarshalStrict([]byte(name), &value) != nil || value != name {
					encoded, _ := json.Marshal(name)
					text = string(encoded)
				}
			}
			if empty {
				if span.Start > 0 && snapshot.Text()[span.Start-1] == ':' {
					text = " " + text
				}
				if span.End < len(snapshot.Text()) && snapshot.Text()[span.End] == '#' {
					text += " "
				}
			}
			if partial {
				detail += "; reliable prefix only"
			}
			a, e1 := snapshot.Position(span.Start)
			b, e2 := snapshot.Position(span.End)
			if e1 != nil || e2 != nil {
				return
			}
			result = append(result, Completion{Label: name, Kind: 18, Detail: detail, TextEdit: TextEdit{Range: Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}, NewText: text}})
		}
		if strings.Join(parts, "/") == "spec/project" {
			for _, candidate := range documents {
				if ctx.Err() != nil {
					return []Completion{}
				}
				if candidate.Kind != "AppProject" {
					continue
				}
				meta := object(candidate.Value["metadata"])
				name, ns := literal(meta["name"]), literal(meta["namespace"])
				if name == "" || strings.Contains(name, "{{") || !strings.HasPrefix(name, prefix) || (options.ControlPlaneNamespace != "" && ns != options.ControlPlaneNamespace) {
					continue
				}
				origin := candidate.origin([]string{"metadata", "name"})
				detail := fmt.Sprintf("AppProject %s/%s; %s; %s location", ns, name, candidate.Snapshot.URI(), origin.Kind)
				if origin.Kind == "exact" {
					location := nodeRange(candidate, origin.Node)
					detail = fmt.Sprintf("AppProject %s/%s; %s:%d", ns, name, candidate.Snapshot.URI(), location.Start.Line+1)
				}
				if options.ControlPlaneNamespace == "" {
					detail += "; candidate only: configure controlPlaneNamespace in .argocd-ls.yaml for navigation"
				}
				add(name, detail, source.Span{Start: start, End: start + len(raw)})
			}
		} else if sourceRefUsePath(parts) && strings.HasPrefix(prefix, "$") {
			slash := strings.IndexByte(raw, '/')
			if slash < 0 {
				slash = len(raw)
			}
			if offset > start+slash {
				continue
			}
			refPrefix := prefix[1:]
			sources := locate(doc.Root, []string{"spec", "sources"})
			if !sourceRefsActive(doc.Target, len(sources.Elements())) {
				continue
			}
			for i, item := range sources.Elements() {
				path := []string{"spec", "sources", strconv.Itoa(i), "ref"}
				if !doc.exact(path) || !exactReferencePath(doc.Root, path) {
					continue
				}
				ref := locate(item, []string{"ref"})
				name, ok := referenceLiteral(ref)
				if !ok || !strings.HasPrefix(name, refPrefix) {
					continue
				}
				pos, _ := snapshot.Position(ref.Span().Start)
				add(name, fmt.Sprintf("source ref; %s:%d:%d", snapshot.URI(), pos.Line+1, pos.UTF16Column+1), source.Span{Start: start + 1, End: start + slash})
			}
		}
	}
	sort.SliceStable(result, func(i, j int) bool {
		if result[i].Label == result[j].Label {
			return result[i].Detail < result[j].Detail
		}
		return result[i].Label < result[j].Label
	})
	return result
}

func referenceLiteral(node yamlview.Node) (string, bool) {
	if node.Kind() != yamlview.Scalar || node.Damaged() || strings.ContainsAny(node.Raw(), "\r\n") || strings.Contains(node.Raw(), "{{") || strings.Contains(node.Raw(), "}}") {
		return "", false
	}
	var value any
	if yaml.UnmarshalStrict([]byte(node.Raw()), &value) != nil {
		return "", false
	}
	name, ok := value.(string)
	if !ok || name == "" {
		return "", false
	}
	return name, true
}

// Each path member must have exactly one literal key; duplicate/merge keys can
// never be used to recover an apparently precise scalar from damaged input.
func exactReferencePath(root yamlview.Node, parts []string) bool {
	node := root
	for _, part := range parts {
		if node.Kind() == yamlview.Sequence {
			i, e := strconv.Atoi(part)
			xs := node.Elements()
			if e != nil || i < 0 || i >= len(xs) {
				return false
			}
			node = xs[i]
			continue
		}
		matches := 0
		var next yamlview.Node
		for _, m := range node.Members() {
			var key string
			if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &key) != nil {
				return false
			}
			if key == "<<" {
				return false
			}
			if key == part {
				matches++
				next = m.Value
			}
		}
		if matches != 1 {
			return false
		}
		node = next
	}
	return true
}

func emptyProjectAt(root yamlview.Node, snapshot source.Snapshot, offset int) bool {
	if !exactReferencePath(root, []string{"spec", "project"}) {
		return false
	}
	spec := locate(root, []string{"spec"})
	for _, m := range spec.Members() {
		var key string
		if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &key) != nil || key != "project" || m.Value.Kind() != yamlview.Empty {
			continue
		}
		start := m.Key.Span().End
		if offset < start || offset > len(snapshot.Text()) {
			return false
		}
		between := snapshot.Text()[start:offset]
		return !strings.ContainsAny(between, "\r\n") && strings.TrimSpace(between) == ":"
	}
	return false
}

// Both supported Helm path shapes share the same reference token semantics.
func sourceRefUsePath(parts []string) bool {
	return len(parts) >= 6 && parts[0] == "spec" && parts[1] == "sources" && parts[3] == "helm" && ((len(parts) == 6 && parts[4] == "valueFiles") || (len(parts) == 7 && parts[4] == "fileParameters" && parts[6] == "path"))
}
