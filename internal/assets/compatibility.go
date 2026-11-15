package assets

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sync"
)

//go:embed compatibility.json
var compatibilityBytes []byte

type CompatibilityBranch struct {
	Ref      string `json:"ref"`
	Versions []int  `json:"versions"`
}

// A profile groups inputs to the current product rules. It makes no assertion
// that unmodeled upstream behavior is equal between these releases.
type CompatibilityProfile struct {
	Representative   string   `json:"representative"`
	SingleSourceRefs bool     `json:"singleSourceRefs"`
	TemplateRequired []string `json:"templateRequired"`
	Versions         []int    `json:"versions"`
}

type CompatibilityKind struct {
	APIVersion string                 `json:"apiVersion"`
	SchemaHash string                 `json:"schemaSHA256"`
	Branches   []CompatibilityBranch  `json:"branches"`
	Profiles   []CompatibilityProfile `json:"profiles"`
}

type EvidenceCoverage struct {
	ModeledInventorySHA256     string   `json:"modeledInventorySHA256"`
	HistoricalReleaseCount     int      `json:"historicalReleaseCount"`
	HistoricalVersions         []string `json:"historicalVersions"`
	BoundarySampleVersions     []string `json:"boundarySampleVersions"`
	AllCatalogSemanticsAudited bool     `json:"allCatalogSemanticsAudited"`
	Statement                  string   `json:"statement"`
}

type CompatibilityMetadata struct {
	SchemaVersion          int                          `json:"schemaVersion"`
	CatalogHash            string                       `json:"catalogSHA256"`
	RuleEvidenceHash       string                       `json:"ruleEvidenceSHA256"`
	HistoricalEvidenceHash string                       `json:"historicalEvidenceSHA256"`
	EvidenceCoverage       EvidenceCoverage             `json:"evidenceCoverage"`
	Versions               []string                     `json:"versions"`
	Kinds                  map[string]CompatibilityKind `json:"kinds"`
}

var compatibilityOnce sync.Once
var compatibilityMetadata CompatibilityMetadata
var compatibilityError error

// CompatibilityIndex returns immutable generated metadata bound to the exact
// embedded catalog. A stale sidecar disables only explicit explanation.
func CompatibilityIndex() (*CompatibilityMetadata, error) {
	compatibilityOnce.Do(func() {
		compatibilityError = json.Unmarshal(compatibilityBytes, &compatibilityMetadata)
		if compatibilityError != nil {
			return
		}
		m := &compatibilityMetadata
		c, err := catalogData()
		if err != nil || m.SchemaVersion != 1 || m.CatalogHash != CatalogHash() || len(m.Versions) != len(c.Versions) {
			compatibilityError = fmt.Errorf("compatibility metadata does not match the embedded catalog")
			return
		}
		for i, target := range c.Versions {
			if m.Versions[i] != target.Version {
				compatibilityError = fmt.Errorf("compatibility release identity differs")
				return
			}
		}
		for _, kind := range m.Kinds {
			for _, branch := range kind.Branches {
				for _, i := range branch.Versions {
					if i < 0 || i >= len(m.Versions) {
						compatibilityError = fmt.Errorf("compatibility branch contains an invalid release")
						return
					}
				}
			}
			for _, profile := range kind.Profiles {
				if !Known(profile.Representative) {
					compatibilityError = fmt.Errorf("compatibility profile contains an unknown release")
					return
				}
				for _, i := range profile.Versions {
					if i < 0 || i >= len(m.Versions) {
						compatibilityError = fmt.Errorf("compatibility profile contains an invalid release")
						return
					}
				}
			}
		}
	})
	return &compatibilityMetadata, compatibilityError
}

func CompatibilityHash() string {
	hash := sha256.Sum256(compatibilityBytes)
	return hex.EncodeToString(hash[:])
}
