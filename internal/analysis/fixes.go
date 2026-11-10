package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"sort"
	"strings"
)

// FixCandidatesInRange defers Project replacement validation until selection.
func FixCandidatesInRange(ctx context.Context, doc Document, scope []Document, options DomainOptions, span source.Span) []Fix {
	return fixesInRange(ctx, doc, scope, options, span, nil)
}

func ResolveFix(ctx context.Context, doc Document, scope []Document, options DomainOptions, span source.Span, code, text string) []Fix {
	return fixesInRange(ctx, doc, scope, options, span, &Fix{Code: code, Plan: edit.Plan{Replacements: []edit.Replacement{{NewText: text}}}})
}

func fixesInRange(ctx context.Context, doc Document, scope []Document, options DomainOptions, span source.Span, selected *Fix) []Fix {
	options.Context = ctx
	out := []Fix{}
	if ctx.Err() != nil || doc.Root.Span().Start > span.End || doc.Root.Span().End < span.Start {
		return out
	}
	// Correcting a field spelling or enum is a document edit. Its safety proof
	// covers schema and local domain errors in that input, even while unrelated
	// files are still being discovered. Selecting a different Project requires
	// the complete candidate set and retains the cross-resource proof below.
	localScope := []Document{}
	for _, candidate := range scope {
		if candidate.Snapshot.URI() == doc.Snapshot.URI() {
			localScope = append(localScope, candidate)
		}
	}
	localOptions := options
	localOptions.Complete = false
	add := func(code, title string, path []string, value any) {
		if !doc.exact(path) {
			return
		}
		node := doc.location(path)
		if ctx.Err() != nil || node.Span().Start > span.End || node.Span().End < span.Start {
			return
		}
		if strings.ContainsAny(node.Raw(), "\r\n") || strings.Contains(node.Raw(), "{{") {
			return
		}
		b, e := json.Marshal(value)
		if e != nil {
			return
		}
		plan := edit.Plan{BaseRevision: doc.Snapshot.Revision(), Replacements: []edit.Replacement{{Span: node.Span(), ExpectedText: node.Raw(), NewText: string(b)}}}
		if selected != nil && (selected.Code != code || selected.Plan.Replacements[0].NewText != string(b)) {
			return
		}
		checkScope, checkOptions := localScope, localOptions
		if code == "FIX_REFERENCE_CANDIDATE" {
			checkScope, checkOptions = scope, options
		}
		if (selected == nil && code == "FIX_REFERENCE_CANDIDATE") || safeScopePlan(ctx, doc, checkScope, checkOptions, plan, map[string]string{"FIX_REFERENCE_CANDIDATE": "CD_PROJECT_UNRESOLVED", "FIX_ENUM_VALUE": "SCHEMA_CONSTRAINT"}[code]) {
			out = append(out, Fix{code, title, plan})
		}
	}
	for _, fix := range fieldCandidates(doc) {
		r := fix.Plan.Replacements[0].Span
		if ctx.Err() != nil || r.Start > span.End || r.End < span.Start {
			continue
		}
		if selected != nil && (selected.Code != fix.Code || selected.Plan.Replacements[0].NewText != fix.Plan.Replacements[0].NewText) {
			continue
		}
		if safeScopePlan(ctx, doc, localScope, localOptions, fix.Plan, "SCHEMA_UNKNOWN_FIELD") {
			out = append(out, fix)
		}
	}
	if options.Complete && doc.Kind == "Application" && options.ControlPlaneNamespace != "" {
		project := literal(object(doc.Value["spec"])["project"])
		unresolved := true
		for _, candidate := range scope {
			meta := object(candidate.Value["metadata"])
			if candidate.Kind == "AppProject" && literal(meta["namespace"]) == options.ControlPlaneNamespace && literal(meta["name"]) == project {
				unresolved = false
			}
		}
		if project != "" && unresolved {
			names := map[string]int{}
			for _, p := range scope {
				meta := object(p.Value["metadata"])
				if p.Kind == "AppProject" && literal(meta["namespace"]) == options.ControlPlaneNamespace {
					names[literal(meta["name"])]++
				}
			}
			sorted := []string{}
			for name, count := range names {
				if name != "" && count == 1 {
					sorted = append(sorted, name)
				}
			}
			sort.Strings(sorted)
			for _, name := range sorted {
				add("FIX_REFERENCE_CANDIDATE", "Use Project "+name, []string{"spec", "project"}, name)
			}
		}
	}
	schema := doc.SchemaData
	if schema != nil {
		var walk func(any, map[string]any, []string)
		walk = func(value any, schema map[string]any, path []string) {
			if enums := objectSlice(schema["enum"]); len(enums) > 0 {
				current, _ := json.Marshal(value)
				valid := false
				for _, candidate := range enums {
					b, _ := json.Marshal(candidate)
					if string(b) == string(current) {
						valid = true
					}
				}
				if !valid {
					for _, candidate := range enums {
						add("FIX_ENUM_VALUE", fmt.Sprintf("Use %v", candidate), path, candidate)
					}
				}
			}
			for key, x := range object(value) {
				if field, ok := object(schema["properties"])[key]; ok {
					walk(x, object(field), appendPath(path, key))
				}
			}
			for i, x := range objectSlice(value) {
				walk(x, object(schema["items"]), appendPath(path, fmt.Sprint(i)))
			}
		}
		walk(doc.Value, schema, nil)
	}
	return out
}
func safeScopePlan(ctx context.Context, doc Document, scope []Document, options DomainOptions, plan edit.Plan, targetCode string) bool {
	if ctx.Err() != nil {
		return false
	}
	documentsStarted, domainsStarted := 0, 0
	defer func() {
		if options.Trace != nil {
			payload, _ := json.Marshal(map[string]any{"scope": "selected-fix-invocations", "safePlans": 1, "documentAnalyses": documentsStarted, "scopeDomainAnalyses": domainsStarted})
			fmt.Fprintln(options.Trace, "argocd-fix", string(payload))
		}
	}()
	next, e := edit.Apply(doc.Snapshot, doc.Snapshot.Revision()+1, plan)
	if e != nil {
		return false
	}
	analyzer := new(Analyzer)
	analyzer.ReuseSchemas(scope)
	documentsStarted++
	before, e := analyzer.Analyze(ctx, doc.Snapshot, doc.Target)
	if e != nil {
		return false
	}
	afterAnalyzer := new(Analyzer)
	afterAnalyzer.ReuseSchemas(scope)
	documentsStarted++
	after, e := afterAnalyzer.Analyze(ctx, next, doc.Target)
	if e != nil || after.Incomplete {
		return false
	}
	updated := []Document{}
	for _, d := range scope {
		if d.Snapshot.URI() != doc.Snapshot.URI() {
			updated = append(updated, d)
		}
	}
	updated = append(updated, after.Documents...)
	domainsStarted++
	beforeDiagnostics := append(before.Diagnostics, Domain(scope, options)...)
	domainsStarted++
	afterDiagnostics := append(after.Diagnostics, Domain(updated, options)...)
	errors := map[string]int{}
	for _, d := range beforeDiagnostics {
		if d.Severity == 1 {
			errors[d.URI+"\n"+d.Code+"\n"+d.Message]++
		}
	}
	targetStart, _ := doc.Snapshot.Position(plan.Replacements[0].Span.Start)
	for _, d := range afterDiagnostics {
		if d.URI == string(doc.Snapshot.URI()) && d.Code == targetCode && d.Range.Start == (Position{Line: targetStart.Line, Character: targetStart.UTF16Column}) {
			return false
		}
		if d.Severity == 1 {
			key := d.URI + "\n" + d.Code + "\n" + d.Message
			errors[key]--
			if errors[key] < 0 {
				return false
			}
		}
	}
	return ctx.Err() == nil
}
