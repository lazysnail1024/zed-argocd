// Package grammar contains a pinned YAML grammar with a bounded scanner-state fix.
package grammar

// #cgo CFLAGS: -std=c11 -fPIC
// #include "parser.c.inc"
// #include "scanner.c.inc"
// static void toolkit_yaml_begin(void) { toolkit_yaml_budget_exceeded = false; }
// static bool toolkit_yaml_exceeded(void) { return toolkit_yaml_budget_exceeded; }
import "C"
import "unsafe"

func Language() unsafe.Pointer { return unsafe.Pointer(C.tree_sitter_yaml()) }

// Begin and Exceeded must be called on the same locked OS thread as Parse.
func Begin()         { C.toolkit_yaml_begin() }
func Exceeded() bool { return bool(C.toolkit_yaml_exceeded()) }
