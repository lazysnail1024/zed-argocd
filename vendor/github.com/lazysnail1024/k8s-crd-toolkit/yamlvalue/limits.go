package yamlvalue

import "github.com/lazysnail1024/k8s-crd-toolkit/yamlview"

func logicalNodes(value any) int {
	count := 1
	switch v := value.(type) {
	case map[string]any:
		for _, child := range v {
			count += 1 + logicalNodes(child)
		}
	case []any:
		for _, child := range v {
			count += logicalNodes(child)
		}
	}
	return count
}

func oversizedScalar(n yamlview.Node) bool {
	if n.Kind() == yamlview.Scalar && len(n.Raw()) > 1024*1024 {
		return true
	}
	for _, member := range n.Members() {
		if oversizedScalar(member.Key) || oversizedScalar(member.Value) {
			return true
		}
	}
	for _, child := range n.Elements() {
		if oversizedScalar(child) {
			return true
		}
	}
	return false
}
