// Package analysis performs partial local validation without filesystem or LSP I/O.
package analysis

import (
	"context"
	"strconv"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"github.com/santhosh-tekuri/jsonschema/v6/kind"
	"sigs.k8s.io/yaml"
)

type Position struct {
	Line      uint32 `json:"line"`
	Character uint32 `json:"character"`
}
type Range struct {
	Start Position `json:"start"`
	End   Position `json:"end"`
}
type Diagnostic struct {
	TargetID string `json:"targetId,omitempty"`
	URI      string `json:"uri"`
	Range    Range  `json:"range"`
	Code     string `json:"code"`
	Source   string `json:"source"`
	Severity int    `json:"severity"`
	Message  string `json:"message"`
}
type Document struct {
	// compact is a detached workspace summary. Only a freshly acquired full
	// document is used for syntax queries, Schema validation or edits.
	compact         *compactDocument
	appSetExpansion applicationSetCost
	OverrideOrigins map[string]yamlview.Node
	ProjectionNodes int
	Origin          *valueOrigin
	UnknownPaths    map[string]bool
	SchemaData      map[string]any
	CompiledSchema  *schema.Schema
	Snapshot        source.Snapshot
	Root            yamlview.Node
	Value           map[string]any
	Kind            string
	Target          string
}

type Result struct {
	Documents   []Document
	Diagnostics []Diagnostic
	Incomplete  bool
}

type preparedSchema struct {
	data     map[string]any
	compiled *schema.Schema
}

// Analyzer owns immutable schema preparation and graph accounting for one result.
type Analyzer struct {
	schemas           map[string]preparedSchema
	projectionNodes   int
	projections       map[*valueOrigin]bool
	appSetBytes       int
	appSetProjections map[applicationSetIdentity]applicationSetCost
}

// An expansion identity is independent of exact/derived source provenance.
// Ordinary YAML has no Origin graph but still owns ApplicationSet work.
type applicationSetIdentity struct {
	uri      source.URI
	revision source.Revision
	span     source.Span
}

// ProjectionNodes includes retained YAML graphs and admitted ApplicationSet work.
func (a *Analyzer) ProjectionNodes() int { return a.projectionNodes }

// BeginFile keeps reusable Schema preparation while starting an independent,
// bounded file job. The CLI's aggregate analyzer does not call this method.
func (a *Analyzer) BeginFile() {
	a.projectionNodes = 0
	a.projections = nil
	a.appSetBytes = 0
	a.appSetProjections = nil
}

// ReuseProjections charges immutable graphs and admitted expansions once without
// retaining text. Both node and string admission are atomic across all documents.
func (a *Analyzer) ReuseProjections(documents []Document) bool {
	pending := map[*valueOrigin]int{}
	expansions := map[applicationSetIdentity]applicationSetCost{}
	for _, doc := range documents {
		graphNodes := doc.ProjectionNodes - doc.appSetExpansion.nodes
		if graphNodes < 0 || doc.appSetExpansion.nodes < 0 || doc.appSetExpansion.bytes < 0 {
			return false
		}
		if doc.Origin != nil && !a.projections[doc.Origin] {
			pending[doc.Origin] = graphNodes
		}
		if doc.appSetExpansion.nodes != 0 || doc.appSetExpansion.bytes != 0 {
			identity := applicationSetIdentity{doc.Snapshot.URI(), doc.Snapshot.Revision(), doc.Root.Span()}
			if _, charged := a.appSetProjections[identity]; !charged {
				expansions[identity] = doc.appSetExpansion
			}
		}
	}
	total := a.projectionNodes
	bytes := a.appSetBytes
	for _, nodes := range pending {
		if nodes > maxProjectionNodes-total {
			return false
		}
		total += nodes
	}
	for _, cost := range expansions {
		if cost.nodes > maxProjectionNodes-total || cost.bytes > maxApplicationSetExpansionBytes-bytes {
			return false
		}
		total += cost.nodes
		bytes += cost.bytes
	}
	if a.projections == nil {
		a.projections = map[*valueOrigin]bool{}
	}
	for origin := range pending {
		a.projections[origin] = true
	}
	if a.appSetProjections == nil {
		a.appSetProjections = map[applicationSetIdentity]applicationSetCost{}
	}
	for identity, cost := range expansions {
		a.appSetProjections[identity] = cost
	}
	a.projectionNodes = total
	a.appSetBytes = bytes
	return true
}

// ReuseSchemas borrows only immutable preparation from completed documents.
// The caller owns the previous result lifetime; no documents are retained here.
func (a *Analyzer) ReuseSchemas(documents []Document) {
	if a.schemas == nil {
		a.schemas = map[string]preparedSchema{}
	}
	for _, doc := range documents {
		if doc.compact != nil {
			continue
		}
		a.schemas[doc.Target+"/"+doc.Kind] = preparedSchema{doc.SchemaData, doc.CompiledSchema}
	}
}

func (a *Analyzer) Analyze(ctx context.Context, s source.Snapshot, target string) (Result, error) {
	result := Result{Diagnostics: []Diagnostic{}}
	view, err := yamlview.Parse(ctx, s)
	if err != nil {
		return result, err
	}
	result.Incomplete = len(view.Issues()) > 0
	emit := func(node yamlview.Node, code, message string) {
		a, e1 := s.Position(node.Span().Start)
		b, e2 := s.Position(node.Span().End)
		if e1 != nil || e2 != nil {
			return
		}
		result.Diagnostics = append(result.Diagnostics, Diagnostic{URI: string(s.URI()), Range: Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}, Code: code, Source: "argocd-ls", Severity: 1, Message: message})
	}
	for _, doc := range view.Documents() {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		root := doc.Root()
		raw, err := s.Slice(doc.Span())
		if err != nil {
			return result, err
		}
		api, resourceKind := "", ""
		// CST scalar views omit tag wrappers. Potential tag syntax must be
		// interpreted by projection, rather than used as an early identity hint.
		if !strings.Contains(raw, "!") {
			api = fieldString(root, "apiVersion")
			resourceKind = fieldString(root, "kind")
		}
		hintOwned := api == "argoproj.io/v1alpha1" && (resourceKind == "Application" || resourceKind == "AppProject" || resourceKind == "ApplicationSet")
		if (api != "" && api != "argoproj.io/v1alpha1") || (resourceKind != "" && resourceKind != "Application" && resourceKind != "AppProject" && resourceKind != "ApplicationSet") {
			continue
		}
		if !doc.Complete() {
			result.Incomplete = true
			continue
		}
		projected, err := projectValuesWithin(ctx, s, doc, maxProjectionNodes-a.projectionNodes)
		if err != nil {
			if ctx.Err() != nil {
				return result, ctx.Err()
			}
			if hintOwned || projected.Owned || !projected.IdentityKnown {
				result.Incomplete = true
			}
			if hintOwned || projected.Owned {
				failure := err.(*projectionError)
				emit(root, failure.Code, failure.Message)
			}
			continue
		}
		value := projected.Value
		api = literal(object(value)["apiVersion"])
		resourceKind = literal(object(value)["kind"])
		if api != "argoproj.io/v1alpha1" || (resourceKind != "Application" && resourceKind != "AppProject" && resourceKind != "ApplicationSet") {
			continue
		}
		if err := ctx.Err(); err != nil {
			return result, err
		}
		expansion := applicationSetCost{}
		if resourceKind == "ApplicationSet" {
			// A reused immutable identity has already reserved its expansion;
			// do not charge it twice when this source is analyzed again.
			identity := applicationSetIdentity{s.URI(), s.Revision(), root.Span()}
			previous := a.appSetProjections[identity]
			expansion, err = applicationSetExpansion(ctx, object(value), maxProjectionNodes-a.projectionNodes-projected.Nodes+previous.nodes, maxApplicationSetExpansionBytes-a.appSetBytes+previous.bytes)
			if err != nil {
				if ctx.Err() != nil {
					return result, ctx.Err()
				}
				result.Incomplete = true
				failure := err.(*projectionError)
				emit(root, failure.Code, failure.Message)
				continue
			}
		}
		key := target + "/" + resourceKind
		prepared, ok := a.schemas[key]
		if !ok {
			data, e := assets.SchemaData(target, api, resourceKind)
			if e != nil {
				return result, e
			}
			compiled, e := assets.CompileSchema(data)
			if e != nil {
				return result, e
			}
			prepared = preparedSchema{data, compiled}
			if a.schemas == nil {
				a.schemas = map[string]preparedSchema{}
			}
			a.schemas[key] = prepared
		}
		unknownPaths := map[string]bool{}
		document := Document{ProjectionNodes: projected.Nodes + expansion.nodes, appSetExpansion: expansion, Snapshot: s, Root: root, Value: object(value), Origin: projected.Origin, Kind: resourceKind, Target: target, SchemaData: prepared.data, CompiledSchema: prepared.compiled}
		a.ReuseProjections([]Document{document})
		if document.Value != nil {
			result.Documents = append(result.Documents, document)
			unknownFields, queryErr := UnknownFields(ctx, document)
			if queryErr != nil {
				result.Incomplete = true
				return result, queryErr
			}
			for _, unknown := range unknownFields {
				unknownPaths[strings.Join(unknown.Path, "/")] = true
				d := diagnostic(document, unknown.Path, "SCHEMA_UNKNOWN_FIELD", 2, "Field is not declared by this target Schema")
				d.Range = nodeRange(document, unknown.Key)
				result.Diagnostics = append(result.Diagnostics, d)
			}
		}
		compiled := prepared.compiled
		if err = compiled.Validate(value); err != nil {
			validation, ok := err.(*schema.ValidationError)
			if !ok {
				return result, err
			}
			if target == "auto" {
				validation = closestVersionError(validation, unknownPaths)
			}
			var collect func(*schema.ValidationError)
			collect = func(e *schema.ValidationError) {
				if len(e.Causes) > 0 {
					for _, child := range e.Causes {
						collect(child)
					}
					return
				}
				if extra, ok := e.ErrorKind.(*kind.AdditionalProperties); ok && target == "auto" {
					for _, name := range extra.Properties {
						parts := appendPath(e.InstanceLocation, name)
						if !unknownPaths[strings.Join(parts, "/")] {
							emit(document.location(parts), "SCHEMA_VERSION_COMBINATION", "This field combination is not declared by any supported Argo CD release")
						}
					}
					return
				}
				if required, ok := e.ErrorKind.(*kind.Required); ok {
					for _, field := range required.Missing {
						emit(document.location(e.InstanceLocation), "SCHEMA_REQUIRED", document.locationMessage(e.InstanceLocation, "Missing required field "+strings.Join(appendPath(e.InstanceLocation, field), ".")))
					}
					return
				}
				code := "SCHEMA_CONSTRAINT"
				switch e.ErrorKind.(type) {
				case *kind.Type:
					code = "SCHEMA_TYPE"
				}
				node := document.location(e.InstanceLocation)
				emit(node, code, document.locationMessage(e.InstanceLocation, e.Error()))
			}
			collect(validation)
		}
		if err := ctx.Err(); err != nil {
			return result, err
		}
	}
	return result, nil
}

// Root compatibility failure contains one cause per whole-release variant.
// Report the closest variant's actionable errors rather than hundreds of copies.
// Unknown names already have their own warning and do not influence selection.
func closestVersionError(e *schema.ValidationError, unknown map[string]bool) *schema.ValidationError {
	if _, ok := e.ErrorKind.(*kind.AnyOf); ok && len(e.InstanceLocation) == 0 && len(e.Causes) > 0 {
		best := e.Causes[0]
		score := versionErrorScore(best, unknown)
		for _, cause := range e.Causes[1:] {
			if current := versionErrorScore(cause, unknown); current < score {
				best, score = cause, current
			}
		}
		return best
	}
	for i, child := range e.Causes {
		e.Causes[i] = closestVersionError(child, unknown)
	}
	return e
}

func versionErrorScore(e *schema.ValidationError, unknown map[string]bool) int {
	if len(e.Causes) > 0 {
		total := 0
		for _, child := range e.Causes {
			total += versionErrorScore(child, unknown)
		}
		return total
	}
	if extra, ok := e.ErrorKind.(*kind.AdditionalProperties); ok {
		total := 0
		for _, name := range extra.Properties {
			if !unknown[strings.Join(appendPath(e.InstanceLocation, name), "/")] {
				total += 100
			}
		}
		return total
	}
	if required, ok := e.ErrorKind.(*kind.Required); ok {
		return len(required.Missing)
	}
	return 1
}

// Early ownership hints require unique literal keys. An unresolved key might
// name the same field, so only the full projection can establish its identity.
func fieldString(n yamlview.Node, key string) string {
	found := false
	value := ""
	for _, m := range n.Members() {
		if m.Key.Kind() != yamlview.Scalar {
			return ""
		}
		var k string
		if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &k) != nil {
			return ""
		}
		if k == key {
			if found {
				return ""
			}
			found = true
			if yaml.UnmarshalStrict([]byte(m.Value.Raw()), &value) != nil {
				value = ""
			}
		}
	}
	return value
}

func locate(n yamlview.Node, parts []string) yamlview.Node {
	for _, part := range parts {
		found := false
		if n.Kind() == yamlview.Mapping {
			for _, m := range n.Members() {
				var k string
				if yaml.UnmarshalStrict([]byte(m.Key.Raw()), &k) == nil && k == part {
					n = m.Value
					if n.Kind() == yamlview.Empty {
						n = m.Key
					}
					found = true
					break
				}
			}
		} else if n.Kind() == yamlview.Sequence {
			i, err := strconv.Atoi(part)
			elements := n.Elements()
			if err == nil && i >= 0 && i < len(elements) {
				n = elements[i]
				found = true
			}
		}
		if !found {
			return n
		}
	}
	return n
}

func oversizedScalar(n yamlview.Node) bool {
	if n.Kind() == yamlview.Scalar && len(n.Raw()) > 1024*1024 {
		return true
	}
	for _, m := range n.Members() {
		if oversizedScalar(m.Key) || oversizedScalar(m.Value) {
			return true
		}
	}
	for _, child := range n.Elements() {
		if oversizedScalar(child) {
			return true
		}
	}
	return false
}
