package analysis

import (
	"fmt"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// ProjectCandidate is a physical declaration to inspect, never a proved binding.
type ProjectCandidate struct {
	Kind      string   `json:"kind"`
	Name      string   `json:"name"`
	Namespace string   `json:"namespace"`
	Location  Location `json:"location"`
	*Refusal
}

// candidateScalar requires original source bytes. Detached index locations and
// shared/derived YAML values cannot supply an exact navigation selection.
func candidateScalar(doc Document, parts []string) (string, Range, bool) {
	if doc.compact != nil || !doc.exact(parts) || !exactReferencePath(doc.Root, parts) {
		return "", Range{}, false
	}
	node := doc.location(parts)
	value, ok := referenceLiteral(node)
	if !ok || strings.ContainsAny(value, "${}\r\n") {
		return "", Range{}, false
	}
	raw, span := node.Raw(), node.Span()
	if len(raw) >= 2 && ((raw[0] == '\'' && raw[len(raw)-1] == '\'') || (raw[0] == '"' && raw[len(raw)-1] == '"')) {
		raw, span.Start, span.End = raw[1:len(raw)-1], span.Start+1, span.End-1
	}
	if raw != value || span.Start < 0 || span.End > len(doc.Snapshot.Text()) || span.End < span.Start {
		return "", Range{}, false
	}
	a, firstErr := doc.Snapshot.Position(span.Start)
	b, lastErr := doc.Snapshot.Position(span.End)
	if firstErr != nil || lastErr != nil {
		return "", Range{}, false
	}
	return value, Range{Start: Position{a.Line, a.UTF16Column}, End: Position{b.Line, b.UTF16Column}}, true
}

func physicalProjectPaths(doc Document) [][]string {
	if doc.Kind == "Application" {
		return [][]string{{"spec", "project"}}
	}
	if doc.Kind != "ApplicationSet" {
		return nil
	}
	paths := [][]string{{"spec", "template", "spec", "project"}}
	for i, generator := range objectSlice(object(doc.Value["spec"])["generators"]) {
		kinds := []string{}
		for kind := range object(generator) {
			if kind != "selector" {
				kinds = append(kinds, kind)
			}
		}
		sort.Strings(kinds)
		for _, kind := range kinds {
			paths = append(paths, []string{"spec", "generators", fmt.Sprint(i), kind, "template", "spec", "project"})
		}
	}
	return paths
}

// ProjectCandidateSelection follows only the physical Application/ApplicationSet
// project sites already supported by Definition, without projecting inheritance.
func ProjectCandidateSelection(documents []Document, uri string, position Position) (string, Location, bool) {
	for _, doc := range documents {
		if string(doc.Snapshot.URI()) != uri {
			continue
		}
		for _, parts := range physicalProjectPaths(doc) {
			name, selection, exact := candidateScalar(doc, parts)
			if exact && len(name) <= 253 && projectResourceName.MatchString(name) && position.Line == selection.Start.Line && position.Character >= selection.Start.Character && position.Character <= selection.End.Character {
				return name, Location{URI: uri, Range: selection}, true
			}
		}
	}
	return "", Location{}, false
}

// ProjectCandidateDeclarations preserves separate declarations, including two
// documents in one file. The caller bounds admission and supplies fresh text.
func candidateNamespace(doc Document) (string, bool) {
	value, exists := object(doc.Value["metadata"])["namespace"]
	if !exists {
		return "", true
	}
	parts := []string{"metadata", "namespace"}
	if value == "" && doc.exact(parts) && exactReferencePath(doc.Root, parts) {
		raw := doc.location(parts).Raw()
		return "", raw == `""` || raw == "''"
	}
	namespace, _, exact := candidateScalar(doc, parts)
	return namespace, exact && len(namespace) <= 63 && !strings.Contains(namespace, ".") && projectResourceName.MatchString(namespace)
}

func ProjectCandidateDeclarations(documents []Document, name string, limit int) ([]ProjectCandidate, bool) {
	items := []ProjectCandidate{}
	complete := true
	for _, doc := range documents {
		if doc.Kind != "AppProject" || doc.Snapshot.URI() == source.URI("") {
			continue
		}
		if literal(object(doc.Value["metadata"])["name"]) != name {
			continue
		}
		declared, selection, exact := candidateScalar(doc, []string{"metadata", "name"})
		if !exact || declared != name {
			complete = false
			continue
		}
		namespace, valid := candidateNamespace(doc)
		if !valid {
			complete = false
			continue
		}
		items = append(items, ProjectCandidate{Kind: "AppProject", Name: name, Namespace: namespace,
			Location: Location{URI: string(doc.Snapshot.URI()), Range: selection},
			Refusal:  Refuse("TARGET_UNKNOWN", "The name matches, but an unknown control-plane namespace does not establish a binding.")})
		if len(items) >= limit {
			break
		}
	}
	return items, complete
}
