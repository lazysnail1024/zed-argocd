// Package schemaquery provides bounded editor queries over the compiled schema
// graph already used for validation. It does not compile schemas, resolve versions,
// load resources, parse YAML, or own caches. Callers keep the graph and value
// immutable for the duration of a call.
package schemaquery

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strconv"
	"strings"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"
)

// Limits bound graph work in one public query, including branch selection and
// candidate checks. Zero fields use 100000 visits and depth 256; negative fields
// permit no work. A validator call counts as one visit: callers must also bound
// schema/value sizes, since the underlying validator has no cancellation API.
type Limits struct{ Work, Depth int }

var ErrBudget = errors.New("schemaquery: query budget exhausted")

// ErrUnsupported means the compiled path uses a conditional/dynamic shape that
// this static editor query does not interpret. It is not an empty candidate set.
var ErrUnsupported = errors.New("schemaquery: unsupported path-dependent schema")

// Result exposes compiled metadata at a value path. Complete is false on any
// error; partial nodes are never returned as a complete absence of candidates.
// Ambiguous means more than one branch is still possible, not invalid input.
type Result struct {
	Nodes     []*jsonschema.Schema
	Ambiguous bool
	Complete  bool
}

type walk struct {
	ctx              context.Context
	remaining, depth int
	err              error
}

func newWalk(ctx context.Context, limits Limits) *walk {
	if limits.Work == 0 {
		limits.Work = 100000
	}
	if limits.Depth == 0 {
		limits.Depth = 256
	}
	return &walk{ctx: ctx, remaining: limits.Work, depth: limits.Depth}
}
func (w *walk) visit(depth int) bool {
	if w.err != nil {
		return false
	}
	if err := w.ctx.Err(); err != nil {
		w.err = err
		return false
	}
	if w.remaining <= 0 || depth > w.depth {
		w.err = ErrBudget
		return false
	}
	w.remaining--
	return true
}
func (w *walk) inspect(s *jsonschema.Schema, depth int) bool {
	if !w.inspectShape(s, depth) {
		return false
	}
	if s.Not != nil {
		w.err = ErrUnsupported
		return false
	}
	return true
}

// Shape selection reads positive field declarations, not negative assertions
// about valid combinations of those fields. Queries on the active path remain
// strict through inspect; the compiled validator still enforces every assertion.
func (w *walk) inspectShape(s *jsonschema.Schema, depth int) bool {
	if !w.visit(depth) {
		return false
	}
	if s.DynamicRef != nil || s.RecursiveRef != nil || s.If != nil || s.Then != nil || s.Else != nil ||
		len(s.PatternProperties) > 0 || len(s.Dependencies) > 0 || len(s.DependentSchemas) > 0 ||
		s.PropertyNames != nil || s.UnevaluatedProperties != nil || s.UnevaluatedItems != nil {
		w.err = ErrUnsupported
		return false
	}
	return true
}
func (w *walk) finish() error {
	if w.err == nil {
		w.err = w.ctx.Err()
	}
	return w.err
}
func (w *walk) validates(s *jsonschema.Schema, value any) bool {
	if !w.visit(0) {
		return false
	}
	valid := s.Validate(value) == nil
	return w.finish() == nil && valid
}

// Lookup follows properties/items, static references and allOf/anyOf/oneOf.
// Only explicit finite sibling constraints choose alternatives. The active path
// is excluded, so its unfinished or invalid value cannot hide repair candidates.
func Lookup(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (Result, error) {
	w := newWalk(ctx, limits)
	nodes, ambiguous := w.nodes(root, path, value, 0)
	if err := w.finish(); err != nil {
		return Result{}, err
	}
	return Result{Nodes: nodes, Ambiguous: ambiguous, Complete: true}, nil
}

// Values returns deterministic finite candidates from enum/const metadata that
// satisfy the selected path constraints. Lookup and every acceptance check share
// one budget, so an incomplete candidate set is never reported as complete.
func Values(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) ([]any, error) {
	w := newWalk(ctx, limits)
	nodes, _ := w.nodes(root, path, value, 0)
	candidates := map[string]any{}
	for _, s := range nodes {
		values := []any{}
		if s.Enum != nil {
			values = append(values, s.Enum.Values...)
		}
		if s.Const != nil {
			values = append(values, *s.Const)
		}
		for _, candidate := range values {
			if !w.visit(0) {
				return nil, w.finish()
			}
			raw, err := json.Marshal(candidate)
			if err != nil {
				return nil, err
			}
			candidates[string(raw)] = candidate
		}
	}
	names := make([]string, 0, len(candidates))
	for name := range candidates {
		names = append(names, name)
	}
	sort.Strings(names)
	result := []any{}
	for _, name := range names {
		candidate := candidates[name]
		if w.accepts(root, path, value, candidate, 0) {
			result = append(result, candidate)
		}
		if err := w.finish(); err != nil {
			return nil, err
		}
	}
	if err := w.finish(); err != nil {
		return nil, err
	}
	return result, nil
}

// AllowsKey applies enclosing object closure without validating a placeholder
// value or requiring a complete object. Open objects remain open; Kubernetes
// preserve-unknown-fields regions must be supplied as open compiled objects.
func AllowsKey(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (bool, error) {
	w := newWalk(ctx, limits)
	allowed := w.allowsKey(root, path, value, 0)
	if err := w.finish(); err != nil {
		return false, err
	}
	return allowed, nil
}

// Type summarizes the allowed primitive types along the active path. Intersection
// applies to conjunctions and union to possible alternatives; a number includes
// integer. The annotation is "unspecified" for an unconstrained schema.
func Type(ctx context.Context, root *jsonschema.Schema, path []string, value any, limits Limits) (string, error) {
	w := newWalk(ctx, limits)
	types := w.types(root, path, value, 0)
	if err := w.finish(); err != nil {
		return "", err
	}
	if types == nil || len(types) == 7 {
		return "unspecified", nil
	}
	if len(types) == 0 {
		return "never", nil
	}
	names := []string{}
	for name := range types {
		if name != "integer" || !types["number"] {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return strings.Join(names, " | "), nil
}

// SelectShapes selects caller-supplied alternatives using existing field names
// only. It ignores the active field, missing required fields and ordinary value
// errors. The caller decides whether and how to use an empty selection (for
// example, retain an automatic version catalog to offer repairs).
func SelectShapes(ctx context.Context, branches []*jsonschema.Schema, value any, active []string, limits Limits) ([]*jsonschema.Schema, error) {
	w := newWalk(ctx, limits)
	possible := []*jsonschema.Schema{}
	for _, branch := range branches {
		if w.err != nil {
			break
		}
		if w.shape(branch, value, active, 0) {
			possible = append(possible, branch)
		}
	}
	if err := w.finish(); err != nil {
		return nil, err
	}
	return possible, nil
}

func child(schema *jsonschema.Schema, name string, value any) *jsonschema.Schema {
	if _, ok := value.([]any); ok {
		index, err := strconv.Atoi(name)
		if err != nil || index < 0 {
			return nil
		}
		if index >= 0 && index < len(schema.PrefixItems) {
			return schema.PrefixItems[index]
		}
		if schema.Items2020 != nil {
			return schema.Items2020
		}
		switch items := schema.Items.(type) {
		case *jsonschema.Schema:
			return items
		case []*jsonschema.Schema:
			if index < len(items) {
				return items[index]
			}
			switch additional := schema.AdditionalItems.(type) {
			case *jsonschema.Schema:
				return additional
			case bool:
				return &jsonschema.Schema{Bool: &additional}
			}
		}
		return nil
	}
	if child := schema.Properties[name]; child != nil {
		return child
	}
	child, _ := schema.AdditionalProperties.(*jsonschema.Schema)
	return child
}

// Only explicit finite sibling constraints can select a branch. The active
// field is excluded, so its partial value cannot eliminate its alternatives.
func (w *walk) compatible(s *jsonschema.Schema, path []string, value any, depth int) bool {
	if s == nil || !w.visit(depth) {
		return false
	}
	if s.Bool != nil && !*s.Bool {
		return false
	}
	if object, ok := value.(map[string]any); ok {
		for name, v := range object {
			if !w.visit(depth) {
				return false
			}
			child := s.Properties[name]
			if len(path) > 0 && name == path[0] {
				continue
			}
			if child != nil && w.finite(child, 0) && !w.validates(child, v) {
				return false
			}
		}
	}
	if s.Ref != nil && !w.compatible(s.Ref, path, value, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !w.compatible(child, path, value, depth+1) {
			return false
		}
	}
	for _, branches := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		if len(branches) == 0 {
			continue
		}
		possible := false
		for _, child := range branches {
			if w.err != nil {
				return false
			}
			if possible {
				break
			}
			possible = possible || w.compatible(child, path, value, depth+1)
		}
		if !possible {
			return false
		}
	}
	return true
}
func (w *walk) finite(s *jsonschema.Schema, depth int) bool {
	if s == nil || !w.visit(depth) {
		return false
	}
	if s.Const != nil || s.Enum != nil {
		return true
	}
	if w.finite(s.Ref, depth+1) {
		return true
	}
	for _, group := range [][]*jsonschema.Schema{s.AllOf, s.AnyOf, s.OneOf} {
		for _, child := range group {
			if w.err != nil {
				return false
			}
			if w.finite(child, depth+1) {
				return true
			}
		}
	}
	return false
}

// Traversal reads the same compiled graph used by validation. Alternatives are
// not globally deduplicated: duplicate oneOf branches still count separately.
func (w *walk) nodes(s *jsonschema.Schema, path []string, value any, depth int) ([]*jsonschema.Schema, bool) {
	if s == nil || !w.inspect(s, depth) {
		return nil, false
	}
	result := []*jsonschema.Schema{}
	ambiguous := false
	if len(path) == 0 {
		result = append(result, s)
	} else if child := child(s, path[0], value); child != nil {
		nodes, a := w.nodes(child, path[1:], member(value, path[0]), depth+1)
		result = append(result, nodes...)
		ambiguous = ambiguous || a
	}
	if s.Ref != nil {
		nodes, a := w.nodes(s.Ref, path, value, depth+1)
		result = append(result, nodes...)
		ambiguous = ambiguous || a
	}
	for _, child := range s.AllOf {
		if w.err != nil {
			return nil, false
		}
		nodes, a := w.nodes(child, path, value, depth+1)
		result = append(result, nodes...)
		ambiguous = ambiguous || a
	}
	for _, branches := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		possible := []*jsonschema.Schema{}
		for _, child := range branches {
			if w.err != nil {
				return nil, false
			}
			if w.compatible(child, path, value, depth+1) {
				possible = append(possible, child)
			}
		}
		if len(possible) > 1 {
			ambiguous = true
		}
		for _, child := range possible {
			if w.err != nil {
				return nil, false
			}
			nodes, a := w.nodes(child, path, value, depth+1)
			result = append(result, nodes...)
			ambiguous = ambiguous || a
		}
	}
	return result, ambiguous
}

// Key candidates have no chosen value yet. Apply enclosing closure and branch
// constraints without validating a placeholder value or requiring a complete
// object. Multiple possible oneOf branches remain candidates at this stage.
func (w *walk) allowsKey(s *jsonschema.Schema, path []string, value any, depth int) bool {
	if s == nil || !w.inspect(s, depth) || s.Bool != nil && !*s.Bool {
		return false
	}
	if len(path) > 0 {
		if child := child(s, path[0], value); child != nil {
			if !w.allowsKey(child, path[1:], member(value, path[0]), depth+1) {
				return false
			}
		} else if allowed, ok := s.AdditionalProperties.(bool); ok && !allowed {
			return false
		}
	}
	if s.Ref != nil && !w.allowsKey(s.Ref, path, value, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !w.allowsKey(child, path, value, depth+1) {
			return false
		}
	}
	for _, branches := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		if len(branches) == 0 {
			continue
		}
		possible := false
		for _, child := range branches {
			if w.err != nil {
				return false
			}
			if possible {
				break
			}
			// Invalid ordinary siblings do not forbid independent keys.
			// Finite siblings only constrain the selection of alternatives.
			possible = possible || w.compatible(child, path, value, depth+1) && w.allowsKey(child, path, value, depth+1)
		}
		if !possible {
			return false
		}
	}
	return true
}

// Project constraints along the active path, preserving conjunction, inclusive
// alternatives and exclusive cardinality. Leaf assertions use Schema.Validate.
func (w *walk) accepts(s *jsonschema.Schema, path []string, value, candidate any, depth int) bool {
	if s == nil || !w.inspect(s, depth) {
		return false
	}
	if len(path) == 0 {
		return w.validates(s, candidate)
	}
	if !w.compatible(s, path, value, depth) {
		return false
	}
	if child := child(s, path[0], value); child != nil {
		if !w.accepts(child, path[1:], member(value, path[0]), candidate, depth+1) {
			return false
		}
	} else if allowed, ok := s.AdditionalProperties.(bool); ok && !allowed {
		return false
	}
	if s.Ref != nil && !w.accepts(s.Ref, path, value, candidate, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !w.accepts(child, path, value, candidate, depth+1) {
			return false
		}
	}
	for i, branches := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		if len(branches) == 0 {
			continue
		}
		matches := 0
		for _, child := range branches {
			if w.err != nil {
				return false
			}
			if w.accepts(child, path, value, candidate, depth+1) {
				matches++
			}
		}
		if matches == 0 || i == 1 && matches != 1 {
			return false
		}
	}
	return true
}

func (w *walk) types(s *jsonschema.Schema, path []string, value any, depth int) map[string]bool {
	if s == nil || !w.inspect(s, depth) {
		return nil
	}
	result := map[string]bool{"null": true, "boolean": true, "number": true, "integer": true, "string": true, "array": true, "object": true}
	intersect := func(other map[string]bool) {
		if other != nil {
			for name := range result {
				if !other[name] {
					delete(result, name)
				}
			}
		}
	}
	if s.Bool != nil && !*s.Bool {
		return map[string]bool{}
	}
	if len(path) == 0 {
		if s.Types != nil {
			types := map[string]bool{}
			for _, name := range s.Types.ToStrings() {
				types[name] = true
				if name == "number" {
					types["integer"] = true
				}
			}
			intersect(types)
		}
	} else if child := child(s, path[0], value); child != nil {
		intersect(w.types(child, path[1:], member(value, path[0]), depth+1))
	} else if s.AdditionalProperties == false {
		// A closed release branch that has no such field contributes no types.
		// Treating it as unconstrained would erase types from other releases.
		return map[string]bool{}
	}
	if s.Ref != nil {
		intersect(w.types(s.Ref, path, value, depth+1))
	}
	for _, child := range s.AllOf {
		if w.err != nil {
			return nil
		}
		intersect(w.types(child, path, value, depth+1))
	}
	for _, branches := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		if len(branches) == 0 {
			continue
		}
		union := map[string]bool{}
		for _, child := range branches {
			if w.err != nil {
				return nil
			}
			if w.compatible(child, path, value, depth+1) {
				for name := range w.types(child, path, value, depth+1) {
					union[name] = true
				}
			}
		}
		intersect(union)
	}
	return result
}

func (w *walk) shape(s *jsonschema.Schema, value any, active []string, depth int) bool {
	if s == nil || !w.inspectShape(s, depth) {
		return false
	}
	visit := func(name string, childValue any) bool {
		if !w.visit(depth) {
			return false
		}
		childActive := []string(nil)
		if len(active) > 0 && name == active[0] {
			if len(active) == 1 {
				return true
			}
			childActive = active[1:]
		}
		if child := child(s, name, value); child != nil {
			switch childValue.(type) {
			case map[string]any, []any:
				return w.shape(child, childValue, childActive, depth+1)
			default:
				return true
			}
		}
		allowed, closed := s.AdditionalProperties.(bool)
		return !closed || allowed
	}
	switch v := value.(type) {
	case map[string]any:
		for name, child := range v {
			if !visit(name, child) {
				return false
			}
		}
	case []any:
		for index, child := range v {
			if !visit(strconv.Itoa(index), child) {
				return false
			}
		}
	}
	if s.Ref != nil && !w.shape(s.Ref, value, active, depth+1) {
		return false
	}
	for _, child := range s.AllOf {
		if !w.shape(child, value, active, depth+1) {
			return false
		}
	}
	for _, alternatives := range [][]*jsonschema.Schema{s.AnyOf, s.OneOf} {
		if len(alternatives) == 0 {
			continue
		}
		possible := false
		for _, child := range alternatives {
			if w.err != nil {
				return false
			}
			if w.shape(child, value, active, depth+1) {
				possible = true
				break
			}
		}
		if !possible {
			return false
		}
	}
	return true
}

func member(value any, name string) any {
	switch v := value.(type) {
	case map[string]any:
		return v[name]
	case []any:
		i, err := strconv.Atoi(name)
		if err == nil && i >= 0 && i < len(v) {
			return v[i]
		}
	}
	return nil
}
