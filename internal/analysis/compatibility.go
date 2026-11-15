package analysis

import (
	"context"
	"fmt"
	"io"
	"strings"
	"sync"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
)

const CompatibilityLabel = "candidates under modeled static constraints"
const maxCompatibilityResources = 32

type CompatibilityInput struct {
	Source        string `json:"source"`
	URI           string `json:"uri"`
	Version       *int   `json:"version,omitempty"`
	Revision      uint64 `json:"revision,omitempty"`
	SHA256        string `json:"sha256"`
	ConfigHash    string `json:"configHash"`
	ConfigSource  string `json:"configSource"`
	TargetVersion string `json:"targetVersion"`
	TargetSource  string `json:"targetSource"`
}

type CompatibilityUnknown struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Range   *Range `json:"range,omitempty"`
}

type CompatibilityExclusion struct {
	Versions             []string     `json:"versions"`
	Diagnostics          []Diagnostic `json:"diagnostics"`
	DiagnosticsTruncated bool         `json:"diagnosticsTruncated,omitempty"`
}

type CompatibilityResource struct {
	Kind                string                   `json:"kind"`
	Name                string                   `json:"name"`
	Range               Range                    `json:"range"`
	SchemaCandidates    []string                 `json:"schemaCandidates"`
	SemanticCandidates  []string                 `json:"semanticCandidates"`
	Candidates          []string                 `json:"candidates"`
	SchemaExclusions    []CompatibilityExclusion `json:"schemaExclusions"`
	SemanticExclusions  []CompatibilityExclusion `json:"semanticExclusions"`
	CoveredRuleIDs      []string                 `json:"coveredRuleIds"`
	ModeledRuleCoverage string                   `json:"modeledRuleCoverage"`
	Unknowns            []CompatibilityUnknown   `json:"unknowns"`
	SelectedTarget      struct {
		Version          string `json:"version"`
		SchemaCandidate  *bool  `json:"schemaCandidate,omitempty"`
		ModeledCandidate *bool  `json:"modeledCandidate,omitempty"`
	} `json:"selectedTarget"`
}

type CompatibilityReport struct {
	SchemaVersion  int                `json:"schemaVersion"`
	Label          string             `json:"label"`
	Root           string             `json:"root"`
	Scope          string             `json:"scope"`
	ScopeSemantics string             `json:"scopeSemantics"`
	InputIdentity  CompatibilityInput `json:"inputIdentity"`
	Catalog        struct {
		SHA256                   string                  `json:"sha256"`
		ReleaseCount             int                     `json:"releaseCount"`
		CompatibilitySHA256      string                  `json:"compatibilitySHA256"`
		RuleEvidenceSHA256       string                  `json:"ruleEvidenceSHA256"`
		HistoricalEvidenceSHA256 string                  `json:"historicalEvidenceSHA256"`
		EvidenceCoverage         assets.EvidenceCoverage `json:"evidenceCoverage"`
	} `json:"catalog"`
	Complete     bool                    `json:"complete"`
	Resources    []CompatibilityResource `json:"resources"`
	Unknowns     []CompatibilityUnknown  `json:"unknowns"`
	NotEvaluated []string                `json:"notEvaluated"`
}

// CompatibilityAnalyzer retains only the finite set of immutable auto and
// representative exact schemas, never documents, snapshots or version verdicts.
// It is safe to share between explicit requests and does not participate in the
// background analysis/publication state machine.
type CompatibilityAnalyzer struct {
	mu      sync.Mutex
	schemas map[string]preparedSchema
}

// Analyze is used only when no accepted full projection can be reused. Exact
// target selection is retained in the report, while the auto schema permits
// explaining resources that did not exist at the selected exact release.
func (a *CompatibilityAnalyzer) Analyze(ctx context.Context, snapshot source.Snapshot) (Result, error) {
	analyzer := new(Analyzer)
	a.mu.Lock()
	analyzer.schemas = make(map[string]preparedSchema, len(a.schemas))
	for key, prepared := range a.schemas {
		analyzer.schemas[key] = prepared
	}
	a.mu.Unlock()
	result, err := analyzer.Analyze(ctx, snapshot, "auto")
	if err != nil {
		return result, err
	}
	for _, doc := range result.Documents {
		if _, err := a.prepared(ctx, "auto", doc.Kind, result.Documents); err != nil {
			return result, err
		}
	}
	return result, nil
}

func (a *CompatibilityAnalyzer) prepared(ctx context.Context, version, kind string, documents []Document) (preparedSchema, error) {
	if err := ctx.Err(); err != nil {
		return preparedSchema{}, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	key := version + "/" + kind
	if cached, ok := a.schemas[key]; ok {
		return cached, nil
	}
	var prepared preparedSchema
	for _, doc := range documents {
		if doc.compact == nil && doc.Target == version && doc.Kind == kind && doc.CompiledSchema != nil {
			prepared = preparedSchema{doc.SchemaData, doc.CompiledSchema}
			break
		}
	}
	if prepared.compiled == nil {
		data, err := assets.SchemaData(version, "argoproj.io/v1alpha1", kind)
		if err != nil {
			return prepared, err
		}
		compiled, err := assets.CompileSchema(data)
		if err != nil {
			return prepared, err
		}
		prepared = preparedSchema{data, compiled}
	}
	if a.schemas == nil {
		a.schemas = map[string]preparedSchema{}
	}
	a.schemas[key] = prepared
	return prepared, ctx.Err()
}

// Explain consumes the same immutable projected resources as normal analysis.
// Whole schemas and currently modeled local rules are separate predicates;
// cross-resource policy, controller effects and dynamic values stay unproved.
func (a *CompatibilityAnalyzer) Explain(ctx context.Context, input Result, target string, trace io.Writer) (CompatibilityReport, error) {
	report := CompatibilityReport{SchemaVersion: 1, Label: CompatibilityLabel, ScopeSemantics: "independent-resources", Complete: !input.Incomplete,
		Resources: []CompatibilityResource{}, Unknowns: []CompatibilityUnknown{},
		NotEvaluated: []string{"controller-admission-and-runtime", "cross-resource-project-policy", "local-repository-path-existence", "source-hydration", "helm-value-file-expansion", "template-function-availability", "generator-expansion", "dynamic-templatePatch-effects", "unmodeled-release-specific-rules", "CEL"}}
	metadata, err := assets.CompatibilityIndex()
	if err != nil {
		return report, err
	}
	report.Catalog.SHA256, report.Catalog.ReleaseCount = metadata.CatalogHash, len(metadata.Versions)
	report.Catalog.CompatibilitySHA256 = assets.CompatibilityHash()
	report.Catalog.RuleEvidenceSHA256, report.Catalog.HistoricalEvidenceSHA256 = metadata.RuleEvidenceHash, metadata.HistoricalEvidenceHash
	report.Catalog.EvidenceCoverage = metadata.EvidenceCoverage
	if input.Incomplete {
		report.Unknowns = append(report.Unknowns, CompatibilityUnknown{Code: "INPUT_INCOMPLETE", Message: "Damaged or bounded input prevented explanation of every resource; any returned resources are independent partial results"})
	}
	for _, doc := range input.Documents {
		if err := ctx.Err(); err != nil {
			return report, err
		}
		if len(report.Resources) == maxCompatibilityResources {
			report.Complete = false
			report.Unknowns = append(report.Unknowns, CompatibilityUnknown{Code: "RESOURCE_LIMIT", Message: "Explanation is limited to 32 resources per request; select a smaller YAML input to explain the remaining resources"})
			break
		}
		resource, err := a.resource(ctx, doc, input.Documents, target, metadata, trace)
		if err != nil {
			return report, err
		}
		report.Resources = append(report.Resources, resource)
	}
	if len(input.Documents) == 0 && !input.Incomplete {
		report.Unknowns = append(report.Unknowns, CompatibilityUnknown{Code: "NO_ARGOCD_RESOURCE", Message: "No complete Application, ApplicationSet or AppProject resource was found in this input"})
	}
	return report, nil
}

func (a *CompatibilityAnalyzer) resource(ctx context.Context, doc Document, documents []Document, target string, metadata *assets.CompatibilityMetadata, trace io.Writer) (CompatibilityResource, error) {
	r := CompatibilityResource{Kind: doc.Kind, Name: literal(object(doc.Value["metadata"])["name"]), Range: nodeRange(doc, doc.Root),
		SchemaCandidates: []string{}, SemanticCandidates: []string{}, Candidates: []string{}, SchemaExclusions: []CompatibilityExclusion{}, SemanticExclusions: []CompatibilityExclusion{},
		CoveredRuleIDs: []string{"whole-resource-normalized-schema"}, ModeledRuleCoverage: "schema-only", Unknowns: []CompatibilityUnknown{}}
	if doc.compact != nil {
		return r, fmt.Errorf("compatibility explanation requires an acquired full document")
	}
	index, ok := metadata.Kinds[doc.Kind]
	if !ok {
		return r, fmt.Errorf("compatibility kind is absent from metadata")
	}
	auto, err := a.prepared(ctx, "auto", doc.Kind, documents)
	if err != nil {
		return r, err
	}
	branches, _ := auto.data["anyOf"].([]any)
	if len(branches) != len(index.Branches) || len(auto.compiled.AnyOf) != len(index.Branches) {
		return r, fmt.Errorf("whole-schema branch count differs from compatibility metadata")
	}
	schemaSet, semanticSet := versionSet(len(metadata.Versions)), versionSet(len(metadata.Versions))
	schemaWork, semanticWork := 0, 0
	for i, branch := range index.Branches {
		if err := ctx.Err(); err != nil {
			return r, err
		}
		if object(branches[i])["$ref"] != branch.Ref {
			return r, fmt.Errorf("whole-schema branch identity differs from compatibility metadata")
		}
		schemaWork++
		if err := auto.compiled.AnyOf[i].Validate(doc.Value); err == nil {
			schemaSet.add(branch.Versions)
		} else {
			validation, ok := err.(*schema.ValidationError)
			if !ok {
				return r, err
			}
			diagnostics, truncated := compatibilitySchemaReasons(doc, validation)
			r.SchemaExclusions = append(r.SchemaExclusions, CompatibilityExclusion{releaseNames(metadata.Versions, branch.Versions), diagnostics, truncated})
		}
	}
	if doc.Kind != "AppProject" {
		r.ModeledRuleCoverage = "current-document-local-static-rules"
		r.CoveredRuleIDs = append(r.CoveredRuleIDs, "current-document-local-static-rules", "single-source-refs")
	} else {
		r.Unknowns = append(r.Unknowns, CompatibilityUnknown{Code: "NO_DOCUMENT_LOCAL_RULES", Message: "AppProject policy requires referencing resources; this explanation applies its whole schema without claiming controller or cross-resource policy validation"})
	}
	if doc.Kind == "ApplicationSet" {
		r.CoveredRuleIDs = append(r.CoveredRuleIDs, "static-template-patch-required-fields")
	}
	unknownSeen := map[Diagnostic]bool{}
	for _, profile := range index.Profiles {
		if err := ctx.Err(); err != nil {
			return r, err
		}
		prepared, err := a.prepared(ctx, profile.Representative, doc.Kind, documents)
		if err != nil {
			return r, err
		}
		candidate := doc
		candidate.Target, candidate.SchemaData, candidate.CompiledSchema = profile.Representative, prepared.data, prepared.compiled
		semanticWork++
		// Complete:false withholds all checks requiring a workspace or controller
		// scope. Domain is the existing rule implementation, not a second checker.
		diagnostics := Domain([]Document{candidate}, DomainOptions{Context: ctx, Complete: false})
		errors := []Diagnostic{}
		for _, diagnostic := range diagnostics {
			if diagnostic.Severity == 1 {
				errors = append(errors, diagnostic)
			}
			if diagnostic.Code == "CD_APPSET_DYNAMIC_FIELD" && !unknownSeen[diagnostic] {
				unknownSeen[diagnostic] = true
				if len(r.Unknowns) < 32 {
					position := diagnostic.Range
					r.Unknowns = append(r.Unknowns, CompatibilityUnknown{Code: diagnostic.Code, Message: diagnostic.Message, Range: &position})
				}
			}
		}
		if len(errors) == 0 {
			semanticSet.add(profile.Versions)
		} else {
			truncated := len(errors) > 16
			if truncated {
				errors = errors[:16]
			}
			r.SemanticExclusions = append(r.SemanticExclusions, CompatibilityExclusion{releaseNames(metadata.Versions, profile.Versions), errors, truncated})
		}
	}
	if len(unknownSeen) > 32 {
		r.Unknowns = append(r.Unknowns, CompatibilityUnknown{Code: "UNKNOWN_DETAILS_TRUNCATED", Message: "Only the first 32 dynamic locations are listed; additional dynamic values remain unproved"})
	}
	r.SchemaCandidates, r.SemanticCandidates = schemaSet.names(metadata.Versions), semanticSet.names(metadata.Versions)
	r.Candidates = schemaSet.intersection(semanticSet).names(metadata.Versions)
	r.SelectedTarget.Version = target
	if target != "auto" {
		schemaCandidate, modeledCandidate := false, false
		for _, v := range r.SchemaCandidates {
			schemaCandidate = schemaCandidate || v == target
		}
		for _, v := range r.Candidates {
			modeledCandidate = modeledCandidate || v == target
		}
		r.SelectedTarget.SchemaCandidate, r.SelectedTarget.ModeledCandidate = &schemaCandidate, &modeledCandidate
	}
	if trace != nil {
		fmt.Fprintf(trace, "argocd-compatibility kind=%s schemaBranches=%d semanticProfiles=%d\n", doc.Kind, schemaWork, semanticWork)
	}
	return r, ctx.Err()
}

func compatibilitySchemaReasons(doc Document, validation *schema.ValidationError) ([]Diagnostic, bool) {
	result := []Diagnostic{}
	truncated := false
	var walk func(*schema.ValidationError)
	walk = func(problem *schema.ValidationError) {
		if len(problem.Causes) > 0 {
			for _, cause := range problem.Causes {
				walk(cause)
			}
			return
		}
		if len(result) == 8 {
			truncated = true
			return
		}
		message := problem.Error()
		if len(message) > 1024 {
			message = message[:1024] + "…"
			truncated = true
		}
		result = append(result, diagnostic(doc, problem.InstanceLocation, "SCHEMA_CONSTRAINT", 1, message))
	}
	walk(validation)
	return result, truncated
}

type releaseSet []uint64

func versionSet(size int) releaseSet { return make(releaseSet, (size+63)/64) }
func (set releaseSet) add(indices []int) {
	for _, i := range indices {
		set[i/64] |= uint64(1) << (i % 64)
	}
}
func (set releaseSet) intersection(other releaseSet) releaseSet {
	result := make(releaseSet, len(set))
	for i := range set {
		result[i] = set[i] & other[i]
	}
	return result
}
func (set releaseSet) names(versions []string) []string {
	result := []string{}
	for i, version := range versions {
		if set[i/64]&(uint64(1)<<(i%64)) != 0 {
			result = append(result, version)
		}
	}
	return result
}
func releaseNames(versions []string, indices []int) []string {
	result := make([]string, 0, len(indices))
	for _, i := range indices {
		result = append(result, versions[i])
	}
	return result
}

func (r CompatibilityReport) Message() string {
	parts := []string{"Argo CD: " + r.Label}
	for i, resource := range r.Resources {
		if i == 5 {
			parts = append(parts, "Additional resources are available in the structured explanation.")
			break
		}
		parts = append(parts, fmt.Sprintf("%s %s: %d schema candidates; %d after modeled local rules. Candidates: %s. Unknown areas: %d.", resource.Kind, resource.Name, len(resource.SchemaCandidates), len(resource.Candidates), compatibilityVersionSummary(resource.Candidates), len(resource.Unknowns)))
		if resource.SelectedTarget.ModeledCandidate != nil {
			parts = append(parts, fmt.Sprintf("Selected target %s is a modeled candidate: %t.", resource.SelectedTarget.Version, *resource.SelectedTarget.ModeledCandidate))
		}
		if len(resource.SemanticExclusions) > 0 && len(resource.SemanticExclusions[0].Diagnostics) > 0 {
			d := resource.SemanticExclusions[0].Diagnostics[0]
			parts = append(parts, "Excluded at "+compatibilityVersionSummary(resource.SemanticExclusions[0].Versions)+": "+d.Code+": "+d.Message)
		} else if len(resource.Candidates) == 0 && len(resource.SchemaExclusions) > 0 && len(resource.SchemaExclusions[0].Diagnostics) > 0 {
			parts = append(parts, resource.SchemaExclusions[0].Diagnostics[0].Message)
		}
	}
	if !r.Complete {
		parts = append(parts, "Input explanation is incomplete; inspect the reported unknowns.")
	}
	parts = append(parts, fmt.Sprintf("Controller/admission behavior, cross-resource policy and dynamic effects remain unproved. Source evidence covers %d historical releases plus sampled boundaries, not all catalog semantics. Pin an intended target for its ordinary validation results.", r.Catalog.EvidenceCoverage.HistoricalReleaseCount))
	return strings.Join(parts, "\n")
}

func compatibilityVersionSummary(versions []string) string {
	if len(versions) == 0 {
		return "none"
	}
	if len(versions) <= 6 {
		return strings.Join(versions, ", ")
	}
	return strings.Join(versions[:3], ", ") + ", …, " + versions[len(versions)-1] + fmt.Sprintf(" (%d collected releases)", len(versions))
}
