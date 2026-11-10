// Package lsp implements a bounded stdio transport and a serial workspace owner.
// Background workers only receive immutable snapshots and publish through epoch checks.
package lsp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/buildinfo"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

// A slow workspace must not consume the independent open-document budget.
const workspaceAnalysisTimeout = 5 * time.Second

type message struct {
	Error  json.RawMessage `json:"error"`
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}
type incoming struct {
	message message
	err     error
}
type buffer struct {
	uri         string
	snapshot    source.Snapshot
	version     int
	revision    source.Revision
	unavailable string
}
type publication struct {
	phase       string
	uri         string
	version     *int
	diagnostics []analysis.Diagnostic
}
type publicationInput struct {
	revision source.Revision
	target   workspace.Target
}
type publishedDiagnostic struct {
	payload []byte
	// Only open, successfully checked inputs acquire this stronger-result
	// proof. It retains neither source text nor a workspace summary.
	input *publicationInput
}
type scopeResult struct {
	checked         map[source.URI]analysis.Result
	projectionStart int
	root, scope     string
	files           []workspace.File
	documents       []analysis.Document
	options         analysis.DomainOptions
}

type pendingQuery struct {
	ctx  context.Context
	stop context.CancelFunc
}

type queryResult struct {
	proof          *readProof
	editGeneration *uint64 // Configuration pin proofs cover the complete workspace.
	trace          string
	ctx            context.Context
	id             json.RawMessage
	epoch          uint64
	answer         any
	code           int
	why            string
}

type completed struct {
	more        bool
	incremental *workspaceIndex
	queryProof  *readProof
	stats       struct {
		Scope                string   `json:"scope"`
		Root                 string   `json:"root,omitempty"`
		Phase                string   `json:"phase,omitempty"`
		ElapsedMilliseconds  int64    `json:"elapsedMilliseconds"`
		AcquisitionRoots     []string `json:"acquisitionRoots"`
		AcquisitionFallbacks int      `json:"acquisitionFallbacks"`
		RepositoryDecodes    int      `json:"repositoryDecodes"`
		DiskReads            int      `json:"diskReads"`
		ParsedFiles          int      `json:"parsedFiles"`
		DomainDocuments      int      `json:"domainDocuments"`
		FullScan             bool     `json:"fullScan"`
		DiscoveryWork        int      `json:"discoveryWork"`
		KnownFiles           int      `json:"knownFiles"`
		QueueDepth           int      `json:"queueDepth"`
		SummaryBytes         int      `json:"summaryBytes"`
		State                string   `json:"state,omitempty"`
		Complete             bool     `json:"complete"`
	}
	documentOnly    bool
	failures        map[string]error
	editGeneration  uint64
	unchangedInputs bool
	inputs          []workspace.Input
	scopes          []scopeResult
	failureURI      string
	epoch           uint64
	publications    []publication
	err             error
}

func Run(in io.Reader, out, errOut io.Writer) int {
	messages := make(chan incoming)
	done := make(chan struct{})
	defer close(done)
	go func() {
		reader := bufio.NewReader(in)
		for {
			m, e := readMessage(reader)
			select {
			case messages <- incoming{m, e}:
			case <-done:
				return
			}
			if e != nil {
				return
			}
		}
	}()
	results := make(chan completed, 1)
	queries := make(chan queryResult, 8)
	requests := map[string]pendingQuery{}
	defer func() {
		for _, request := range requests {
			request.stop()
		}
	}()
	buffers := map[string]buffer{}
	workspaceState := newWorkspaceIndex()
	defer workspaceState.close()
	workEvents := workspaceEvents{files: map[string]bool{}, reset: map[string]bool{}, reconcile: map[string]bool{}}
	diskGenerations := map[string]uint64{}
	roots := []string{}
	published := map[string]publishedDiagnostic{}
	documentPublished := map[string]publication{}
	epoch := uint64(0)
	editGeneration := uint64(0)
	initialized := false
	shutdown := false
	canEdit := false
	canWatch := false
	override := ""
	var cancel, cancelDocument context.CancelFunc
	var latest completed
	var documents completed // Partial facts never become the editable workspace.
	defer func() {
		if cancel != nil {
			cancel()
		}
		if cancelDocument != nil {
			cancelDocument()
		}
	}()
	send := func(value any) error {
		b, e := json.Marshal(value)
		if e != nil {
			return e
		}
		_, e = fmt.Fprintf(out, "Content-Length: %d\r\n\r\n%s", len(b), b)
		return e
	}
	// Keep only the current payload actually sent for each URI. Equal payloads
	// use the new bytes so this state does not retain older result data.
	publish := func(params map[string]any) {
		uri := params["uri"].(string)
		if _, ok := params["x-argocd-analysis-scope"]; !ok {
			params["x-argocd-analysis-scope"] = "workspace"
		}
		payload, err := json.Marshal(params)
		if err != nil {
			return
		}
		next := publishedDiagnostic{payload: payload}
		if params["x-argocd-analysis-scope"] == "workspace" {
			if current, opened := buffers[uri]; opened && current.unavailable == "" {
				file, catalog, _, _ := workspaceState.lookup(uri)
				if catalog != nil && file != nil && file.err == nil && !file.incomplete && !file.invalidated && !file.dirty && file.revision == current.revision {
					target := file.target
					target.Complete = false
					next.input = &publicationInput{current.revision, target}
				}
			}
		}
		if bytes.Equal(published[uri].payload, payload) {
			published[uri] = next
			return
		}
		if send(map[string]any{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": json.RawMessage(payload)}) == nil {
			published[uri] = next
		}
	}
	reply := func(id json.RawMessage, value any) {
		_ = send(map[string]any{"jsonrpc": "2.0", "id": id, "result": value})
	}
	reject := func(id json.RawMessage, code int, why string) {
		if len(id) > 0 {
			_ = send(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": why}})
		}
	}
	var active, pending, documentActive, documentPending bool
	debounce := time.NewTimer(time.Hour)
	debounce.Stop()
	defer debounce.Stop()
	var ready <-chan time.Time
	launch := func(inputChanged bool) {
		if inputChanged {
			editGeneration++
		}
		epoch++
		if inputChanged {
			documentPending = true
			if cancelDocument != nil {
				cancelDocument()
			}
		}
		pending = true
		debounce.Reset(120 * time.Millisecond)
		ready = debounce.C
	}
	acceptBuffer := func(uri, text string) (source.Snapshot, error) {
		if old, open := workspaceState.documents.Snapshot(source.URI(uri)); open {
			return workspaceState.documents.Change(source.URI(uri), old.Revision(), []source.Change{{Range: source.Span{Start: 0, End: len(old.Text())}, Text: text}})
		}
		return workspaceState.documents.Open(source.URI(uri), text)
	}
	recordFileEvent := func(uri string) {
		path, err := workspace.PathFromURI(uri)
		root := workspace.Owner(roots, path)
		if err != nil || root == "" {
			return
		}
		workspaceState.documents.Invalidate(source.URI(uri))
		workspaceState.withdraw(uri)
		if filepath.Base(path) == ".argocd-ls.yaml" {
			workEvents.reset[root] = true
		}
		if workEvents.files[uri] || len(workEvents.files) < indexQueueLimit {
			workEvents.files[uri] = true
		} else {
			// Coalescing overflow schedules a bounded full membership pass. The
			// event's already withdrawn facts cannot be served in the meantime.
			workEvents.reconcile[root] = true
		}
	}
	fitsBuffer := func(uri string, size int) bool {
		if size < 0 || size > workspace.MaxFileText {
			return false
		}
		total := size
		for other, b := range buffers {
			if other != uri {
				total += len(b.snapshot.Text())
				if total > workspace.MaxRetainedText {
					return false
				}
			}
		}
		return total <= workspace.MaxRetainedText
	}
	refuseBuffer := func(uri string, version int, revision source.Revision, reason string) {
		workspaceState.documents.Close(source.URI(uri))
		buffers[uri] = buffer{uri: uri, version: version, revision: revision, unavailable: reason}
		recordFileEvent(uri)
		// Release current owner facts immediately. Already-running consumers are
		// cancelled and still must pass the independent epoch delivery guard.
		for _, request := range requests {
			request.stop()
		}
		launch(true)
		publish(map[string]any{"uri": uri, "version": version, "diagnostics": []analysis.Diagnostic{{URI: uri, Code: "INPUT_BUDGET_EXCEEDED", Source: "argocd-ls", Severity: 1, Message: reason}}})
	}
	startAnalysis := func() {
		if shutdown {
			return
		}
		copyBuffers := map[string]buffer{}
		for uri, b := range buffers {
			copyBuffers[uri] = b
		}
		copyRoots := append([]string(nil), roots...)
		current, selected := epoch, override
		if documentPending && !documentActive {
			documentPending, documentActive = false, true
			ctx, stop := context.WithTimeout(context.Background(), 5*time.Second)
			cancelDocument = stop
			previous := documents
			go func() {
				defer stop()
				local := analyzeDocuments(ctx, current, copyRoots, copyBuffers, selected, previous)
				select {
				case results <- local:
				case <-done:
				}
			}()
		}
		if active || !pending {
			return
		}
		pending, active = false, true
		ctx, stop := context.WithCancel(context.Background())
		cancel = stop
		events := workEvents
		workEvents = workspaceEvents{files: map[string]bool{}, reset: map[string]bool{}, reconcile: map[string]bool{}}
		go func() {
			defer stop()
			result := workspaceState.advance(ctx, current, copyRoots, copyBuffers, selected, events)
			select {
			case results <- result:
			case <-done:
			}
		}()
	}

	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ready:
			ready = nil
			startAnalysis()
		case <-ticker.C:
			if initialized && !shutdown {
				for _, root := range roots {
					diskGenerations[root]++
					workEvents.reconcile[root] = true
				}
				launch(false)
			}
		case q := <-queries:
			if q.trace != "" {
				fmt.Fprint(errOut, q.trace)
			}
			key := string(q.id)
			request, pending := requests[key]
			if !pending || request.ctx != q.ctx {
				continue
			}
			request.stop()
			delete(requests, key)
			if shutdown || (q.epoch != epoch && q.proof == nil) || (q.editGeneration != nil && *q.editGeneration != editGeneration) || (q.proof != nil && !q.proof.valid(buffers, roots, override, diskGenerations)) {
				reject(q.id, -32801, "analysis changed")
				continue
			}
			if q.code != 0 {
				reject(q.id, q.code, q.why)
			} else {
				reply(q.id, q.answer)
			}
		case result := <-results:
			if result.documentOnly {
				documentActive = false
			} else {
				active = false
			}
			if result.more {
				pending = true
			}
			if (pending && !active) || (documentPending && !documentActive) {
				debounce.Reset(time.Millisecond)
				ready = debounce.C
			}
			if !result.documentOnly && !result.unchangedInputs {
				// The index commit already happened, even if a newer event makes
				// this batch's diagnostic delivery stale.
				editGeneration++
			}

			if result.epoch != epoch || shutdown {
				if !shutdown && !result.documentOnly && len(result.publications) > 0 {
					for _, clear := range workspaceState.retryPublications(result.publications, buffers) {
						publish(map[string]any{"uri": clear.uri, "diagnostics": clear.diagnostics})
						delete(published, clear.uri)
					}
					pending = true
					debounce.Reset(time.Millisecond)
					ready = debounce.C
				}
				if os.Getenv("ARGOCD_LS_TRACE") == "1" {
					trace, _ := json.Marshal(result.stats)
					fmt.Fprintln(errOut, "argocd-analysis-discarded", string(trace))
				}
				continue
			}
			if result.documentOnly {
				if os.Getenv("ARGOCD_LS_TRACE") == "1" {
					trace, _ := json.Marshal(result.stats)
					fmt.Fprintln(errOut, "argocd-document-analysis", string(trace))
				}
				nextPublished := map[string]publication{}
				targets := map[string]workspace.Target{}
				for _, scope := range result.scopes {
					for _, file := range scope.files {
						target := file.Target
						target.Complete = false
						targets[string(file.Snapshot.URI())] = target
					}
				}
				for _, p := range result.publications {
					current, opened := buffers[p.uri]
					stronger := published[p.uri].input
					workspaceDelivered := opened && stronger != nil && stronger.revision == current.revision && stronger.target == targets[p.uri]
					// Independent workers may finish in either order. A late
					// Schema-only result must not clear already delivered domain
					// diagnostics for this exact accepted input and configuration.
					if previous, ok := documentPublished[p.uri]; (!ok || !reflect.DeepEqual(previous, p)) && !workspaceDelivered {
						publish(map[string]any{"uri": p.uri, "version": p.version, "diagnostics": p.diagnostics, "x-argocd-analysis-scope": "document"})
					}
					nextPublished[p.uri] = p
				}
				documentPublished = nextPublished
				documents = result
				continue
			}
			result.editGeneration = editGeneration
			latest = result
			if os.Getenv("ARGOCD_LS_TRACE") == "1" {
				trace, _ := json.Marshal(result.stats)
				fmt.Fprintln(errOut, "argocd-analysis", string(trace))
			}
			for _, publication := range sortedPublications(recoverDocumentPublications(result, documents, roots, buffers)) {
				if publication.diagnostics == nil {
					publication.diagnostics = []analysis.Diagnostic{}
				}
				if b, ok := buffers[publication.uri]; ok {
					version := b.version
					publication.version = &version
				}
				phase := publication.phase
				if phase == "" {
					phase = "workspace"
				}
				publish(map[string]any{"uri": publication.uri, "version": publication.version, "diagnostics": publication.diagnostics, "x-argocd-analysis-scope": phase})
				if publication.version == nil && len(publication.diagnostics) == 0 && !workspaceState.retainsURI(publication.uri) {
					delete(published, publication.uri)
				}
			}
			continue
		case event := <-messages:
			if event.err != nil {
				if event.err != io.EOF {
					fmt.Fprintln(errOut, event.err)
				}
				if shutdown {
					return 0
				}
				fmt.Fprintln(errOut, "LSP ended before shutdown")
				return 2
			}
			m := event.message
			if m.Method == "" && string(m.ID) == `"argocd-watch-unregister"` {
				continue
			}
			if m.Method == "" && string(m.ID) == `"argocd-watch-registration"` {
				if len(m.Error) > 0 && string(m.Error) != "null" {
					fmt.Fprintln(errOut, "file watcher registration failed; using 30-second reconciliation")
				}
				continue
			}
			if m.Method == "exit" {
				if shutdown {
					return 0
				}
				return 2
			}
			if m.Method == "initialize" {
				if initialized {
					reject(m.ID, -32600, "already initialized")
					continue
				}
				var p struct {
					Capabilities struct {
						Workspace struct {
							DidChangeWatchedFiles struct {
								DynamicRegistration bool `json:"dynamicRegistration"`
							} `json:"didChangeWatchedFiles"`
							WorkspaceEdit struct {
								DocumentChanges bool `json:"documentChanges"`
							} `json:"workspaceEdit"`
						} `json:"workspace"`
					} `json:"capabilities"`
					RootURI          string `json:"rootUri"`
					WorkspaceFolders []struct {
						URI string `json:"uri"`
					} `json:"workspaceFolders"`
					InitializationOptions struct {
						TargetVersion string `json:"targetVersion"`
					} `json:"initializationOptions"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					reject(m.ID, -32602, "invalid initialize params")
					continue
				}
				roots = nil
				if len(p.WorkspaceFolders) > 0 {
					for _, f := range p.WorkspaceFolders {
						if path, e := workspace.PathFromURI(f.URI); e == nil {
							roots = append(roots, path)
						}
					}
				} else if path, e := workspace.PathFromURI(p.RootURI); e == nil {
					roots = append(roots, path)
				}
				if len(roots) == 0 {
					reject(m.ID, -32602, "a local workspace root is required")
					continue
				}
				override = p.InitializationOptions.TargetVersion
				initialized = true
				canEdit = p.Capabilities.Workspace.WorkspaceEdit.DocumentChanges
				canWatch = p.Capabilities.Workspace.DidChangeWatchedFiles.DynamicRegistration
				capabilities := map[string]any{
					"textDocumentSync":       map[string]any{"openClose": true, "change": 2, "save": true},
					"hoverProvider":          true,
					"definitionProvider":     true,
					"referencesProvider":     true,
					"documentSymbolProvider": true,
					"completionProvider":     map[string]any{},
					"workspace": map[string]any{
						"workspaceFolders": map[string]any{"supported": true, "changeNotifications": true},
					},
				}
				if canEdit {
					capabilities["codeActionProvider"] = map[string]any{"resolveProvider": true}
				}
				reply(m.ID, map[string]any{
					"capabilities": capabilities,
					"serverInfo":   map[string]any{"name": "argocd-ls", "version": buildinfo.ServerVersion},
				})
				continue
			}
			if !initialized {
				reject(m.ID, -32002, "server not initialized")
				continue
			}
			if shutdown {
				reject(m.ID, -32600, "server is shut down")
				continue
			}
			switch m.Method {
			case "initialized":
				if canWatch {
					_ = send(map[string]any{"jsonrpc": "2.0", "id": "argocd-watch-registration", "method": "client/registerCapability", "params": map[string]any{"registrations": []any{map[string]any{"id": "argocd-workspace-files", "method": "workspace/didChangeWatchedFiles", "registerOptions": map[string]any{"watchers": []any{map[string]any{"globPattern": "**/*", "kind": 7}}}}}}})
				}
				launch(true)
			case "shutdown":
				if canWatch {
					_ = send(map[string]any{"jsonrpc": "2.0", "id": "argocd-watch-unregister", "method": "client/unregisterCapability", "params": map[string]any{"unregisterations": []any{map[string]any{"id": "argocd-workspace-files", "method": "workspace/didChangeWatchedFiles"}}}})
				}
				shutdown = true
				if cancelDocument != nil {
					cancelDocument()
				}
				for key, request := range requests {
					request.stop()
					reject(json.RawMessage(key), -32800, "server shutting down")
					delete(requests, key)
				}
				epoch++
				if cancel != nil {
					cancel()
				}
				reply(m.ID, nil)
			case "textDocument/didOpen":
				var p struct {
					TextDocument struct {
						URI     string `json:"uri"`
						Version int    `json:"version"`
						Text    string `json:"text"`
					} `json:"textDocument"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				if _, e := workspace.PathFromURI(p.TextDocument.URI); e != nil {
					continue
				}
				if old, ok := buffers[p.TextDocument.URI]; ok && p.TextDocument.Version <= old.version {
					continue
				}
				if len(p.TextDocument.URI) > 4096 {
					fmt.Fprintln(errOut, "open URI exceeds input budget")
					continue
				}
				if _, exists := buffers[p.TextDocument.URI]; !exists && len(buffers) >= 2048 {
					fmt.Fprintln(errOut, "open document entry budget exhausted")
					continue
				}
				revision := source.Revision(1)
				if old, ok := buffers[p.TextDocument.URI]; ok {
					revision = old.revision + 1
				}
				if !fitsBuffer(p.TextDocument.URI, len(p.TextDocument.Text)) {
					refuseBuffer(p.TextDocument.URI, p.TextDocument.Version, revision, "open buffer text exceeds the 5 MiB file or 128 MiB retained-text budget")
					continue
				}
				s, e := acceptBuffer(p.TextDocument.URI, p.TextDocument.Text)
				if e != nil {
					fmt.Fprintln(errOut, e)
					continue
				}
				buffers[p.TextDocument.URI] = buffer{snapshot: s, version: p.TextDocument.Version, revision: s.Revision()}
				recordFileEvent(p.TextDocument.URI)
				launch(true)
			case "textDocument/didChange":
				var p struct {
					TextDocument struct {
						URI     string `json:"uri"`
						Version int    `json:"version"`
					} `json:"textDocument"`
					ContentChanges []struct {
						Range *analysis.Range `json:"range"`
						Text  string          `json:"text"`
					} `json:"contentChanges"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				old, ok := buffers[p.TextDocument.URI]
				if !ok || p.TextDocument.Version <= old.version {
					continue
				}
				snapshot := old.snapshot
				valid := true
				unavailable := old.unavailable != ""
				budgetFailure := ""
				nextRevision := old.revision + 1
				for _, change := range p.ContentChanges {
					if unavailable {
						if change.Range != nil {
							budgetFailure = "buffer unavailable after budget refusal; send a full replacement or close/reopen"
							valid = false
							break
						}
						if !fitsBuffer(p.TextDocument.URI, len(change.Text)) {
							budgetFailure = "replacement exceeds retained text budget"
							valid = false
							break
						}
						next, e := source.New(source.URI(p.TextDocument.URI), nextRevision, change.Text)
						if e != nil {
							valid = false
							break
						}
						snapshot = next
						nextRevision++
						unavailable = false
						continue
					}
					span := source.Span{Start: 0, End: len(snapshot.Text())}
					if change.Range != nil {
						a, e1 := snapshot.Offset(source.Position{Line: change.Range.Start.Line, UTF16Column: change.Range.Start.Character})
						b, e2 := snapshot.Offset(source.Position{Line: change.Range.End.Line, UTF16Column: change.Range.End.Character})
						if e1 != nil || e2 != nil {
							valid = false
							break
						}
						span = source.Span{Start: a, End: b}
					}
					if !fitsBuffer(p.TextDocument.URI, len(snapshot.Text())-(span.End-span.Start)+len(change.Text)) {
						budgetFailure = "replacement exceeds the 5 MiB file or 128 MiB retained-text budget"
						valid = false
						break
					}
					next, e := snapshot.Apply(nextRevision, []source.Change{{Range: span, Text: change.Text}})
					if e != nil {
						valid = false
						break
					}
					snapshot = next
					nextRevision++
				}
				if unavailable && budgetFailure == "" {
					budgetFailure = "buffer unavailable after budget refusal; send a full replacement or close/reopen"
				}
				if budgetFailure != "" {
					refuseBuffer(p.TextDocument.URI, p.TextDocument.Version, nextRevision, budgetFailure)
					continue
				}
				if valid {
					accepted, err := acceptBuffer(p.TextDocument.URI, snapshot.Text())
					if err != nil {
						refuseBuffer(p.TextDocument.URI, p.TextDocument.Version, nextRevision, err.Error())
						continue
					}
					snapshot = accepted
					recordFileEvent(p.TextDocument.URI)
					buffers[p.TextDocument.URI] = buffer{snapshot: snapshot, version: p.TextDocument.Version, revision: snapshot.Revision()}
					launch(true)
				} else {
					fmt.Fprintln(errOut, "rejected invalid incremental edit")
				}
			case "textDocument/didClose":
				var p struct {
					TextDocument struct {
						URI string `json:"uri"`
					} `json:"textDocument"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					_, existed := buffers[p.TextDocument.URI]
					workspaceState.documents.Close(source.URI(p.TextDocument.URI))
					recordFileEvent(p.TextDocument.URI)
					delete(buffers, p.TextDocument.URI)
					delete(documentPublished, p.TextDocument.URI)
					if existed {
						publish(map[string]any{"uri": p.TextDocument.URI, "diagnostics": []analysis.Diagnostic{}, "x-argocd-analysis-scope": "document"})
					}
					launch(existed)
				}
			case "textDocument/didSave", "workspace/didChangeWatchedFiles":
				var event struct {
					TextDocument struct {
						URI string `json:"uri"`
					} `json:"textDocument"`
					Changes []struct {
						URI string `json:"uri"`
					} `json:"changes"`
				}
				_ = json.Unmarshal(m.Params, &event)
				changedRoots := map[string]bool{}
				if event.TextDocument.URI != "" {
					p, _ := workspace.PathFromURI(event.TextDocument.URI)
					changedRoots[workspace.Owner(roots, p)] = true
					recordFileEvent(event.TextDocument.URI)
				}
				for _, change := range event.Changes {
					p, _ := workspace.PathFromURI(change.URI)
					changedRoots[workspace.Owner(roots, p)] = true
					recordFileEvent(change.URI)
				}
				if len(changedRoots) == 0 {
					for _, root := range roots {
						changedRoots[root] = true
						workEvents.reconcile[root] = true
					}
				}
				for root := range changedRoots {
					diskGenerations[root]++
				}
				launch(false)
			case "workspace/didChangeWorkspaceFolders":
				previousRoots := append([]string(nil), roots...)
				var p struct {
					Event struct {
						Added []struct {
							URI string `json:"uri"`
						} `json:"added"`
						Removed []struct {
							URI string `json:"uri"`
						} `json:"removed"`
					} `json:"event"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				for _, f := range p.Event.Removed {
					path, e := workspace.PathFromURI(f.URI)
					if e != nil {
						continue
					}
					remaining := roots[:0]
					for _, r := range roots {
						if r != path {
							remaining = append(remaining, r)
						}
					}
					roots = remaining
				}
				for _, f := range p.Event.Added {
					path, e := workspace.PathFromURI(f.URI)
					if e != nil {
						continue
					}
					// A remove/add pair may be coalesced before a worker runs.
					// Re-establish its membership and publications even then.
					workEvents.reset[path] = true
					exists := false
					for _, r := range roots {
						if r == path {
							exists = true
						}
					}
					if !exists {
						roots = append(roots, path)
					}
				}
				sort.Slice(roots, func(i, j int) bool {
					if len(roots[i]) != len(roots[j]) {
						return len(roots[i]) > len(roots[j])
					}
					return roots[i] < roots[j]
				})
				for uri := range published {
					path, _ := workspace.PathFromURI(uri)
					if workspace.Owner(roots, path) == "" {
						publish(map[string]any{"uri": uri, "diagnostics": []analysis.Diagnostic{}})
						delete(published, uri)
					}
				}
				launch(!reflect.DeepEqual(previousRoots, roots))
			case "workspace/didChangeConfiguration":
				var p struct {
					Settings struct {
						TargetVersion string `json:"targetVersion"`
					} `json:"settings"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					changed := override != p.Settings.TargetVersion
					override = p.Settings.TargetVersion
					if changed {
						for _, root := range roots {
							workEvents.reset[root] = true
						}
					}
					launch(changed)
				}

			case "textDocument/codeAction", "codeAction/resolve", "textDocument/definition", "textDocument/references", "textDocument/documentSymbol", "textDocument/hover", "textDocument/completion":
				key := string(m.ID)
				if _, exists := requests[key]; exists {
					reject(m.ID, -32600, "duplicate request id")
					continue
				}
				ctx, stop := context.WithTimeout(context.Background(), 5*time.Second)
				requests[key] = pendingQuery{ctx, stop}
				input, localInput, current := latest, documents, epoch
				selected := override
				var queryRoots []string
				var copied map[string]buffer
				queryDisk := make(map[string]uint64, len(diskGenerations))
				{
					queryRoots = append([]string{}, roots...)
					copied = map[string]buffer{}
					for uri, b := range buffers {
						copied[uri] = b
					}
					for root, generation := range diskGenerations {
						queryDisk[root] = generation
					}
				}
				editable := canEdit
				go func() {
					var proof *readProof
					var editProof *uint64
					var trace bytes.Buffer
					var answer any
					var code int
					var why string
					if m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve" {
						input = workspaceState.query(ctx, m, input, localInput, copied, queryRoots, selected, current)
						if os.Getenv("ARGOCD_LS_TRACE") == "1" {
							for i := range input.scopes {
								input.scopes[i].options.Trace = &trace
							}
						}
						answer, code, why = fixRequest(ctx, m, input, copied, current, editable, queryRoots, selected)
						if code == 0 {
							proof = newReadProof(m, input, copied, queryRoots, selected, queryDisk)
							if proof != nil && localFixAnswer(m, answer) {
								proof.localize(queryURI(m))
							}
							if path, _ := workspace.PathFromURI(queryURI(m)); proof == nil && filepath.Base(path) == ".argocd-ls.yaml" {
								generation := input.editGeneration
								editProof = &generation
							}
						}
					} else {
						fresh := workspaceState.query(ctx, m, input, localInput, copied, queryRoots, selected, current)
						answer, code, why = readRequest(ctx, m, fresh, current, queryRoots)
						if code == 0 {
							// The proof must describe the very scopes used by this
							// answer, including configuration changes refreshed above.
							proof = newReadProof(m, fresh, copied, queryRoots, selected, queryDisk)
						}
						traceReadRequest(&trace, m, fresh, proof)
					}
					if ctx.Err() != nil {
						code, why = -32800, "request cancelled"
					}
					select {
					case queries <- queryResult{proof: proof, editGeneration: editProof, trace: trace.String(), ctx: ctx, id: m.ID, epoch: current, answer: answer, code: code, why: why}:
					case <-done:
					}
				}()
			case "$/cancelRequest":
				var p struct {
					ID json.RawMessage `json:"id"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					key := string(p.ID)
					if request, ok := requests[key]; ok {
						request.stop()
						delete(requests, key)
						reject(p.ID, -32800, "request cancelled")
					}
				}
			case "$/setTrace":
			default:
				reject(m.ID, -32601, "method is not implemented")
			}
		}
	}
}

// A failed scan must withdraw cross-file claims without withdrawing independently
// checked current text. The merged view is for publication only, never edits.
func recoverDocumentPublications(result, documents completed, roots []string, buffers map[string]buffer) []publication {
	if documents.err != nil {
		return result.publications
	}
	checked := map[string]source.Revision{}
	for _, scope := range documents.scopes {
		for _, file := range scope.files {
			checked[string(file.Snapshot.URI())] = file.Snapshot.Revision()
		}
	}
	publications := append([]publication(nil), result.publications...)
	for i, failed := range publications {
		unavailable := false
		for _, diagnostic := range failed.diagnostics {
			unavailable = unavailable || diagnostic.Code == "ANALYSIS_INCOMPLETE" || diagnostic.Code == "INPUT_BUDGET_EXCEEDED"
		}
		if !unavailable {
			continue
		}
		for _, local := range documents.publications {
			if local.uri != failed.uri {
				continue
			}
			current, open := buffers[local.uri]
			if !open || current.unavailable != "" || checked[local.uri] != current.snapshot.Revision() {
				continue
			}
			path, err := workspace.PathFromURI(local.uri)
			if err != nil || workspace.Owner(roots, path) == "" {
				continue
			}
			merged := append([]analysis.Diagnostic(nil), local.diagnostics...)
			for _, diagnostic := range failed.diagnostics {
				duplicate := false
				for _, existing := range merged {
					duplicate = duplicate || reflect.DeepEqual(existing, diagnostic)
				}
				if !duplicate {
					merged = append(merged, diagnostic)
				}
			}
			publications[i].diagnostics = merged
		}
	}
	return publications
}

func analyzeRoots(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed, rescanRoots map[string]bool) completed {
	return analyze(ctx, epoch, roots, buffers, override, previous, rescanRoots, false)
}

func analyzeDocuments(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed) completed {
	selected := map[string]bool{}
	for uri := range buffers {
		file, err := workspace.PathFromURI(uri)
		if err == nil {
			if root := workspace.Owner(roots, file); root != "" {
				selected[root] = true
			}
		}
	}
	activeRoots := []string{}
	for _, root := range roots {
		if selected[root] {
			activeRoots = append(activeRoots, root)
		}
	}
	return analyze(ctx, epoch, activeRoots, buffers, override, previous, selected, true)
}

func analyze(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed, rescanRoots map[string]bool, documentOnly bool) (result completed) {
	started := time.Now()
	defer func() { result.stats.ElapsedMilliseconds = time.Since(started).Milliseconds() }()
	result = completed{epoch: epoch, failures: map[string]error{}, documentOnly: documentOnly}
	result.stats.FullScan = len(rescanRoots) > 0
	result.stats.Scope = "diagnostic-pass"
	if documentOnly {
		result.stats.Scope = "document-pass"
		result.stats.FullScan = false
	}
	if ctx.Err() != nil {
		result.err = ctx.Err()
		return result
	}
	oldPublications := map[string]publication{}
	if previous.err == nil {
		for _, p := range previous.publications {
			oldPublications[p.uri] = p
		}
	}
	seen := map[string]bool{}
	analyzer := retainedAnalyzer(previous, nil)
	remaining := workspace.MaxRetainedText
	openFiles := map[string]source.Snapshot{}
	for uri, b := range buffers {
		if b.unavailable != "" {
			file, _ := workspace.PathFromURI(uri)
			root := workspace.Owner(roots, file)
			result.failures[root] = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: b.unavailable}
			v := b.version
			result.publications = append(result.publications, publication{"", uri, &v, []analysis.Diagnostic{{URI: uri, Code: "INPUT_BUDGET_EXCEEDED", Source: "argocd-ls", Severity: 1, Message: b.unavailable}}})
			continue
		}
		remaining -= len(b.snapshot.Text())
		if remaining < 0 {
			result.err = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: "open buffer text exceeds 128 MiB"}
			result.failureURI = uri
			return result
		}
		if file, e := workspace.PathFromURI(uri); e == nil {
			openFiles[file] = b.snapshot
		}
	}
	roots = append([]string(nil), roots...)
	sort.Slice(roots, func(i, j int) bool {
		if len(roots[i]) != len(roots[j]) {
			return len(roots[i]) > len(roots[j])
		}
		return roots[i] < roots[j]
	})
rootLoop:
	for _, root := range roots {
		if result.failures[root] != nil {
			continue
		}
		checkpoint := rootCheckpoint{len(result.inputs), len(result.scopes), len(result.publications)}
		result.stats.Root, result.stats.Phase = root, "acquisition"
		configURI := workspace.FileURI(filepath.Join(root, ".argocd-ls.yaml"))
		var config *string
		configPath, _ := workspace.PathFromURI(configURI)
		if snapshot, ok := openFiles[configPath]; ok {
			// Filesystem identity selects authority; replies retain the client's URI.
			configURI = string(snapshot.URI())
			t := snapshot.Text()
			config = &t
		}

		var input workspace.Input
		var e error
		reusedInput := false
		beforeRemaining := remaining
		if !documentOnly && !rescanRoots[root] {
			for _, prior := range previous.inputs {
				if prior.Root == root {
					input, reusedInput = workspace.RefreshBuffers(ctx, prior, openFiles, roots, &remaining)
					break
				}
			}
		}
		if !reusedInput {
			remaining = beforeRemaining
			if documentOnly {
				input, e = workspace.LoadOpenBuffers(ctx, root, override, config, openFiles, roots, &remaining)
			} else {
				result.stats.AcquisitionRoots = append(result.stats.AcquisitionRoots, root)
				if !rescanRoots[root] {
					result.stats.AcquisitionFallbacks++
				}
				input, e = workspace.LoadBuffers(ctx, root, override, config, openFiles, roots, &remaining)
			}
			result.stats.DiskReads += input.AcquisitionReads
			input.AcquisitionReads = 0
		}
		if e != nil {
			remaining = beforeRemaining
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
			result.failRoot(checkpoint, root, configURI, buffers, e)
			continue
		}
		result.stats.RepositoryDecodes += input.RepositoryDecodes
		input.RepositoryDecodes = 0
		result.inputs = append(result.inputs, input)
		for _, snapshot := range input.Skipped {
			uri := string(snapshot.URI())
			if b, ok := buffers[uri]; ok {
				v := b.version
				result.publications = append(result.publications, publication{"", uri, &v, []analysis.Diagnostic{{URI: uri, Code: "HELM_UNRENDERED", Source: "argocd-ls", Severity: 3, Message: "Helm template is not rendered; static Argo CD checks are incomplete. Run the explicit render-check task and inspect its generated YAML diagnostics."}}})
			}
		}

		fileIndices := map[source.URI]int{}
		for i, f := range input.Files {
			fileIndices[f.Snapshot.URI()] = i
		}

		scopeFiles := map[string][]workspace.File{}
		for _, target := range input.Targets {
			scopeFiles[target.Scope] = nil
		}
		for _, f := range input.Files {
			uri := string(f.Snapshot.URI())
			if !seen[uri] {
				seen[uri] = true
				scopeFiles[f.Target.Scope] = append(scopeFiles[f.Target.Scope], f)
			}
		}
		reusableRoot := false
		if previous.err == nil {
			for _, oldInput := range previous.inputs {
				if oldInput.Root == input.Root && oldInput.Complete == input.Complete && reflect.DeepEqual(oldInput.Config, input.Config) && reflect.DeepEqual(oldInput.LocalPaths, input.LocalPaths) {
					reusableRoot = true
					break
				}
			}
		}
		scopeNames := make([]string, 0, len(scopeFiles))
		for scope := range scopeFiles {
			scopeNames = append(scopeNames, scope)
		}
		sort.Strings(scopeNames)
		for _, scope := range scopeNames {
			result.stats.Phase = "schema"
			currentFiles := scopeFiles[scope]
			sort.Slice(currentFiles, func(i, j int) bool { return currentFiles[i].Snapshot.URI() < currentFiles[j].Snapshot.URI() })
			projectionStart := analyzer.ProjectionNodes()
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
			reused := false
			if reusableRoot {
				for _, old := range previous.scopes {
					if old.root == input.Root && old.scope == scope && old.projectionStart == projectionStart && sameScopeFiles(old.files, currentFiles) && analyzer.ReuseProjections(old.documents) {
						// This is the single latest-result cache. Fresh acquisition
						// proved identical authority; no disk metadata shortcut.
						// old is a value copy: keep earlier readers immutable while
						// sharing this acquisition's equal whole-root path facts.
						old.options.LocalPaths = input.LocalPaths
						result.scopes = append(result.scopes, old)
						// Keep the completed input and reused documents on the same
						// immutable snapshots after exact fresh-text comparison.
						for _, f := range old.files {
							input.Files[fileIndices[f.Snapshot.URI()]].Snapshot = f.Snapshot
						}
						for _, f := range currentFiles {
							uri := string(f.Snapshot.URI())
							p := oldPublications[uri]
							p.version = nil
							if b, ok := buffers[uri]; ok {
								v := b.version
								p.version = &v
							}
							result.publications = append(result.publications, p)
						}
						reused = true
						break
					}
				}
			}
			if reused {
				continue
			}
			docs := []analysis.Document{}
			checks := map[source.URI]analysis.Result{}
			var priorScope *scopeResult
			if reusableRoot {
				for i := range previous.scopes {
					old := &previous.scopes[i]
					if old.root == root && old.scope == scope {
						priorScope = old
						break
					}
				}
			}
			priorFiles := map[source.URI]workspace.File{}
			if priorScope != nil {
				for _, f := range priorScope.files {
					priorFiles[f.Snapshot.URI()] = f
				}
			}
			changed := map[string]bool{}
			projectChanged := false
			if priorScope != nil {
				currentMembers := map[source.URI]bool{}
				for _, f := range currentFiles {
					currentMembers[f.Snapshot.URI()] = true
				}
				for _, f := range priorScope.files {
					if !currentMembers[f.Snapshot.URI()] {
						projectChanged = true
					}
				}
			}
			incomplete := false
			firstPublication := len(result.publications)
			for _, f := range currentFiles {
				uri := string(f.Snapshot.URI())
				if ctx.Err() != nil {
					result.err = ctx.Err()
					result.failureURI = uri
					return result
				}
				var checked analysis.Result
				var e error
				reusedFile := false
				if priorScope != nil {
					if oldFile, ok := priorFiles[f.Snapshot.URI()]; ok && sameFile(oldFile, f) {
						if old, ok := priorScope.checked[f.Snapshot.URI()]; ok && analyzer.ReuseProjections(old.Documents) {
							checked = old
							reusedFile = true
						}
					}
				}

				if !reusedFile {
					checked, e = analyzer.Analyze(ctx, f.Snapshot, f.Target.Version)
					result.stats.ParsedFiles++
					changed[uri] = true
				}
				checks[f.Snapshot.URI()] = checked
				if changed[uri] {
					for _, doc := range checked.Documents {
						if doc.Kind == "AppProject" {
							projectChanged = true
						}
					}
					if priorScope != nil {
						for _, doc := range priorScope.documents {
							if string(doc.Snapshot.URI()) == uri && doc.Kind == "AppProject" {
								projectChanged = true
							}
						}
					}
				}
				if e != nil {
					if ctx.Err() != nil {
						result.err = ctx.Err()
						result.failureURI = uri
						return result
					}
					// A parser/input failure invalidates this root's authority,
					// not the other roots. Discard all partial facts and charges
					// from this root before continuing with independent inputs.
					result.failRoot(checkpoint, root, uri, buffers, e)
					remaining = beforeRemaining
					for _, f := range input.Files {
						delete(seen, string(f.Snapshot.URI()))
					}
					analyzer = retainedAnalyzer(previous, result.scopes)
					continue rootLoop
				}
				incomplete = incomplete || checked.Incomplete
				docs = append(docs, checked.Documents...)
				var version *int
				if b, ok := buffers[uri]; ok {
					v := b.version
					version = &v
				}
				result.publications = append(result.publications, publication{"", uri, version, checked.Diagnostics})
			}
			options := analysis.DomainOptions{LocalPaths: input.LocalPaths, ControlPlaneNamespace: input.Config.ControlPlaneNamespace, ReferencePolicy: input.Config.ReferencePolicy, Complete: input.Complete && !incomplete && !strings.HasPrefix(scope, "isolated:")}
			result.scopes = append(result.scopes, scopeResult{checked: checks, projectionStart: projectionStart, root: input.Root, scope: scope, files: currentFiles, documents: docs, options: options})
			if documentOnly {
				continue // No reference or repository claims from a partial index.
			}
			result.stats.Phase = "domain"
			domainOptions := options
			domainOptions.Context = ctx
			domainDocs := docs
			if priorScope != nil && priorScope.options.Complete && options.Complete && !projectChanged && len(priorScope.files) == len(currentFiles) {
				domainDocs = nil
				for _, doc := range docs {
					if changed[string(doc.Snapshot.URI())] || doc.Kind == "AppProject" {
						domainDocs = append(domainDocs, doc)
					}
				}
				for i := firstPublication; i < len(result.publications); i++ {
					uri := result.publications[i].uri
					if !changed[uri] {
						result.publications[i].diagnostics = oldPublications[uri].diagnostics
					}
				}
			}
			result.stats.DomainDocuments += len(domainDocs)
			for _, d := range analysis.Domain(domainDocs, domainOptions) {
				for i := firstPublication; i < len(result.publications); i++ {
					if result.publications[i].uri == d.URI {
						result.publications[i].diagnostics = append(result.publications[i].diagnostics, d)
						break
					}
				}
			}
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
		}
	}
	// Open skipped documents still receive an explicit empty replacement.
	for uri, b := range buffers {
		path, err := workspace.PathFromURI(uri)
		if err != nil || workspace.Owner(roots, path) == "" {
			continue
		}
		found := false
		for _, p := range result.publications {
			if p.uri == uri {
				found = true
				break
			}
		}
		if !found {
			version := b.version
			result.publications = append(result.publications, publication{"", uri, &version, []analysis.Diagnostic{}})
		}
	}

	// Reconciliation may replace the worker epoch without changing edit inputs.
	// Compare existing immutable acquisition facts in the worker, not the owner.
	if err := ctx.Err(); err != nil {
		result.err = err
		return result
	}
	complete := previous.err == nil && len(previous.failures) == 0 && len(result.failures) == 0
	for _, scopes := range [][]scopeResult{previous.scopes, result.scopes} {
		for _, scope := range scopes {
			complete = complete && scope.options.Complete
		}
	}
	result.unchangedInputs = complete && reflect.DeepEqual(previous.inputs, result.inputs)
	return result
}

type rootCheckpoint struct{ inputs, scopes, publications int }

// failRoot drops the failed root's partially accumulated result, so it cannot
// supply stale diagnostics, reference authority, or edit candidates.
func (result *completed) failRoot(checkpoint rootCheckpoint, root, uri string, buffers map[string]buffer, err error) {
	clear(result.inputs[checkpoint.inputs:])
	clear(result.scopes[checkpoint.scopes:])
	clear(result.publications[checkpoint.publications:])
	result.inputs = result.inputs[:checkpoint.inputs]
	result.scopes = result.scopes[:checkpoint.scopes]
	result.publications = result.publications[:checkpoint.publications]
	result.failures[root] = err
	code := "ANALYSIS_INCOMPLETE"
	if failure, ok := err.(*workspace.Error); ok {
		code = failure.Code
	}
	var version *int
	if b, ok := buffers[uri]; ok {
		v := b.version
		version = &v
	}
	result.publications = append(result.publications, publication{"", uri, version, []analysis.Diagnostic{{URI: uri, Code: code, Source: "argocd-ls", Severity: 1, Message: err.Error()}}})
}

// Rebuild budget accounting only after a root is rolled back. Schema preparation
// remains reusable, while projections belonging to discarded roots are released.
func retainedAnalyzer(previous completed, retained []scopeResult) *analysis.Analyzer {
	analyzer := new(analysis.Analyzer)
	if previous.err == nil {
		for _, scope := range previous.scopes {
			analyzer.ReuseSchemas(scope.documents)
		}
	}
	for _, scope := range retained {
		analyzer.ReuseSchemas(scope.documents)
		analyzer.ReuseProjections(scope.documents)
	}
	return analyzer
}

func sameScopeFiles(before, after []workspace.File) bool {
	if len(before) != len(after) {
		return false
	}
	// Both slices come from analyzeRoots' URI-sorted scope membership.
	for i := range before {
		if !sameFile(before[i], after[i]) {
			return false
		}
	}
	return true
}

func sameFile(before, after workspace.File) bool {
	return before.Snapshot.URI() == after.Snapshot.URI() && before.Path == after.Path && before.Target == after.Target && before.Output == after.Output && before.Snapshot.Revision() == after.Snapshot.Revision() && before.Snapshot.Text() == after.Snapshot.Text()
}
func readMessage(reader *bufio.Reader) (message, error) {
	var m message
	length := -1
	headers := 0
	for {
		line, e := reader.ReadSlice('\n')
		if e != nil {
			return m, e
		}
		headers += len(line)
		if headers > 8192 {
			return m, fmt.Errorf("LSP headers too large")
		}
		text := strings.TrimSpace(string(line))
		if text == "" {
			break
		}
		parts := strings.SplitN(text, ":", 2)
		if len(parts) != 2 {
			return m, fmt.Errorf("invalid LSP header")
		}
		if strings.EqualFold(parts[0], "Content-Length") {
			if length >= 0 {
				return m, fmt.Errorf("duplicate Content-Length")
			}
			length, e = strconv.Atoi(strings.TrimSpace(parts[1]))
			if e != nil {
				return m, e
			}
		}
	}
	if length < 0 || length > 10*1024*1024 {
		return m, fmt.Errorf("invalid LSP message length")
	}
	body := make([]byte, length)
	if _, e := io.ReadFull(reader, body); e != nil {
		return m, e
	}
	if e := json.Unmarshal(body, &m); e != nil {
		return m, e
	}
	return m, nil
}

type fixData struct {
	URI         string           `json:"uri"`
	Epoch       uint64           `json:"epoch"`
	Revision    uint64           `json:"revision"`
	Target      string           `json:"target"`
	InputTarget workspace.Target `json:"inputTarget"`
	FixCode     string           `json:"fixCode"`
	Start       int              `json:"start"`
	End         int              `json:"end"`
	NewText     string           `json:"newText"`
}

func replacementEdit(snapshot source.Snapshot, span source.Span, text string) analysis.TextEdit {
	a, _ := snapshot.Position(span.Start)
	b, _ := snapshot.Position(span.End)
	return analysis.TextEdit{Range: analysis.Range{Start: analysis.Position{Line: a.Line, Character: a.UTF16Column}, End: analysis.Position{Line: b.Line, Character: b.UTF16Column}}, NewText: text}
}

func fixRequest(ctx context.Context, m message, latest completed, buffers map[string]buffer, epoch uint64, canEdit bool, roots []string, override string) (any, int, string) {
	if result, status, message, handled := pinRequest(ctx, m, latest, buffers, epoch, canEdit, roots, override); handled {
		return result, status, message
	}
	switch m.Method {
	case "textDocument/codeAction":
		if !canEdit {
			return []any{}, 0, ""
		}
		if latest.epoch != epoch || latest.err != nil {
			return nil, -32801, "analysis changed"
		}
		var p struct {
			TextDocument struct {
				URI string `json:"uri"`
			} `json:"textDocument"`
			Range analysis.Range `json:"range"`
		}
		if json.Unmarshal(m.Params, &p) != nil {
			return nil, -32602, "invalid codeAction params"
		}
		b, ok := buffers[p.TextDocument.URI]
		if !ok {
			return []any{}, 0, ""
		}
		actions := []any{}
		for _, scope := range latest.scopes {
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) != p.TextDocument.URI {
					continue
				}
				rangeStart, e1 := doc.Snapshot.Offset(source.Position{Line: p.Range.Start.Line, UTF16Column: p.Range.Start.Character})
				rangeEnd, e2 := doc.Snapshot.Offset(source.Position{Line: p.Range.End.Line, UTF16Column: p.Range.End.Character})
				if e1 != nil || e2 != nil {
					continue
				}
				for _, fix := range analysis.FixCandidatesInRange(ctx, doc, scope.documents, scope.options, source.Span{Start: rangeStart, End: rangeEnd}) {
					replacement := fix.Plan.Replacements[0]
					if replacement.Span.Start > rangeEnd || replacement.Span.End < rangeStart {
						continue
					}
					data := fixData{URI: p.TextDocument.URI, Revision: uint64(b.snapshot.Revision()), Target: doc.Target, InputTarget: inputTarget(latest, p.TextDocument.URI), FixCode: fix.Code, Start: replacement.Span.Start, End: replacement.Span.End, NewText: replacement.NewText}
					actions = append(actions, map[string]any{"title": fix.Title, "kind": "quickfix", "data": data})
				}
			}
		}
		return actions, 0, ""
	case "codeAction/resolve":
		var action map[string]any
		var envelope struct {
			Data fixData `json:"data"`
		}
		if json.Unmarshal(m.Params, &action) != nil || json.Unmarshal(m.Params, &envelope) != nil {
			return nil, -32602, "invalid action"
		}
		data := envelope.Data
		b, ok := buffers[data.URI]
		if !canEdit || !ok || data.InputTarget != inputTarget(latest, data.URI) || latest.epoch != epoch || latest.err != nil || data.Revision != uint64(b.snapshot.Revision()) {
			return nil, -32801, "code action is stale"
		}
		matched := false
		for _, scope := range latest.scopes {
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) != data.URI || doc.Target != data.Target {
					continue
				}
				for _, fix := range analysis.ResolveFix(ctx, doc, scope.documents, scope.options, source.Span{Start: data.Start, End: data.End}, data.FixCode, data.NewText) {
					r := fix.Plan.Replacements[0]
					if fix.Code == data.FixCode && r.Span.Start == data.Start && r.Span.End == data.End && r.NewText == data.NewText {
						action["edit"] = map[string]any{"documentChanges": []any{map[string]any{"textDocument": map[string]any{"uri": data.URI, "version": b.version}, "edits": []analysis.TextEdit{replacementEdit(doc.Snapshot, r.Span, r.NewText)}}}}
						matched = true
					}
				}
			}
		}
		if !matched {
			return nil, -32801, "action is no longer safe"
		}
		return action, 0, ""
	}
	return nil, 0, ""
}

func readRequest(ctx context.Context, m message, latest completed, epoch uint64, roots []string) (answer any, code int, why string) {
	if latest.epoch != epoch || latest.err != nil {
		return nil, -32801, "analysis input changed; retry after current diagnostics"
	}
	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Position analysis.Position `json:"position"`
		Context  struct {
			IncludeDeclaration bool `json:"includeDeclaration"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return nil, -32602, "invalid query params"
	}
	file, err := workspace.PathFromURI(p.TextDocument.URI)
	if err == nil {
		if failure := latest.failures[workspace.Owner(roots, file)]; failure != nil {
			return nil, -32801, "analysis input unavailable: " + failure.Error()
		}
	}
	answer = []any{}
	if m.Method == "textDocument/hover" {
		answer = nil
	}
	for _, scope := range latest.scopes {
		found := false
		for _, doc := range scope.documents {
			if string(doc.Snapshot.URI()) == p.TextDocument.URI {
				found = true
				break
			}
		}
		if m.Method == "textDocument/completion" {
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					found = true
				}
			}
		}
		if m.Method == "textDocument/definition" && latest.incremental == nil {
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if recovered := analysis.RecoverProjectUse(ctx, f.Snapshot, f.Target.Version, p.Position); recovered != nil {
						scope.documents = append(append([]analysis.Document(nil), scope.documents...), *recovered)
						found = true
					}
				}
			}
		}
		if !found {
			continue
		}
		switch m.Method {
		case "textDocument/definition":
			answer = analysis.Definition(scope.documents, scope.options, p.TextDocument.URI, p.Position)
		case "textDocument/references":
			if !scope.options.Complete {
				return nil, -32801, "workspace discovery is incomplete; target uniqueness is unproved"
			}
			answer = analysis.References(scope.documents, scope.options, p.TextDocument.URI, p.Position, p.Context.IncludeDeclaration)
		case "textDocument/documentSymbol":
			answer = analysis.Symbols(scope.documents, p.TextDocument.URI)
		case "textDocument/hover":
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) == p.TextDocument.URI {
					if hover := analysis.SchemaHover(doc, p.Position); hover != nil {
						answer = hover
						break
					}
				}
			}
		case "textDocument/completion":
			for _, f := range scope.files {
				if latest.documentOnly {
					break // Field completion needs no complete reference index.
				}
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if items := analysis.ReferenceCandidates(ctx, f.Snapshot, f.Target.Version, scope.documents, scope.options, p.Position); len(items) > 0 {
						answer = items
						break
					}
				}
			}
			if items, ok := answer.([]analysis.Completion); ok && len(items) > 0 {
				break
			}
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if items := analysis.FieldCompletions(ctx, f.Snapshot, f.Target.Version, scope.documents, p.Position); len(items) > 0 {
						answer = items
					} else if latest.documentOnly {
						// An empty field result can be a reference-value request.
						// Do not claim that a partial index has no candidates.
						return nil, -32801, "workspace reference candidates are not ready; retry after workspace analysis"
					}
					break
				}
			}
		}
		break
	}
	return
}

// A proof spans only the immutable inputs actually used for an answer. Local
// edits need the selected buffer and configuration; candidate edits and reads
// additionally retain the relevant cross-file identities and positions.
type readProof struct {
	engine                 *workspaceIndex
	catalog                *workspace.Catalog
	scope                  index.Scope
	scopeName              string
	complete               bool
	hashes                 map[source.URI][32]byte
	candidates             map[index.Key][]index.Definition
	uses                   map[index.Key][]index.Use
	allProjects, localOnly bool
	projectGeneration      uint64
	root, override         string
	diskGeneration         uint64
	files, knownFiles      map[string]bool
	buffers                map[string]buffer
}

func traceReadRequest(out io.Writer, m message, input completed, proof *readProof) {
	if os.Getenv("ARGOCD_LS_TRACE") != "1" {
		return
	}
	if input.stats.Scope == "query-acquisition" {
		payload, _ := json.Marshal(input.stats)
		fmt.Fprintln(out, "argocd-query-acquisition", string(payload))
	}
	if proof == nil {
		return
	}
	// Counts and identities explain which immutable dependency set was used
	// without logging document contents or unbounded lists of files.
	payload, _ := json.Marshal(map[string]any{"method": m.Method, "epoch": input.epoch, "root": proof.root, "dependencyFiles": len(proof.files), "knownFiles": len(proof.knownFiles), "diskGeneration": proof.diskGeneration})
	fmt.Fprintln(out, "argocd-read-proof", string(payload))
}

func newReadProof(m message, input completed, buffers map[string]buffer, roots []string, override string, disk map[string]uint64) *readProof {
	uri := queryURI(m)
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return nil
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return nil
	}
	proof := &readProof{root: root, override: override, diskGeneration: disk[root], files: map[string]bool{}, knownFiles: map[string]bool{}, buffers: buffers}
	if input.queryProof != nil {
		proof.engine, proof.catalog, proof.scope, proof.scopeName, proof.complete = input.queryProof.engine, input.queryProof.catalog, input.queryProof.scope, input.queryProof.scopeName, input.queryProof.complete
		proof.hashes, proof.candidates = input.queryProof.hashes, input.queryProof.candidates
		proof.uses, proof.allProjects, proof.projectGeneration = input.queryProof.uses, input.queryProof.allProjects, input.queryProof.projectGeneration
	}
	found := false
	for _, scope := range input.scopes {
		if scope.root != root {
			continue
		}
		selected := false
		for _, f := range scope.files {
			uri := string(f.Snapshot.URI())
			proof.knownFiles[uri] = true
			if uri == queryURI(m) {
				selected = true
			}
		}
		if selected {
			found = true
			for _, f := range scope.files {
				proof.files[string(f.Snapshot.URI())] = true
			}
		}
	}
	if !found {
		return nil
	}
	return proof
}
func (proof *readProof) valid(current map[string]buffer, roots []string, override string, disk map[string]uint64) bool {
	if proof.engine != nil && !proof.engine.proofValid(proof) {
		return false
	}
	if proof.override != override || (proof.engine == nil && proof.diskGeneration != disk[proof.root]) {
		return false
	}
	relevant := func(uri string) bool {
		p, err := workspace.PathFromURI(uri)
		return err == nil && workspace.Owner(roots, p) == proof.root && (proof.files[uri] || filepath.Base(p) == ".argocd-ls.yaml")
	}
	same := func(a, b buffer) bool {
		return a.revision == b.revision && a.version == b.version && a.unavailable == b.unavailable && a.snapshot.Text() == b.snapshot.Text()
	}
	for uri := range proof.files {
		p, err := workspace.PathFromURI(uri)
		if err != nil || workspace.Owner(roots, p) != proof.root {
			return false
		}
	}
	for uri, b := range proof.buffers {
		if relevant(uri) {
			now, ok := current[uri]
			if !ok || !same(b, now) {
				return false
			}
		}
	}
	for uri, b := range current {
		p, err := workspace.PathFromURI(uri)
		if err != nil || workspace.Owner(roots, p) != proof.root {
			continue
		}
		old, wasOpen := proof.buffers[uri]
		if relevant(uri) {
			if !wasOpen || !same(old, b) {
				return false
			}
		} else if !proof.localOnly && !proof.knownFiles[uri] && (!wasOpen || !same(old, b)) {
			if proof.catalog != nil {
				target, included, err := proof.catalog.Select(p, true)
				if err == nil && (!included || target.Scope != proof.scopeName) {
					continue
				}
			}
			return false
		}
	}
	return true
}
