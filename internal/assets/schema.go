package assets

import (
	"embed"
	"encoding/json"
	"fmt"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"path"
	"strings"
)

//go:embed schemas/*.json
var schemas embed.FS

type noNetwork struct{}

func (noNetwork) Load(url string) (any, error) {
	return nil, fmt.Errorf("external schema reference forbidden: %s", url)
}

func CompileSchema(value map[string]any) (*schema.Schema, error) {
	compiler := schema.NewCompiler()
	compiler.DefaultDraft(schema.Draft2020)
	compiler.UseLoader(noNetwork{})
	const uri = "urn:argocd:embedded-schema"
	if err := compiler.AddResource(uri, value); err != nil {
		return nil, err
	}
	return compiler.Compile(uri)
}

func SchemaData(version, apiVersion, kind string) (map[string]any, error) {
	c, err := catalogData()
	if err != nil {
		return nil, err
	}
	for _, v := range c.Versions {
		if v.Version == version {
			for _, raw := range v.Schemas {
				var s struct {
					APIVersion string `json:"apiVersion"`
					Kind       string `json:"kind"`
					Path       string `json:"path"`
				}
				if err := json.Unmarshal(raw, &s); err != nil {
					return nil, err
				}
				if s.APIVersion == apiVersion && s.Kind == kind {
					b, err := schemas.ReadFile("schemas/" + path.Base(s.Path))
					if err != nil {
						return nil, err
					}
					var value any
					if err = json.Unmarshal(b, &value); err != nil {
						return nil, err
					}
					if err = checkKeywords(value); err != nil {
						return nil, err
					}
					return value.(map[string]any), nil
				}
			}
		}
	}
	return nil, fmt.Errorf("no collected schema for %s %s %s", version, apiVersion, kind)
}

// Unknown constraint keywords fail closed. These source schemas currently use
// no nullable/exclusive boolean conversion; future inputs must add explicit support.
func checkKeywords(value any) error {
	m, ok := value.(map[string]any)
	if !ok {
		return nil
	}
	for k, v := range m {
		switch k {
		case "type", "description", "default", "const", "required", "format", "minLength", "maxLength", "pattern", "enum", "x-kubernetes-preserve-unknown-fields", "x-kubernetes-int-or-string", "x-kubernetes-list-type", "x-kubernetes-map-type":
		case "$ref":
			ref, ok := v.(string)
			if !ok || !strings.HasPrefix(ref, "#/") {
				return fmt.Errorf("only local schema references are supported")
			}
		case "properties", "$defs", "definitions":
			for _, child := range v.(map[string]any) {
				if err := checkKeywords(child); err != nil {
					return err
				}
			}
		case "items", "additionalProperties":
			if err := checkKeywords(v); err != nil {
				return err
			}
		case "anyOf", "allOf", "oneOf":
			for _, child := range v.([]any) {
				if err := checkKeywords(child); err != nil {
					return err
				}
			}
		default:
			return fmt.Errorf("unsupported OpenAPI keyword %q", k)
		}
	}
	return nil
}
