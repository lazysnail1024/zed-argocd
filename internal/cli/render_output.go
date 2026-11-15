package cli

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

// customRenderOutput pins a user-selected directory for this invocation only.
// It never claims ownership, writes a cache marker or recursively removes files.
type customRenderOutput struct {
	path, chart string
	root        *os.Root
	identity    os.FileInfo
}

func openCustomRenderOutput(path, chart string) (*customRenderOutput, error) {
	if err := os.MkdirAll(path, 0755); err != nil {
		return nil, err
	}
	before, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !before.IsDir() || before.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("custom render output must be a directory, not a symlink")
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return nil, err
	}
	output := &customRenderOutput{path: path, chart: chart, root: root, identity: before}
	file, err := root.Open(".")
	if err == nil {
		var opened os.FileInfo
		opened, err = file.Stat()
		file.Close()
		if err == nil && !os.SameFile(before, opened) {
			err = fmt.Errorf("custom render output changed while opening")
		}
	}
	if err == nil {
		err = output.check()
	}
	if err != nil {
		root.Close()
		return nil, err
	}
	return output, nil
}

func (output *customRenderOutput) check() error {
	current, err := os.Lstat(output.path)
	if err != nil || !current.IsDir() || current.Mode()&os.ModeSymlink != 0 || !os.SameFile(output.identity, current) {
		return fmt.Errorf("custom render output directory changed; select a fresh output directory")
	}
	canonical, err := renderOutputPath(output.path, output.chart)
	if err != nil || canonical != output.path {
		return fmt.Errorf("custom render output placement changed")
	}
	return nil
}

func (output *customRenderOutput) write(name string, data []byte) error {
	if filepath.Base(name) != name {
		return fmt.Errorf("invalid render artifact name")
	}
	if err := output.check(); err != nil {
		return err
	}
	before, err := output.root.Lstat(name)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if before != nil && (!before.Mode().IsRegular() || before.Mode()&os.ModeSymlink != 0) {
		return fmt.Errorf("render output must be a regular file: %s", name)
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return err
	}
	temporary := ".argocd-render-write-" + hex.EncodeToString(nonce[:])
	mode := os.FileMode(0644)
	if before != nil {
		mode = before.Mode().Perm()
	}
	file, err := output.root.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	defer output.root.Remove(temporary)
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr != nil {
		return writeErr
	}
	if closeErr != nil {
		return closeErr
	}
	if err := output.check(); err != nil {
		return err
	}
	after, err := output.root.Lstat(name)
	if before == nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("render output appeared during write: %s", name)
		}
	} else if err != nil || !after.Mode().IsRegular() || after.Mode()&os.ModeSymlink != 0 || !os.SameFile(before, after) {
		return fmt.Errorf("render output changed during write: %s", name)
	}
	// Replace the directory entry, never truncate the prior inode. Even an
	// existing hardlink cannot modify the source bytes through this write.
	return output.root.Rename(temporary, name)
}
