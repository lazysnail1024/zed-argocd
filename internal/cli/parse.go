package cli

import (
	"context"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

type parsedFile struct {
	file analysis.ParsedFile
	err  error
}

// The unbuffered handoff overlaps one next parse with serial analysis. It never
// queues a second parsed view, alters file order or commits analysis counts.
func parseFiles(parent context.Context, files []workspace.File) (<-chan parsedFile, func()) {
	ctx, cancel := context.WithCancel(parent)
	ready := make(chan parsedFile)
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer close(ready)
		for _, file := range files {
			if ctx.Err() != nil {
				return
			}
			parsed, err := analysis.ParseFile(ctx, file.Snapshot)
			select {
			case ready <- parsedFile{parsed, err}:
			case <-ctx.Done():
				return
			}
		}
	}()
	return ready, func() { cancel(); <-done }
}
