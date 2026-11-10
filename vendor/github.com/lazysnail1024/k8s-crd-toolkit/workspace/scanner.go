package workspace

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

type ScanOptions struct {
	Extensions     []string
	ExcludeDirs    []string
	MaxDirectories int
}

type ScanIssue struct {
	Path string
	Err  error
}

// Batch contains usable partial results even when Next returns cancellation.
// Done means enumeration ended; Complete additionally excludes I/O/limit errors.
// Neither establishes that analysis finished or that disk stopped changing.
type Batch struct {
	Files            []string
	Visited, Pending int
	Done, Complete   bool
	Issues           []ScanIssue
}

type directoryWork struct {
	path     string
	priority bool
}

type directoryCursor struct {
	work    *directoryWork
	file    *os.File
	entries []fs.DirEntry
	err     error
}

// Scanner is a resumable, serially used discovery cursor. It retains at most
// two directory handles and 512 entries. Prioritizing a related directory can
// pause a background directory without restarting its potentially large scan.
// Workers and filesystem watching are the consumer's responsibility.
type Scanner struct {
	roots      []string
	extensions map[string]bool
	excluded   map[string]bool
	maximum    int
	// An absent path is unseen; an element owns queued/active/paused work;
	// nil retains only the identity of a completed or failed directory.
	seen       map[string]*list.Element
	pending    list.List
	active     *directoryCursor
	paused     *directoryCursor
	incomplete bool
	closed     bool
}

// NewScanner normalizes paths without opening roots, enumerating directories,
// or reading file contents. Relative paths may consult the working directory.
// Roots must denote directories. Symlinks below roots are not followed, and a
// root that is itself a symlink produces an explicit issue during discovery.
func NewScanner(roots []string, options ScanOptions) (*Scanner, error) {
	if options.MaxDirectories <= 0 {
		return nil, fmt.Errorf("workspace: directory limit must be positive")
	}
	s := &Scanner{maximum: options.MaxDirectories, seen: make(map[string]*list.Element), extensions: make(map[string]bool), excluded: make(map[string]bool)}
	for _, extension := range options.Extensions {
		if len(extension) < 2 || extension[0] != '.' || strings.ContainsAny(extension, `/\\`) {
			return nil, fmt.Errorf("workspace: invalid extension")
		}
		s.extensions[strings.ToLower(extension)] = true
	}
	for _, name := range options.ExcludeDirs {
		if name == "" || name == "." || name == ".." || strings.ContainsAny(name, `/\\`) {
			return nil, fmt.Errorf("workspace: exclusions must be directory basenames")
		}
		s.excluded[strings.Clone(name)] = true
	}
	for _, root := range roots {
		path, err := absoluteDirectory(root)
		if err != nil {
			return nil, err
		}
		if _, exists := s.seen[path]; exists {
			continue
		}
		s.roots = append(s.roots, path)
		if err := s.enqueue(path, false); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func absoluteDirectory(path string) (string, error) {
	if path == "" {
		return "", fmt.Errorf("workspace: empty directory")
	}
	return filepath.Abs(path)
}

func (s *Scanner) enqueue(path string, priority bool) error {
	if element, exists := s.seen[path]; exists {
		if priority && element != nil {
			element.Value.(*directoryWork).priority = true
			// MoveToFront is a no-op for an active/paused element already removed.
			s.pending.MoveToFront(element)
		}
		return nil
	}
	if len(s.seen) >= s.maximum {
		return ErrLimit
	}
	work := &directoryWork{path: path, priority: priority}
	var element *list.Element
	if priority {
		element = s.pending.PushFront(work)
	} else {
		element = s.pending.PushBack(work)
	}
	s.seen[path] = element
	return nil
}

// relativeRoot chooses the most specific explicitly supplied root. This lets
// an explicit root be scanned even if its basename is excluded under another.
func (s *Scanner) relativeRoot(path string) (string, string, bool) {
	best, relative := "", ""
	for _, root := range s.roots {
		rel, err := filepath.Rel(root, path)
		if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && len(root) > len(best) {
			best, relative = root, rel
		}
	}
	return best, relative, best != ""
}

// Prioritize discovers an as-yet-unvisited related directory before background
// directories, including when its parents have not been enumerated. At most
// one background cursor is paused; an active priority directory is not preempted.
// Completed directories are not rescanned. Start a new scanner for a new pass.
func (s *Scanner) Prioritize(directory string) error {
	if s.closed {
		return fs.ErrClosed
	}
	path, err := absoluteDirectory(directory)
	if err != nil {
		return err
	}
	_, relative, inside := s.relativeRoot(path)
	if !inside {
		return fmt.Errorf("workspace: priority directory is outside roots")
	}
	for _, part := range strings.Split(relative, string(filepath.Separator)) {
		if s.excluded[part] {
			return fmt.Errorf("workspace: priority directory is excluded")
		}
	}
	return s.enqueue(path, true)
}

func (s *Scanner) urgent() bool {
	return s.pending.Front() != nil && s.pending.Front().Value.(*directoryWork).priority
}

func (s *Scanner) status(batch *Batch) {
	batch.Pending = s.pending.Len()
	if s.active != nil {
		batch.Pending++
	}
	if s.paused != nil {
		batch.Pending++
	}
	batch.Done = batch.Pending == 0
	batch.Complete = batch.Done && !s.incomplete && !s.closed
}

func (s *Scanner) issue(batch *Batch, path string, err error) {
	s.incomplete = true
	batch.Issues = append(batch.Issues, ScanIssue{Path: path, Err: err})
}

// Next performs at most maxWork units (1..4096). A unit is an inspected directory
// entry, an opened directory, or an empty directory read. Nonmatching files count
// too, so a large non-YAML directory cannot monopolize a batch. Cancellation is
// checked between operations; an OS syscall itself is not forcibly interruptible.
func (s *Scanner) Next(ctx context.Context, maxWork int) (batch Batch, err error) {
	if s.closed {
		return batch, fs.ErrClosed
	}
	if ctx == nil || maxWork <= 0 || maxWork > 4096 {
		return batch, fmt.Errorf("workspace: context and work budget (1..4096) required")
	}
	defer func() {
		s.status(&batch)
		if err != nil {
			batch.Complete = false
		}
	}()
	for batch.Visited < maxWork {
		if err := ctx.Err(); err != nil {
			return batch, err
		}
		if s.active != nil && !s.active.work.priority && s.urgent() {
			s.paused, s.active = s.active, nil
		}
		if s.active == nil {
			if err := s.activate(ctx, &batch); err != nil {
				return batch, err
			}
			if s.active == nil && s.pending.Len() == 0 {
				break
			}
			continue
		}
		if len(s.active.entries) != 0 {
			entry := s.active.entries[0]
			s.active.entries = s.active.entries[1:]
			batch.Visited++
			s.inspect(entry, &batch)
			continue
		}
		if s.active.err != nil {
			s.finish(&batch)
			continue
		}
		count := min(256, maxWork-batch.Visited)
		s.active.entries, s.active.err = s.active.file.ReadDir(count)
		if len(s.active.entries) == 0 {
			batch.Visited++
		}
	}
	return batch, nil
}

func (s *Scanner) activate(ctx context.Context, batch *Batch) error {
	if s.paused != nil && !s.urgent() {
		s.active, s.paused = s.paused, nil
		return nil
	}
	element := s.pending.Front()
	if element == nil {
		return nil
	}
	work := element.Value.(*directoryWork)
	file, err := s.openDirectory(ctx, work.path)
	batch.Visited++
	if err != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	s.pending.Remove(element)
	if err != nil {
		s.seen[work.path] = nil
		s.issue(batch, work.path, err)
		return nil
	}
	s.active = &directoryCursor{work: work, file: file}
	return nil
}

func (s *Scanner) openDirectory(ctx context.Context, path string) (*os.File, error) {
	root, relative, inside := s.relativeRoot(path)
	if !inside {
		return nil, fmt.Errorf("workspace: directory outside roots")
	}
	// Check every component below the explicit root, including priority paths
	// that bypassed normal directory discovery. This is not a security sandbox
	// against concurrent hostile filesystem mutations.
	current := root
	parts := append([]string{"."}, strings.Split(relative, string(filepath.Separator))...)
	for _, part := range parts {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return nil, fmt.Errorf("workspace: expected a directory without symlinks: %s", current)
		}
	}
	return os.Open(path)
}

func (s *Scanner) inspect(entry fs.DirEntry, batch *Batch) {
	path := filepath.Join(s.active.work.path, entry.Name())
	if entry.Type()&os.ModeSymlink != 0 {
		return
	}
	if entry.IsDir() {
		if !s.excluded[entry.Name()] {
			if err := s.enqueue(path, s.active.work.priority); err != nil {
				s.issue(batch, path, err)
			}
		}
		return
	}
	if len(s.extensions) != 0 && !s.extensions[strings.ToLower(filepath.Ext(entry.Name()))] {
		return
	}
	info, err := entry.Info()
	if err != nil {
		s.issue(batch, path, err)
		return
	}
	if info.Mode().IsRegular() {
		batch.Files = append(batch.Files, path)
	}
}

func (s *Scanner) finish(batch *Batch) {
	cursor := s.active
	if !errors.Is(cursor.err, io.EOF) {
		s.issue(batch, cursor.work.path, cursor.err)
	}
	if err := cursor.file.Close(); err != nil {
		s.issue(batch, cursor.work.path, err)
	}
	s.seen[cursor.work.path] = nil
	s.active = nil
}

// Close releases both active and paused directory handles. A closed scanner
// cannot resume or report Complete. Closing twice is harmless.
func (s *Scanner) Close() error {
	s.closed = true
	var result error
	for _, cursor := range []*directoryCursor{s.active, s.paused} {
		if cursor != nil {
			result = errors.Join(result, cursor.file.Close())
		}
	}
	s.active, s.paused = nil, nil
	s.seen = nil
	s.pending.Init()
	return result
}
