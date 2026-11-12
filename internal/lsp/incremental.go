package lsp

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	shared "github.com/lazysnail1024/k8s-crd-toolkit/workspace"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

const (
	indexFileLimit    = 100000
	indexSummaryBytes = 96 * 1024 * 1024
	indexBatchFiles   = 32
	indexScanWork     = 256
	indexQueueLimit   = 512
)

// File events are coalesced by the serial LSP owner. Reconciliation requests a
// new discovery pass after the current pass; it never throws away its cursor.
type workspaceEvents struct {
	files     map[string]bool
	reset     map[string]bool
	reconcile map[string]bool
}

type indexedFile struct {
	uri                  source.URI // canonical acquisition identity, separate from client spelling
	path, spelling, root string
	target               workspace.Target
	hash                 [32]byte
	revision             source.Revision
	summary              analysis.Summary
	schema, diagnostics  []analysis.Diagnostic
	incomplete           bool
	err                  error
	seen                 shared.Observation
	// The queue owns waiting/running identity. This flag only selects whether
	// the next analysis needs fresh input or can use committed domain facts.
	workRead    bool
	bytes       int
	event       uint64
	invalidated bool
}

type indexedRoot struct {
	catalog                  *workspace.Catalog
	err                      error
	scanner                  *shared.Scanner
	membership               shared.Membership
	scanPass                 shared.ScanPass
	discovered, scanComplete bool
	rescan, finalDomain      bool
	readPending, pending     int
	issues                   map[string]int
	scopeReads, scopeInvalid map[string]int
	projectGeneration        map[string]uint64
	deferredDomain           map[source.URI]bool // Bounded by registered files, no retained source.
	passActive               bool
	configEvent              uint64
}

// workspaceIndex retains the domain payload and completeness evidence for one
// LSP session. Short locks let queries borrow immutable summaries while its
// background worker is outside the lock doing filesystem/parse work.
// No closed-file Snapshot, CST or projected YAML graph is stored here.
type workspaceIndex struct {
	mu           sync.RWMutex
	documents    *shared.Documents
	facts        *index.Index
	queue        *shared.WorkQueue
	roots        map[string]*indexedRoot
	files        map[source.URI]*indexedFile
	open         map[source.URI]buffer
	override     string
	rootOrder    []string
	summaryBytes int
	analyzer     analysis.Analyzer
	closed       bool
	running      bool // An active advance owns scanner shutdown while outside mu.
	event        uint64
	configEvents map[string]uint64
}

func newWorkspaceIndex() *workspaceIndex {
	documents, _ := shared.NewDocuments(shared.DocumentLimits{Entries: indexFileLimit, FileBytes: workspace.MaxFileText, OpenBytes: workspace.MaxRetainedText})
	facts, _ := index.New(index.Limits{Files: indexFileLimit, Symbols: 500000, References: 1000000, Bytes: indexSummaryBytes})
	// A removed in-flight job keeps its execution slot until Complete, while a
	// replacement file may already be admitted within the unchanged file limit.
	queue, _ := shared.NewWorkQueue(indexQueueLimit, indexFileLimit+1)
	return &workspaceIndex{documents: documents, facts: facts, queue: queue, roots: map[string]*indexedRoot{}, files: map[source.URI]*indexedFile{}, open: map[source.URI]buffer{}, configEvents: map[string]uint64{}}
}

func indexScope(root, scope string) index.Scope { return index.Scope(root + "\x00" + scope) }

// A remove/add of the same workspace root invalidates its interpretation even
// before the debounced background worker rebuilds the catalog.
func (s *workspaceIndex) invalidateRoot(root string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.event++
	s.configEvents[root] = s.event
}

// Withdraw at notification time, before debounce or disk I/O. The event fence
// also prevents an already-running job from reintroducing superseded facts.
func (s *workspaceIndex) withdraw(spelling string) {
	path, err := workspace.PathFromURI(spelling)
	if err != nil {
		return
	}
	uri := source.URI(workspace.FileURI(path))
	s.mu.Lock()
	defer s.mu.Unlock()
	if filepath.Base(path) == ".argocd-ls.yaml" {
		if root := workspace.Owner(s.rootOrder, path); root != "" {
			s.event++
			s.configEvents[root] = s.event
		}
	}
	file := s.files[uri]
	if file == nil {
		if strings.HasSuffix(path, ".yaml") || strings.HasSuffix(path, ".yml") {
			s.enqueue(uri, true, shared.Related)
		}
		return
	}
	copy := *file
	s.event++
	copy.event = s.event
	if root := s.roots[copy.root]; root != nil {
		copy.seen = root.membership.Observe()
	}
	if !copy.invalidated {
		copy.invalidated = true
		if root := s.roots[copy.root]; root != nil {
			root.scopeInvalid[copy.target.Scope]++
		}
	}
	if copy.revision != 0 {
		_, _ = s.facts.Begin(uri, indexScope(copy.root, copy.target.Scope), copy.revision)
	}
	s.documents.Invalidate(source.URI(copy.spelling))
	s.files[uri] = &copy
}

func (s *workspaceIndex) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	if !s.running {
		for _, root := range s.roots {
			if root.scanner != nil {
				_ = root.scanner.Close()
			}
		}
	}
}

func (s *workspaceIndex) enqueue(uri source.URI, read bool, priority shared.Priority) {
	file := s.files[uri]
	path := ""
	var err error
	if file != nil {
		path = file.path
	} else {
		path, err = workspace.PathFromURI(string(uri))
	}
	if err != nil {
		return
	}
	root := s.roots[workspace.Owner(s.rootOrder, path)]
	if root == nil || root.catalog == nil {
		return
	}
	if file == nil {
		if len(s.files) >= indexFileLimit {
			root.err = fmt.Errorf("workspace exceeds 100,000 candidate files")
			return
		}
		file = &indexedFile{uri: uri, path: path, spelling: string(uri), root: root.catalog.Input.Root}
		file.target, _, _ = root.catalog.Select(path, true)
		file.bytes = retainedFileBytes(file)
		if s.summaryBytes+file.bytes > indexSummaryBytes {
			root.err = fmt.Errorf("workspace file metadata exceeds %d MiB budget", indexSummaryBytes/(1024*1024))
			return
		}
		s.summaryBytes += file.bytes
		s.files[uri] = file
	}
	waiting := s.queue.Waiting(uri)
	if err := s.queue.Put(shared.Job{URI: uri, Priority: priority}); err != nil {
		root.err = err
		return
	}
	copy := *file
	if read {
		copy.seen = root.membership.Observe()
	}
	if !waiting {
		root.pending++
	}
	if read && !copy.workRead {
		root.readPending++
		root.scopeReads[copy.target.Scope]++
	}
	copy.workRead = copy.workRead || read
	s.files[uri] = &copy
}

func (s *workspaceIndex) remove(uri source.URI) publication {
	old := s.files[uri]
	if old == nil {
		return publication{}
	}
	change, _ := s.facts.Remove(uri)
	s.summaryBytes -= old.bytes
	delete(s.files, uri)
	if root := s.roots[old.root]; root != nil {
		delete(root.deferredDomain, uri)
	}
	if s.queue.Waiting(uri) {
		if root := s.roots[old.root]; root != nil {
			root.pending--
			if old.workRead {
				root.readPending--
				root.scopeReads[old.target.Scope]--
			}
		}
	}
	s.queue.Remove(uri)
	if old.err != nil || old.incomplete {
		if root := s.roots[old.root]; root != nil {
			root.issues[old.target.Scope]--
		}
	}
	if old.invalidated {
		if root := s.roots[old.root]; root != nil {
			root.scopeInvalid[old.target.Scope]--
		}
	}
	if hasProject(old.summary) {
		if root := s.roots[old.root]; root != nil {
			root.projectGeneration[old.target.Scope]++
		}
	}
	for _, affected := range change.Affected {
		if affected != uri {
			s.enqueue(affected, false, shared.Related)
		}
	}
	return publication{uri: old.spelling, diagnostics: []analysis.Diagnostic{}}
}

func (s *workspaceIndex) startScan(root *indexedRoot) {
	if root.catalog == nil {
		return
	}
	root.scanPass = root.membership.Start()
	root.discovered, root.scanComplete, root.finalDomain = false, false, true
	root.passActive = true
	root.scanner, root.err = shared.NewScanner([]string{root.catalog.Input.Root}, shared.ScanOptions{Extensions: []string{".yaml", ".yml"}, ExcludeDirs: root.catalog.ScanExclusions(), MaxDirectories: indexFileLimit})
}

func (s *workspaceIndex) apply(ctx context.Context, roots []string, buffers map[string]buffer, override string, events workspaceEvents) []publication {
	publications := []publication{}
	s.mu.Lock()
	previousRoots := append([]string(nil), s.rootOrder...)
	ownershipChanged := func(root string) bool {
		for _, set := range [][]string{previousRoots, roots} {
			for _, candidate := range set {
				before, after := false, false
				for _, old := range previousRoots {
					before = before || old == candidate
				}
				for _, current := range roots {
					after = after || current == candidate
				}
				if before != after && (pathWithin(root, candidate) || pathWithin(candidate, root)) {
					return true
				}
			}
		}
		return false
	}
	s.rootOrder = append([]string(nil), roots...)
	wanted := map[string]bool{}
	for _, root := range roots {
		wanted[root] = true
	}
	for root, previous := range s.roots {
		if wanted[root] {
			continue
		}
		if previous.scanner != nil {
			_ = previous.scanner.Close()
		}
		for uri, file := range s.files {
			if file.root == root {
				publications = append(publications, s.remove(uri))
			}
		}
		delete(s.roots, root)
		delete(s.configEvents, root)
	}
	oldOverride := s.override
	s.override = override
	s.mu.Unlock()
	for _, path := range roots {
		s.mu.RLock()
		previous := s.roots[path]
		configEvent := s.configEvents[path]
		reset := previous == nil || ownershipChanged(path) || oldOverride != override
		forced := events.reset[path]
		s.mu.RUnlock()
		if !reset && !forced && !events.reconcile[path] {
			continue
		}
		var config *string
		var configErr error
		configURI := workspace.FileURI(filepath.Join(path, ".argocd-ls.yaml"))
		configSpelling := configURI
		for uri, b := range buffers {
			p, _ := workspace.PathFromURI(uri)
			if p == filepath.Join(path, ".argocd-ls.yaml") {
				configSpelling = uri
				if b.unavailable != "" {
					configErr = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: b.unavailable}
				} else {
					text := b.snapshot.Text()
					config = &text
				}
			}
		}
		var catalog *workspace.Catalog
		err := configErr
		if err == nil {
			catalog, err = workspace.Prepare(ctx, path, override, config, roots)
		}
		// A config notification requires fresh bytes, not a reset when those
		// bytes and target policy are unchanged. Folder re-add remains forced.
		configNotified := events.files[configURI]
		for spelling := range events.files {
			p, _ := workspace.PathFromURI(spelling)
			configNotified = configNotified || p == filepath.Join(path, ".argocd-ls.yaml")
		}
		if !reset && (!forced || configNotified) && err == nil && previous.catalog != nil && reflect.DeepEqual(previous.catalog.Input.Config, catalog.Input.Config) && reflect.DeepEqual(previous.catalog.Input.Targets, catalog.Input.Targets) {
			s.mu.Lock()
			previous.configEvent = configEvent
			s.mu.Unlock()
			continue
		}
		s.mu.Lock()
		if previous != nil && previous.scanner != nil {
			_ = previous.scanner.Close()
		}
		for uri, file := range s.files {
			if file.root == path {
				publications = append(publications, s.remove(uri))
			}
		}
		root := &indexedRoot{catalog: catalog, err: err, issues: map[string]int{}, scopeReads: map[string]int{}, scopeInvalid: map[string]int{}, projectGeneration: map[string]uint64{}, deferredDomain: map[source.URI]bool{}, configEvent: configEvent}
		s.roots[path] = root
		if err == nil {
			s.startScan(root)
		} else {
			publications = append(publications, publication{uri: configSpelling, diagnostics: []analysis.Diagnostic{inputDiagnostic(configSpelling, err)}})
		}
		s.mu.Unlock()
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	open := map[source.URI]buffer{}
	for uri, b := range buffers {
		path, err := workspace.PathFromURI(uri)
		if err != nil || workspace.Owner(roots, path) == "" {
			continue
		}
		canonical := source.URI(workspace.FileURI(path))
		b.uri = uri
		open[canonical] = b
		old, existed := s.open[canonical]
		if !existed || s.files[canonical] == nil || old.revision != b.revision || old.version != b.version || old.unavailable != b.unavailable || old.snapshot.URI() != b.snapshot.URI() {
			s.enqueue(canonical, true, shared.Current)
			if root := s.roots[workspace.Owner(roots, path)]; root != nil && root.scanner != nil {
				_ = root.scanner.Prioritize(filepath.Dir(path))
			}
		}
	}
	for uri := range s.open {
		if _, exists := open[uri]; !exists {
			s.enqueue(uri, true, shared.Related)
		}
	}
	s.open = open
	for spelling := range events.files {
		path, err := workspace.PathFromURI(spelling)
		if err != nil {
			continue
		}
		root := s.roots[workspace.Owner(roots, path)]
		if root == nil || root.catalog == nil {
			continue
		}
		uri := source.URI(workspace.FileURI(path))
		if _, known := s.files[uri]; known || strings.HasSuffix(path, ".yaml") || strings.HasSuffix(path, ".yml") {
			s.enqueue(uri, true, shared.Related)
		}
		// Non-YAML paths are inputs to repository mappings and Helm ownership.
		// Only files using those inputs are resampled; no root YAML reacquisition.
		mappedEvent := filepath.Base(path) == "Chart.yaml"
		for _, repo := range root.catalog.Input.Config.Repositories {
			base := filepath.Join(root.catalog.Input.Root, repo.Path)
			mappedEvent = mappedEvent || pathWithin(base, path) || pathWithin(path, base)
		}
		if !mappedEvent {
			continue
		}
		for candidate, file := range s.files {
			if file.root != root.catalog.Input.Root {
				continue
			}
			if filepath.Base(path) == "Chart.yaml" && pathWithin(filepath.Dir(path), file.path) {
				s.enqueue(candidate, true, shared.Related)
				continue
			}
			for _, repo := range root.catalog.Input.Config.Repositories {
				base := filepath.Join(root.catalog.Input.Root, repo.Path)
				if !pathWithin(base, path) && !pathWithin(path, base) {
					continue
				}
				for _, ref := range file.summary.Repositories {
					if ref.RepoURL == repo.RepoURL && ref.TargetRevision == repo.TargetRevision {
						target := filepath.Join(base, ref.Path)
						if pathWithin(path, target) || pathWithin(target, path) {
							s.enqueue(candidate, false, shared.Related)
						}
					}
				}
			}
		}
	}
	for path := range events.reconcile {
		root := s.roots[path]
		if root == nil || root.catalog == nil {
			continue
		}
		if root.scanner == nil {
			s.startScan(root)
		} else {
			root.rescan = true
		}
	}
	return publications
}

func pathWithin(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func inputDiagnostic(uri string, err error) analysis.Diagnostic {
	code := "ANALYSIS_INCOMPLETE"
	if e, ok := err.(*workspace.Error); ok {
		code = e.Code
	}
	if errors.Is(err, shared.ErrLimit) || errors.Is(err, index.ErrLimit) {
		code = "INPUT_BUDGET_EXCEEDED"
	}
	return analysis.Diagnostic{URI: uri, Code: code, Source: "argocd-ls", Severity: 1, Message: err.Error()}
}

func diagnosticBytes(values []analysis.Diagnostic) int {
	bytes := 0
	for _, value := range values {
		bytes += 96 + len(value.URI) + len(value.Code) + len(value.Source) + len(value.TargetID) + len(value.Message)
	}
	return bytes
}

func retainedFileBytes(file *indexedFile) int {
	return 256 + len(file.uri) + len(file.path) + len(file.spelling) + len(file.root) + len(file.target.Scope) + file.summary.Bytes + diagnosticBytes(file.schema) + diagnosticBytes(file.diagnostics)
}

func boundedDiagnostics(values []analysis.Diagnostic) ([]analysis.Diagnostic, bool) {
	limited := len(values) > 256
	if limited {
		values = values[:256]
	}
	out := append([]analysis.Diagnostic(nil), values...)
	for i := range out {
		if len(out[i].Message) > 4096 {
			out[i].Message = out[i].Message[:4096] + "…"
			limited = true
		}
		out[i].URI, out[i].Code, out[i].Source = strings.Clone(out[i].URI), strings.Clone(out[i].Code), strings.Clone(out[i].Source)
		out[i].TargetID, out[i].Message = strings.Clone(out[i].TargetID), strings.Clone(out[i].Message)
	}
	return out, limited
}

// scopeComplete is independent of cached text. Discovery, queued reads and
// unknown input facts, not the absence of currently known references, prove it.
func (s *workspaceIndex) scopeComplete(root *indexedRoot, scope string) bool {
	if root == nil || root.catalog == nil || root.err != nil || !root.discovered || !root.scanComplete || root.scopeReads[scope] != 0 || root.scopeReads[""] != 0 || root.scopeInvalid[scope] != 0 || root.scopeInvalid[""] != 0 {
		return false
	}
	if strings.HasPrefix(scope, "isolated:") {
		return false
	}
	if s.configEvents[root.catalog.Input.Root] != root.configEvent {
		return false
	}
	return root.issues[scope] == 0 && root.issues[""] == 0
}

func hasProject(summary analysis.Summary) bool {
	for _, doc := range summary.Documents {
		if doc.Kind == "AppProject" {
			return true
		}
	}
	return false
}

func (s *workspaceIndex) related(file *indexedFile, facts index.Facts, references bool, candidates *index.SymbolQueryResult) ([]analysis.Document, bool) {
	docs := append([]analysis.Document(nil), file.summary.Documents...)
	seen := map[source.URI]bool{file.uri: true}
	add := func(uri source.URI) {
		if seen[uri] {
			return
		}
		seen[uri] = true
		if f := s.files[uri]; f != nil && f.err == nil && !f.incomplete && !f.invalidated {
			docs = append(docs, f.summary.Documents...)
		}
	}
	scope := indexScope(file.root, file.target.Scope)
	keys := map[index.Key]bool{}
	for _, ref := range facts.References {
		keys[ref.Target] = true
	}
	for _, key := range facts.Dependencies {
		keys[key] = true
	}
	if references {
		for _, symbol := range facts.Symbols {
			keys[symbol.Key] = true
		}
	}
	for key := range keys {
		defs, _ := s.facts.Definitions(scope, key)
		for _, def := range defs {
			add(def.URI)
		}
		if references {
			users, _ := s.facts.Dependents(scope, key)
			for _, uri := range users {
				add(uri)
			}
		}
	}
	if candidates != nil {
		complete := candidates.Complete && !candidates.Truncated
		for _, candidate := range candidates.Items {
			current := s.files[candidate.URI]
			if current == nil || current.revision != candidate.Revision || current.root != file.root || current.target.Scope != file.target.Scope || current.err != nil || current.incomplete || current.invalidated {
				complete = false
				continue
			}
			add(candidate.URI)
		}
		return docs, complete
	}
	return docs, true
}

func (s *workspaceIndex) domain(ctx context.Context, file *indexedFile) ([]analysis.Diagnostic, int) {
	if len(file.summary.Documents) == 0 {
		return file.schema, 0
	}
	s.mu.RLock()
	root := s.roots[file.root]
	if root == nil || root.catalog == nil {
		s.mu.RUnlock()
		return file.schema, 0
	}
	catalog := root.catalog
	entry, _, _ := s.facts.GetFile(file.uri)
	docs, _ := s.related(file, entry.Facts, false, nil)
	complete := s.scopeComplete(root, file.target.Scope)
	s.mu.RUnlock()
	paths, err := catalog.LocalPaths(ctx, file.summary.Repositories)
	if err != nil {
		return append(append([]analysis.Diagnostic(nil), file.schema...), inputDiagnostic(file.spelling, err)), len(docs)
	}
	options := analysis.DomainOptions{Context: ctx, Complete: complete, ControlPlaneNamespace: catalog.Input.Config.ControlPlaneNamespace, ReferencePolicy: catalog.Input.Config.ReferencePolicy, LocalPaths: paths}
	diagnostics := append([]analysis.Diagnostic(nil), file.schema...)
	for _, diagnostic := range analysis.Domain(docs, options) {
		if diagnostic.URI == file.spelling {
			diagnostics = append(diagnostics, diagnostic)
		}
	}
	return diagnostics, len(docs)
}

func (s *workspaceIndex) process(ctx context.Context, uri source.URI, read bool, releaseRead func(), result *completed) {
	s.mu.RLock()
	old := s.files[uri]
	path, _ := workspace.PathFromURI(string(uri))
	rootPath := workspace.Owner(s.rootOrder, path)
	root := s.roots[rootPath]
	b, opened := s.open[uri]
	s.mu.RUnlock()
	if old == nil || root == nil || root.catalog == nil {
		return
	}
	expectedEvent := old.event
	catalog := root.catalog
	spelling := string(uri)
	if opened {
		if b.snapshot.URI() != "" {
			spelling = string(b.snapshot.URI())
		} else if b.uri != "" {
			spelling = b.uri
		}
	}
	file := old
	if read || old.revision == 0 {
		target, included, err := catalog.Select(path, opened)
		remove := func() {
			s.mu.Lock()
			current := s.files[uri]
			var p publication
			if current != nil && current.event == expectedEvent {
				p = s.remove(uri)
			}
			s.mu.Unlock()
			if p.uri != "" {
				result.publications = append(result.publications, p)
				result.unchangedInputs = false
			}
		}
		if err == nil && !included {
			remove()
			return
		}
		var snapshot source.Snapshot
		if opened {
			if b.unavailable != "" {
				err = fmt.Errorf("%s", b.unavailable)
			} else {
				current, stillOpen := s.documents.Snapshot(b.snapshot.URI())
				if !stillOpen || current.Revision() != b.snapshot.Revision() {
					return
				}
				snapshot = b.snapshot
			}
		} else if err == nil {
			ticket, beginErr := s.documents.BeginRead(source.URI(spelling))
			err = beginErr
			if err == nil {
				text, readErr := workspace.ReadFile(ctx, path)
				result.stats.DiskReads++
				if readErr != nil {
					s.documents.DiscardRead(ticket)
					err = readErr
				} else {
					snapshot, err = s.documents.PublishRead(ticket, text)
				}
			}
		}
		if !opened && os.IsNotExist(err) {
			remove()
			return
		}
		if errors.Is(err, shared.ErrStale) || errors.Is(err, shared.ErrOverlay) {
			return
		}
		file = &indexedFile{uri: uri, path: path, spelling: spelling, root: rootPath, target: target, err: err, event: expectedEvent}
		if snapshot.URI() != "" {
			file.hash = sha256.Sum256([]byte(snapshot.Text()))
			file.revision = snapshot.Revision()
		}
		same := err == nil && old.err == nil && old.hash == file.hash && old.target == target && old.spelling == spelling
		if same {
			copy := *old
			file = &copy
			file.revision = snapshot.Revision()
			file.invalidated = false
		} else if err == nil {
			var checked analysis.Result
			if workspace.IsHelmTemplate(rootPath, path, snapshot) {
				checked.Diagnostics = []analysis.Diagnostic{{URI: spelling, Code: "HELM_UNRENDERED", Source: "argocd-ls", Severity: 3, Message: "Helm template is not rendered; static Argo CD checks are incomplete. Run the explicit render-check task and inspect its generated YAML diagnostics."}}
			} else {
				s.analyzer.BeginFile()
				checked, err = s.analyzer.Analyze(ctx, snapshot, target.Version)
				s.analyzer.BeginFile()
				result.stats.ParsedFiles++
			}
			if err == nil {
				file.summary = analysis.Summarize(checked.Documents, catalog.Input.Config.ControlPlaneNamespace)
				var limited bool
				file.schema, limited = boundedDiagnostics(checked.Diagnostics)
				file.incomplete = checked.Incomplete || limited
			}
		}
		s.mu.Lock()
		current := s.files[uri]
		if current == nil || current.event != expectedEvent {
			s.mu.Unlock()
			return
		}
		if err == nil && s.summaryBytes-current.bytes+retainedFileBytes(file) > indexSummaryBytes {
			err = fmt.Errorf("workspace summary exceeds %d MiB budget", indexSummaryBytes/(1024*1024))
		}
		var change index.Change
		if err == nil {
			// Ticket creation and commit share the owner event fence. A newer input
			// cannot reappear as fresh between committing facts and their summary.
			ticket, beginErr := s.facts.Begin(uri, indexScope(rootPath, target.Scope), file.revision)
			err = beginErr
			if err == nil {
				if same {
					change, err = s.facts.Reuse(ticket)
				} else {
					change, err = s.facts.Commit(ticket, file.summary.Facts)
				}
			}
		}
		if err != nil {
			file.err, file.incomplete = err, true
			file.summary = analysis.Summary{}
			file.schema = []analysis.Diagnostic{inputDiagnostic(spelling, err)}
			change, _ = s.facts.Remove(uri)
		}
		if current.err != nil || current.incomplete {
			root.issues[current.target.Scope]--
		}
		if file.err != nil || file.incomplete {
			root.issues[file.target.Scope]++
		}
		if current.invalidated {
			root.scopeInvalid[current.target.Scope]--
		}
		file.invalidated = false
		file.seen = root.membership.Observe()
		file.workRead = current.workRead
		// Index owns the committed symbols/references. The retained byte charge
		// still includes those facts, alongside this file's domain summary.
		file.summary.Facts = index.Facts{}
		file.bytes = retainedFileBytes(file)
		s.summaryBytes += file.bytes - current.bytes
		s.files[uri] = file
		if !same && (hasProject(current.summary) || hasProject(file.summary)) {
			root.projectGeneration[file.target.Scope]++
		}
		if (current.incomplete != file.incomplete) || ((current.err != nil) != (file.err != nil)) {
			root.finalDomain = true
		}
		for _, affected := range change.Affected {
			if affected != uri {
				s.enqueue(affected, false, shared.Related)
			}
		}
		s.mu.Unlock()
		if !same {
			result.unchangedInputs = false
		}
	}
	releaseRead()
	diagnostics, count := s.domain(ctx, file)
	result.stats.DomainDocuments += count
	diagnostics, limited := boundedDiagnostics(diagnostics)
	copy := *file
	copy.diagnostics = diagnostics
	s.mu.Lock()
	current := s.files[uri]
	if current == nil || current.event != expectedEvent || current.invalidated {
		s.mu.Unlock()
		return
	}
	copy.bytes = retainedFileBytes(&copy)
	if limited || s.summaryBytes-current.bytes+copy.bytes > indexSummaryBytes {
		copy.summary, copy.schema = analysis.Summary{}, nil
		copy.err, copy.incomplete = fmt.Errorf("workspace diagnostics exceed retained budget"), true
		copy.diagnostics = []analysis.Diagnostic{inputDiagnostic(spelling, copy.err)}
		if !file.incomplete && file.err == nil {
			root.issues[file.target.Scope]++
		}
		change, _ := s.facts.Remove(uri)
		for _, affected := range change.Affected {
			if affected != uri {
				s.enqueue(affected, false, shared.Related)
			}
		}
		copy.bytes = retainedFileBytes(&copy)
		diagnostics = copy.diagnostics
	}
	s.summaryBytes += copy.bytes - current.bytes
	s.files[uri] = &copy
	s.mu.Unlock()
	var version *int
	if opened {
		v := b.version
		version = &v
	}
	s.mu.Lock()
	phase := "workspace"
	if !root.discovered || root.scopeReads[file.target.Scope] > 0 || root.scopeReads[""] > 0 {
		phase = "document"
		if root.discovered {
			root.deferredDomain[uri] = true
		}
	}
	s.mu.Unlock()
	result.publications = append(result.publications, publication{phase: phase, uri: spelling, version: version, diagnostics: diagnostics})
}

func (s *workspaceIndex) processNext(ctx context.Context, result *completed) bool {
	s.mu.Lock()
	job, ok := s.queue.Pop()
	if !ok {
		s.mu.Unlock()
		return false
	}
	read, readScope := false, ""
	var root *indexedRoot
	if file := s.files[job.URI]; file != nil {
		root = s.roots[file.root]
		read, readScope = file.workRead, file.target.Scope
		copy := *file
		copy.workRead = false
		s.files[job.URI] = &copy
	}
	s.mu.Unlock()
	defer s.queue.Complete(job)
	defer func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		if root != nil {
			root.pending--
		}
	}()
	fileCtx, stop := context.WithTimeout(ctx, workspaceAnalysisTimeout)
	defer stop()
	released := false
	releaseRead := func() {
		if !read || released {
			return
		}
		released = true
		s.mu.Lock()
		defer s.mu.Unlock()
		if root != nil {
			root.readPending--
			root.scopeReads[readScope]--
		}
	}
	defer releaseRead()
	s.process(fileCtx, job.URI, read, releaseRead, result)
	return true
}

func (s *workspaceIndex) advance(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, events workspaceEvents) (result completed) {
	started := time.Now()
	s.mu.Lock()
	s.running = true
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.running = false
		if s.closed {
			for _, root := range s.roots {
				if root.scanner != nil {
					_ = root.scanner.Close()
				}
			}
		}
	}()
	result.epoch, result.incremental, result.unchangedInputs = epoch, s, true
	result.failures = map[string]error{}
	result.stats.Scope, result.stats.Phase = "incremental", "indexing"
	result.publications = s.apply(ctx, roots, buffers, override, events)
	s.mu.Lock()
	for path, root := range s.roots {
		if root.passActive {
			result.stats.FullScan = true
			result.stats.AcquisitionRoots = append(result.stats.AcquisitionRoots, path)
		}
	}
	s.mu.Unlock()
	for _, path := range roots {
		s.mu.RLock()
		root := s.roots[path]
		canScan := root != nil && root.scanner != nil && s.queue.Backlog() == 0
		s.mu.RUnlock()
		if !canScan || s.queue.Len() >= indexQueueLimit-indexScanWork {
			continue
		}
		batch, err := root.scanner.Next(ctx, indexScanWork)
		s.mu.Lock()
		result.stats.DiscoveryWork += batch.Visited
		for _, path := range batch.Files {
			if workspace.Owner(roots, path) != root.catalog.Input.Root {
				continue
			}
			uri := source.URI(workspace.FileURI(path))
			if _, included, selectErr := root.catalog.Select(path, false); selectErr == nil && !included {
				continue
			}
			if s.files[uri] == nil && len(s.files) >= indexFileLimit {
				root.err = fmt.Errorf("workspace exceeds 100,000 candidate files")
				_ = root.scanner.Close()
				root.scanner = nil
				root.discovered, root.scanComplete = true, false
				break
			}
			if old := s.files[uri]; old != nil && root.membership.Seen(old.seen) {
				continue
			}
			s.enqueue(uri, true, shared.Background)
		}
		if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			root.err = err
		}
		if batch.Done && root.scanner != nil {
			root.discovered, root.scanComplete = true, batch.Complete
			if !batch.Complete && root.err == nil {
				root.err = fmt.Errorf("workspace discovery incomplete")
			}
			_ = root.scanner.Close()
			root.scanner = nil
			if root.membership.Finish(root.scanPass, batch.Complete) {
				for uri, file := range s.files {
					if file.root == path && root.scanPass.Missing(file.seen) {
						if _, opened := s.open[uri]; !opened {
							result.publications = append(result.publications, s.remove(uri))
						}
					}
				}
			}
		}
		s.mu.Unlock()
	}
	for i := 0; i < indexBatchFiles && ctx.Err() == nil; i++ {
		if !s.processNext(ctx, &result) {
			break
		}
		if time.Since(started) >= 40*time.Millisecond {
			break
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for path, root := range s.roots {
		if root.catalog != nil {
			result.inputs = append(result.inputs, root.catalog.Input)
			if root.discovered && root.readPending == 0 && root.finalDomain {
				root.finalDomain = false
				for uri, file := range s.files {
					_, opened := s.open[uri]
					if file.root == path && (len(file.summary.Documents) > 0 || len(file.schema) > 0 || file.incomplete || opened) {
						s.enqueue(uri, false, shared.Background)
					}
				}
			}
			for uri := range root.deferredDomain {
				file := s.files[uri]
				if file == nil {
					delete(root.deferredDomain, uri)
					continue
				}
				if root.scopeReads[file.target.Scope] == 0 && root.scopeReads[""] == 0 {
					delete(root.deferredDomain, uri)
					s.enqueue(uri, false, shared.Related)
				}
			}
			if root.discovered && root.pending == 0 && root.rescan {
				root.rescan = false
				s.startScan(root)
			}
		}
		if root.err != nil {
			result.failures[path] = root.err
			uri := workspace.FileURI(filepath.Join(path, ".argocd-ls.yaml"))
			if opened, ok := s.open[source.URI(uri)]; ok {
				uri = opened.uri
			}
			result.publications = append(result.publications, publication{uri: uri, diagnostics: []analysis.Diagnostic{inputDiagnostic(uri, root.err)}})
		}
		if root.discovered && root.pending == 0 && !root.rescan {
			root.passActive = false
		}
		if root.scanner != nil || root.pending > 0 {
			result.more = true
		}
	}
	result.stats.QueueDepth, result.stats.SummaryBytes = s.queue.Len(), s.summaryBytes
	result.stats.KnownFiles = len(s.files)
	result.stats.State = "Ready"
	if result.more {
		result.stats.State = "Indexing"
	}
	for _, root := range s.roots {
		if !root.discovered && root.catalog != nil {
			result.stats.State = "Discovering"
		}
		if root.err != nil {
			result.stats.State = "Limited"
		}
	}
	for _, root := range s.roots {
		for _, issues := range root.issues {
			if issues > 0 {
				result.stats.State = "Limited"
			}
		}
	}
	result.stats.Complete = !result.more && result.stats.State == "Ready"
	result.stats.ElapsedMilliseconds = time.Since(started).Milliseconds()
	return result
}

// lookup copies only immutable pointers. Slow reads and parsing happen after
// releasing the state lock so foreground requests do not wait behind discovery.
func (s *workspaceIndex) lookup(uri string) (*indexedFile, *workspace.Catalog, bool) {
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return nil, nil, false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	file := s.files[source.URI(workspace.FileURI(path))]
	root := s.roots[workspace.Owner(s.rootOrder, path)]
	if root == nil {
		return file, nil, false
	}
	if root.catalog != nil && root.configEvent != s.configEvents[root.catalog.Input.Root] {
		return file, nil, false
	}
	complete := file != nil && s.scopeComplete(root, file.target.Scope)
	return file, root.catalog, complete
}

func (s *workspaceIndex) retainsURI(uri string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.files[source.URI(uri)] != nil
}

// A persistent commit can outlive the protocol epoch in which its work began.
// Return its publication obligation to the shared queue when the owner declines
// that batch. Revalidation uses retained facts; it does not reread YAML.
// A removed closed file needs only a clear, which cannot overwrite a newer open
// buffer's local result.
func (s *workspaceIndex) retryPublications(values []publication, buffers map[string]buffer) []publication {
	opened := map[source.URI]bool{}
	for spelling := range buffers {
		if path, err := workspace.PathFromURI(spelling); err == nil {
			opened[source.URI(workspace.FileURI(path))] = true
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var clears []publication
	for _, value := range values {
		path, err := workspace.PathFromURI(value.uri)
		if err != nil {
			continue
		}
		uri := source.URI(workspace.FileURI(path))
		if s.files[uri] != nil {
			s.enqueue(uri, false, shared.Related)
		} else if !opened[uri] && len(value.diagnostics) == 0 {
			clears = append(clears, publication{uri: value.uri, diagnostics: []analysis.Diagnostic{}})
		}
	}
	return clears
}

func sortedPublications(values []publication) []publication {
	last := map[string]publication{}
	for _, p := range values {
		if p.uri != "" {
			last[p.uri] = p
		}
	}
	keys := make([]string, 0, len(last))
	for key := range last {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]publication, 0, len(keys))
	for _, key := range keys {
		out = append(out, last[key])
	}
	return out
}
