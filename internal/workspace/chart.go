package workspace

import (
	"fmt"
	"os"
	"path/filepath"
)

// FindChart selects the nearest ancestor Chart.yaml inside the owning root.
// Both boundaries are canonical filesystem paths; sibling charts are never guessed.
func FindChart(root, file string) (string, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	if !filepath.IsAbs(file) {
		file = filepath.Join(root, file)
	}
	file, err = filepath.EvalSymlinks(file)
	if err != nil {
		return "", err
	}
	if !inside(root, file) {
		return "", fmt.Errorf("chart input must stay inside source root")
	}
	info, err := os.Stat(file)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		file = filepath.Dir(file)
	}
	for dir := file; inside(root, dir); dir = filepath.Dir(dir) {
		if info, err := os.Stat(filepath.Join(dir, "Chart.yaml")); err == nil && info.Mode().IsRegular() {
			return dir, nil
		}
		if dir == root {
			break
		}
	}
	return "", fmt.Errorf("no ancestor Chart.yaml inside source root; select --chart or --root")
}
