// Package buildinfo describes the actual bootstrap binary without inventing release metadata.
package buildinfo

import (
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"runtime/debug"
	"strings"
)

const Product = "argo-cd"
const Server = "argocd-ls"

// These variables may be set by an explicit go build -ldflags invocation.
var ServerVersion = "0.0.1-alpha.1"
var SourceCommit = ""
var Dirty = "true"
var BuildMode = "development"

type Info struct {
	Product                string               `json:"product"`
	ServerVersion          string               `json:"serverVersion"`
	AdapterContractVersion int                  `json:"adapterContractVersion"`
	ConfigSchemaVersions   []int                `json:"configSchemaVersions"`
	ReportSchemaVersions   []int                `json:"reportSchemaVersions"`
	PackFormatVersions     []int                `json:"packFormatVersions"`
	ToolkitVersion         *string              `json:"toolkitVersion"`
	CatalogHash            *string              `json:"catalogHash"`
	SourceCommit           *string              `json:"sourceCommit"`
	Dirty                  bool                 `json:"dirty"`
	BuildMode              string               `json:"buildMode"`
	AnalysisReady          bool                 `json:"analysisReady"`
	DefaultTargetVersion   string               `json:"defaultTargetVersion"`
	SupportedVersions      assets.SupportWindow `json:"supportedVersions"`
}

func Current() Info {
	catalogHash := assets.CatalogHash()
	var toolkitVersion *string
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, dep := range info.Deps {
			if dep.Path == "github.com/lazysnail1024/k8s-crd-toolkit" {
				version := dep.Version
				toolkitVersion = &version
				break
			}
		}
	}
	var commit *string
	if s := strings.TrimSpace(SourceCommit); s != "" {
		commit = &s
	}
	return Info{
		Product: Product, ServerVersion: ServerVersion, AdapterContractVersion: 1,
		DefaultTargetVersion: assets.DefaultVersion(), SupportedVersions: assets.SupportedVersions(),
		ConfigSchemaVersions: []int{1}, ReportSchemaVersions: []int{1}, PackFormatVersions: []int{},
		SourceCommit: commit, Dirty: Dirty != "false", BuildMode: BuildMode,
		AnalysisReady: assets.DefaultVersion() != "", CatalogHash: &catalogHash, ToolkitVersion: toolkitVersion,
	}
}
