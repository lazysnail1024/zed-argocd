// Package assets resolves the immutable release inventory embedded in this binary.
package assets

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"sync"
)

//go:embed catalog.json
var catalogBytes []byte

type schemaEntry struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Path       string `json:"path"`
	Hash       string `json:"sha256"`
}
type catalog struct {
	DefaultVersion  string        `json:"defaultVersion"`
	LatestVersion   string        `json:"latestVersion"`
	SupportedMajors []int         `json:"supportedMajors"`
	AutoSchemas     []schemaEntry `json:"autoSchemas"`
	Ready           bool          `json:"ready"`
	Versions        []struct {
		UpstreamCommit string        `json:"upstreamCommit"`
		Version        string        `json:"version"`
		Verified       bool          `json:"semanticsVerified"`
		Schemas        []schemaEntry `json:"schemas"`
		LocalRules     struct {
			SingleSourceRefs bool `json:"singleSourceRefs"`
		} `json:"localRules"`
	} `json:"versions"`
}

var catalogOnce sync.Once
var decodedCatalog catalog
var catalogError error

func catalogData() (catalog, error) {
	catalogOnce.Do(func() { catalogError = json.Unmarshal(catalogBytes, &decodedCatalog) })
	return decodedCatalog, catalogError
}

func CatalogHash() string { h := sha256.Sum256(catalogBytes); return hex.EncodeToString(h[:]) }

// Known accepts auto or an exact collected stable version; it never guesses a patch.
func Known(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	if version == "auto" {
		return len(c.AutoSchemas) > 0
	}
	for _, v := range c.Versions {
		if v.Version == version {
			return true
		}
	}
	return false
}

func schemaEntries(version string) []schemaEntry {
	c, err := catalogData()
	if err != nil {
		return nil
	}
	if version == "auto" {
		return c.AutoSchemas
	}
	for _, v := range c.Versions {
		if v.Version == version {
			return v.Schemas
		}
	}
	return nil
}

func BundleHash(version string) string {
	entries := schemaEntries(version)
	if len(entries) == 0 {
		return ""
	}
	b, _ := json.Marshal(entries)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// SingleSourceRefs follows the selected release, or any covered release in auto.
func SingleSourceRefs(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	for _, target := range c.Versions {
		if target.Version == version || version == "auto" {
			if target.LocalRules.SingleSourceRefs {
				return true
			}
		}
	}
	return false
}

// SchemaSource links exact targets to immutable official source. Auto cites its
// complete, content-addressed catalog rather than suggesting one release was chosen.
func SchemaSource(version, kind string) string {
	if version == "auto" {
		return "https://github.com/lazysnail1024/zed-argocd/blob/main/compat/catalog.lock.json"
	}
	name := map[string]string{"Application": "application", "AppProject": "appproject", "ApplicationSet": "applicationset"}[kind]
	if name == "" {
		return ""
	}
	c, err := catalogData()
	if err != nil {
		return ""
	}
	for _, v := range c.Versions {
		if v.Version == version {
			return "https://raw.githubusercontent.com/argoproj/argo-cd/" + v.UpstreamCommit + "/manifests/crds/" + name + "-crd.yaml"
		}
	}
	return ""
}

// DefaultVersion never requires a workspace pin for supported stable releases.
func DefaultVersion() string {
	c, err := catalogData()
	if err != nil || !c.Ready {
		return ""
	}
	return c.DefaultVersion
}

// Verified covers this product's tested local editing contract, not controller execution.
func Verified(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	if version == "auto" {
		return c.Ready
	}
	for _, target := range c.Versions {
		if target.Version == version {
			return target.Verified
		}
	}
	return false
}

type SupportWindow struct {
	Min          string `json:"min"`
	Max          string `json:"max"`
	Majors       []int  `json:"majors"`
	ReleaseCount int    `json:"releaseCount"`
}

func SupportedVersions() SupportWindow {
	c, err := catalogData()
	if err != nil || len(c.Versions) == 0 {
		return SupportWindow{}
	}
	return SupportWindow{c.Versions[0].Version, c.LatestVersion, c.SupportedMajors, len(c.Versions)}
}
