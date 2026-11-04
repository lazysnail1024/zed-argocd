// Package assets resolves only exact, compiled-in catalog identities. Collection
// does not imply that the local editing contract has been behaviorally verified.
package assets

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"sync"
)

//go:embed catalog.json
var catalogBytes []byte

type catalog struct {
	Versions []struct {
		UpstreamCommit string            `json:"upstreamCommit"`
		Version        string            `json:"version"`
		Verified       bool              `json:"semanticsVerified"`
		Schemas        []json.RawMessage `json:"schemas"`
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

// Known reports inventory membership without claiming behavior acceptance.
func Known(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	for _, v := range c.Versions {
		if v.Version == version {
			return true
		}
	}
	return false
}

func BundleHash(version string) string {
	c, err := catalogData()
	if err != nil {
		return ""
	}
	for _, target := range c.Versions {
		if target.Version == version {
			b, _ := json.Marshal(target.Schemas)
			h := sha256.Sum256(b)
			return hex.EncodeToString(h[:])
		}
	}
	return ""
}

// SingleSourceRefs is pinned from the selected upstream GetRefSources behavior.
func SingleSourceRefs(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	for _, target := range c.Versions {
		if target.Version == version {
			return target.LocalRules.SingleSourceRefs
		}
	}
	return false
}

// SchemaSource identifies the immutable official CRD used by an exact target.
func SchemaSource(version, kind string) string {
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

// DefaultVersion selects the highest exact locally verified target in this binary.
func DefaultVersion() string {
	c, err := catalogData()
	if err != nil {
		return ""
	}
	best := ""
	var bestParts [3]int
	for _, target := range c.Versions {
		if !target.Verified {
			continue
		}
		parts := strings.Split(target.Version, ".")
		if len(parts) != 3 {
			continue
		}
		var current [3]int
		valid := true
		for i, part := range parts {
			n, e := strconv.Atoi(part)
			if e != nil || n < 0 {
				valid = false
				break
			}
			current[i] = n
		}
		if !valid {
			continue
		}
		for i := range current {
			if best == "" || current[i] > bestParts[i] {
				best, bestParts = target.Version, current
				break
			}
			if current[i] < bestParts[i] {
				break
			}
		}
	}
	return best
}

// Verified describes this product's local editing contract, not controller execution.
func Verified(version string) bool {
	c, err := catalogData()
	if err != nil {
		return false
	}
	for _, target := range c.Versions {
		if target.Version == version {
			return target.Verified
		}
	}
	return false
}
