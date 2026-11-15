package analysis

import "errors"

// Refusal is a product-local explanation of the guard that actually refused an
// operation. Codes are selected at the guard, never inferred from prose.
type Refusal struct {
	ReasonCode       string     `json:"reasonCode"`
	Message          string     `json:"message"`
	RelatedLocations []Location `json:"relatedLocations"`
	NextStep         string     `json:"nextStep"`
}

func (r *Refusal) Error() string { return r.Message }

func Refuse(code, message string, locations ...Location) *Refusal {
	steps := map[string]string{
		"INDEX_INCOMPLETE":           "Wait for indexing to finish; if it remains incomplete, correct unavailable or malformed files in this scope.",
		"AMBIGUOUS_BINDING":          "Resolve duplicate or ambiguous definitions in the selected scope, then request the operation again.",
		"TARGET_UNKNOWN":             "Review the intended control-plane namespace and configure it explicitly only when it is known.",
		"AFFECTED_FILE_NOT_OPEN":     "Open each listed affected file in the editor, then request the operation again.",
		"DYNAMIC_REFERENCE":          "Use a literal binding or inspect an explicitly rendered manifest; dynamic bindings cannot prove a safe rename.",
		"STALE_INPUT":                "Request the operation again using the current buffer and configuration.",
		"REFERENCE_SCOPE_OPEN":       "Review the scope's reference policy; only a scope intentionally declared closed can authorize a cross-resource rename.",
		"SOURCE_NOT_EDITABLE":        "Select a directly editable literal; shared, derived or unmapped source locations cannot be edited safely.",
		"BINDING_NOT_FOUND":          "Select a binding with a unique definition in the selected scope.",
		"NOT_RENAME_TARGET":          "Select an Application source ref or an AppProject binding supported by Rename.",
		"NOT_REFERENCE_TARGET":       "Select an Application source ref or an AppProject declaration or reference.",
		"REFERENCE_COVERAGE_UNKNOWN": "Inspect the reported shared, derived or unknown uses; the available static locations do not prove absence of other uses.",
		"CLIENT_CAPABILITY_MISSING":  "Use a client supporting versioned documentChanges for safe edits.",
		"INPUT_UNAVAILABLE":          "Correct the unavailable input or configuration, then request analysis again.",
		"OUTSIDE_WORKSPACE":          "Open the containing folder explicitly in the editor; the server does not scan arbitrary parent folders.",
		"ISOLATED_DOCUMENT":          "This file is excluded from the workspace index. Review the exclusion configuration if it should participate in this scope.",
		"INVALID_NAME":               "Choose a valid unused name and request Rename again.",
		"SEMANTIC_CHANGE":            "Resolve the reported semantic conflict before renaming.",
		"BUILTIN_BINDING":            "The default Project has implicit uses; choose a separately declared Project.",
	}
	next := steps[code]
	if next == "" {
		next = "Review the reported condition and request the operation again after correcting it."
	}
	if locations == nil {
		locations = []Location{}
	}
	return &Refusal{code, message, locations, next}
}

func ExplainRefusal(err error) *Refusal {
	if err == nil {
		return nil
	}
	var refusal *Refusal
	if errors.As(err, &refusal) {
		return refusal
	}
	return Refuse("INPUT_UNAVAILABLE", err.Error())
}
