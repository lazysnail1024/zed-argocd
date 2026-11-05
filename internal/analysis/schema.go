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
	schemas         map[string]preparedSchema
	projectionNodes int
	projections     map[*valueOrigin]bool
}

// ProjectionNodes is the retained logical graph size in this analysis result.
func (a *Analyzer) ProjectionNodes() int { return a.projectionNodes }

// ReuseProjections charges immutable graph instances once, without retaining text.
// Admission is atomic so a refused scope can be analyzed normally instead.
func (a *Analyzer) ReuseProjections(documents []Document) bool {
	pending := map[*valueOrigin]int{}
	for _, doc := range documents {
		if doc.Origin != nil && !a.projections[doc.Origin] {
			pending[doc.Origin] = doc.ProjectionNodes
		}
	}
	total := a.projectionNodes
	for _, nodes := range pending {
		total += nodes
	}
	if total > maxProjectionNodes {
		return false
	}
	if a.projections == nil {
		a.projections = map[*valueOrigin]bool{}
	}
	for origin := range pending {
		a.projections[origin] = true
	}
	a.projectionNodes = total
	return true
}

// ReuseSchemas borrows only immutable preparation from completed documents.
// The caller owns the previous result lifetime; no documents are retained here.
func (a *Analyzer) ReuseSchemas(documents []Document) {
	if a.schemas == nil {
		a.schemas = map[string]preparedSchema{}
	}
	for _, doc := range documents {
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
		raw, err := documentText(s, doc.Span())
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
		projected, err := projectValuesWithin(ctx, raw, root, maxProjectionNodes-a.projectionNodes)
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
		document := Document{ProjectionNodes: projected.Nodes, Snapshot: s, Root: root, Value: object(value), Origin: projected.Origin, Kind: resourceKind, Target: target, SchemaData: prepared.data, CompiledSchema: prepared.compiled}
		a.ReuseProjections([]Document{document})
		if document.Value != nil {
			result.Documents = append(result.Documents, document)
			for _, unknown := range UnknownFields(document) {
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
			var collect func(*schema.ValidationError)
			collect = func(e *schema.ValidationError) {
				if len(e.Causes) > 0 {
					for _, child := range e.Causes {
						collect(child)
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
