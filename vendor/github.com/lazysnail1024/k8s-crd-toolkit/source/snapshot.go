// Package source provides immutable UTF-8 text snapshots and UTF-16 coordinates.
// It contains no filesystem, editor, Kubernetes, or domain-specific behavior.
package source

import (
	"fmt"
	"sort"
	"unicode/utf8"
)

type URI string
type Revision uint64

// Span is a half-open UTF-8 byte range in one snapshot.
type Span struct{ Start, End int }
type Position struct{ Line, UTF16Column uint32 }
type Change struct {
	Range Span
	Text  string
}

type line struct{ start, end int }

// Snapshot is immutable. Its zero value is an empty snapshot at revision zero.
// Text, URI and revisions have no connection to mutable files or LSP versions.
type Snapshot struct {
	uri      URI
	revision Revision
	text     string
	lines    []line
}

func New(uri URI, revision Revision, text string) (Snapshot, error) {
	if !utf8.ValidString(text) {
		return Snapshot{}, fmt.Errorf("source: text is not valid UTF-8")
	}
	s := Snapshot{uri: uri, revision: revision, text: text}
	start := 0
	for i := 0; i < len(text); i++ {
		if text[i] != '\n' {
			continue
		}
		end := i
		if i > start && text[i-1] == '\r' {
			end--
		}
		s.lines = append(s.lines, line{start: start, end: end})
		start = i + 1
	}
	s.lines = append(s.lines, line{start: start, end: len(text)})
	return s, nil
}

func (s Snapshot) URI() URI           { return s.uri }
func (s Snapshot) Revision() Revision { return s.revision }
func (s Snapshot) Text() string       { return s.text }

func (s Snapshot) lineIndex() []line {
	if len(s.lines) == 0 {
		return []line{{}}
	}
	return s.lines
}

func (s Snapshot) boundary(offset int) bool {
	return offset >= 0 && offset <= len(s.text) &&
		(offset == len(s.text) || utf8.RuneStart(s.text[offset]))
}

// Position rejects out-of-range offsets, UTF-8 interiors and the middle of CRLF.
// A CRLF's CR offset is the preceding line's end; the next line starts after LF.
// BOM is preserved and counts as one UTF-16 code unit, like any other U+FEFF.
func (s Snapshot) Position(offset int) (Position, error) {
	if !s.boundary(offset) {
		return Position{}, fmt.Errorf("source: invalid byte offset %d", offset)
	}
	lines := s.lineIndex()
	i := sort.Search(len(lines), func(i int) bool { return lines[i].start > offset }) - 1
	if offset > lines[i].end {
		return Position{}, fmt.Errorf("source: byte offset %d is inside CRLF", offset)
	}
	column := uint64(0)
	for _, r := range s.text[lines[i].start:offset] {
		column++
		if r > 0xffff {
			column++
		}
	}
	if uint64(i) > uint64(^uint32(0)) || column > uint64(^uint32(0)) {
		return Position{}, fmt.Errorf("source: coordinate exceeds uint32 range")
	}
	return Position{Line: uint32(i), UTF16Column: uint32(column)}, nil
}

// Offset rejects out-of-range columns and UTF-16 surrogate pair interiors.
func (s Snapshot) Offset(p Position) (int, error) {
	lines := s.lineIndex()
	if uint64(p.Line) >= uint64(len(lines)) {
		return 0, fmt.Errorf("source: line %d is out of range", p.Line)
	}
	l := lines[p.Line]
	column := uint64(0)
	wanted := uint64(p.UTF16Column)
	for i, r := range s.text[l.start:l.end] {
		if column == wanted {
			return l.start + i, nil
		}
		column++
		if r > 0xffff {
			column++
		}
		if column > wanted {
			return 0, fmt.Errorf("source: column %d is inside a surrogate pair", p.UTF16Column)
		}
	}
	if column == wanted {
		return l.end, nil
	}
	return 0, fmt.Errorf("source: column %d is out of range", p.UTF16Column)
}

func (s Snapshot) Slice(span Span) (string, error) {
	if span.Start > span.End || !s.boundary(span.Start) || !s.boundary(span.End) {
		return "", fmt.Errorf("source: invalid byte span [%d,%d)", span.Start, span.End)
	}
	return s.text[span.Start:span.End], nil
}

// Apply applies changes in order, with each range referring to the preceding
// change's result. Failure returns no new snapshot and never modifies s.
func (s Snapshot) Apply(next Revision, changes []Change) (Snapshot, error) {
	if next <= s.revision {
		return Snapshot{}, fmt.Errorf("source: next revision must increase")
	}
	text := s.text
	for i, change := range changes {
		view := Snapshot{text: text}
		if _, err := view.Slice(change.Range); err != nil {
			return Snapshot{}, fmt.Errorf("source: change %d: %w", i, err)
		}
		if !utf8.ValidString(change.Text) {
			return Snapshot{}, fmt.Errorf("source: change %d is not valid UTF-8", i)
		}
		text = text[:change.Range.Start] + change.Text + text[change.Range.End:]
	}
	return New(s.uri, next, text)
}
