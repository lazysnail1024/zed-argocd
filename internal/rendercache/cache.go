// Package rendercache owns only the program's default rendered-artifact cache.
// Explicit output directories never enter this package. Ownership requires a
// sealed inventory, an exact directory identity and the fixed product namespace.
package rendercache

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	product       = "argocd-ls"
	MarkerName    = "cache-manifest.json"
	maxFileBytes  = 64 * 1024 * 1024
	maxTotalBytes = 128 * 1024 * 1024
	maxMarker     = 64 * 1024
	maxEntries    = 4096
)

var artifactNames = map[string]bool{
	"helm.stdout": true, "helm.stderr": true,
	"report.json": true, "generated.yaml": true,
}

// File describes exact artifact bytes and permissions, not an arbitrary path.
type File struct {
	Name   string `json:"name"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
	Mode   uint32 `json:"mode"`
}

// Entry.Owned is true only after the current sealed inventory has been verified.
// Unsealed, changed and foreign entries remain visible with a refusal reason.
type Entry struct {
	Directory string `json:"directory"`
	Owned     bool   `json:"owned"`
	Reason    string `json:"reason,omitempty"`
	CreatedAt string `json:"createdAt,omitempty"`
	Files     []File `json:"files,omitempty"`
}

type manifest struct {
	SchemaVersion     int    `json:"schemaVersion"`
	Product           string `json:"product"`
	Nonce             string `json:"nonce"`
	Directory         string `json:"directory"`
	DirectoryIdentity string `json:"directoryIdentity"`
	CreatedAt         string `json:"createdAt"`
	Sealed            bool   `json:"sealed"`
	Files             []File `json:"files"`
}

type fileState struct {
	record File
	info   fs.FileInfo
}

// Store holds the actual parent and entry directories. A renamed or replaced
// path cannot redirect writes through the user-visible Directory string.
type Store struct {
	Directory string
	mu        sync.Mutex
	ns        *namespace
	root      *os.Root
	info      fs.FileInfo
	metadata  manifest
	marker    fileState
	files     map[string]fileState
	closed    bool
}

type namespace struct {
	cachePath, directory             string
	cache, product, base             *os.Root
	cacheInfo, productInfo, baseInfo fs.FileInfo
}

func randomNonce() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw[:]), nil
}

func nonceValid(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == 16 && hex.EncodeToString(decoded) == value
}

func regular(info fs.FileInfo) bool {
	return info != nil && info.Mode().IsRegular() && info.Mode()&os.ModeSymlink == 0
}

func directoryInfo(root *os.Root) (fs.FileInfo, string, error) {
	f, err := root.Open(".")
	if err != nil {
		return nil, "", err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, "", err
	}
	if !info.IsDir() {
		return nil, "", fmt.Errorf("cache handle does not name a directory")
	}
	identity, err := nativeDirectoryIdentity(f)
	return info, identity, err
}

func childRoot(parent *os.Root, name string, create bool) (*os.Root, fs.FileInfo, error) {
	if create {
		if err := parent.Mkdir(name, 0700); err != nil && !errors.Is(err, fs.ErrExist) {
			return nil, nil, err
		}
	}
	before, err := parent.Lstat(name)
	if err != nil {
		return nil, nil, err
	}
	if !before.IsDir() || before.Mode()&os.ModeSymlink != 0 {
		return nil, nil, fmt.Errorf("cache directory %q must not be a symlink or non-directory", name)
	}
	root, err := parent.OpenRoot(name)
	if err != nil {
		return nil, nil, err
	}
	opened, _, err := directoryInfo(root)
	after, pathErr := parent.Lstat(name)
	if err != nil || pathErr != nil || after.Mode()&os.ModeSymlink != 0 || !os.SameFile(before, opened) || !os.SameFile(opened, after) {
		root.Close()
		return nil, nil, fmt.Errorf("cache directory %q changed while opening", name)
	}
	return root, opened, nil
}

func openNamespace(create bool) (_ *namespace, err error) {
	cache, err := os.UserCacheDir()
	if err != nil {
		return nil, err
	}
	cache, err = filepath.Abs(cache)
	if err != nil {
		return nil, err
	}
	if create {
		if err := os.MkdirAll(cache, 0700); err != nil {
			return nil, err
		}
	}
	// The configured user cache may itself be a normal platform symlink. The
	// product and render namespaces below this canonical anchor may not be.
	cache, err = filepath.EvalSymlinks(cache)
	if err != nil {
		return nil, err
	}
	n := &namespace{cachePath: cache, directory: filepath.Join(cache, product, "render")}
	defer func() {
		if err != nil {
			n.close()
		}
	}()
	n.cache, err = os.OpenRoot(cache)
	if err != nil {
		return nil, err
	}
	n.cacheInfo, _, err = directoryInfo(n.cache)
	if err != nil {
		return nil, err
	}
	n.product, n.productInfo, err = childRoot(n.cache, product, create)
	if err != nil {
		return nil, err
	}
	n.base, n.baseInfo, err = childRoot(n.product, "render", create)
	if err != nil {
		return nil, err
	}
	return n, n.check()
}

func sameDirectory(parent *os.Root, name string, expected fs.FileInfo) error {
	current, err := parent.Lstat(name)
	if err != nil {
		return err
	}
	if !current.IsDir() || current.Mode()&os.ModeSymlink != 0 || !os.SameFile(current, expected) {
		return fmt.Errorf("cache directory identity changed: %s", name)
	}
	return nil
}

func (n *namespace) check() error {
	current, err := os.Lstat(n.cachePath)
	if err != nil {
		return err
	}
	if !current.IsDir() || current.Mode()&os.ModeSymlink != 0 || !os.SameFile(current, n.cacheInfo) {
		return fmt.Errorf("user cache anchor changed")
	}
	if err := sameDirectory(n.cache, product, n.productInfo); err != nil {
		return err
	}
	return sameDirectory(n.product, "render", n.baseInfo)
}

func (n *namespace) close() error {
	var errs []error
	for _, root := range []*os.Root{n.base, n.product, n.cache} {
		if root != nil {
			errs = append(errs, root.Close())
		}
	}
	return errors.Join(errs...)
}

// Create allocates a fresh default-cache entry. Until Seal succeeds, neither
// List nor Remove treats that entry as a verified removable artifact.
func Create() (*Store, error) {
	n, err := openNamespace(true)
	if err != nil {
		return nil, err
	}
	nonce, err := randomNonce()
	if err != nil {
		n.close()
		return nil, err
	}
	name := "chart-" + nonce
	if err := n.base.Mkdir(name, 0700); err != nil {
		n.close()
		return nil, err
	}
	root, info, err := childRoot(n.base, name, false)
	if err != nil {
		n.close()
		return nil, err
	}
	_, identity, err := directoryInfo(root)
	if err != nil {
		root.Close()
		n.close()
		return nil, err
	}
	s := &Store{Directory: filepath.Join(n.directory, name), ns: n, root: root, info: info, files: map[string]fileState{}}
	s.metadata = manifest{SchemaVersion: 1, Product: product, Nonce: nonce, Directory: s.Directory,
		DirectoryIdentity: identity, CreatedAt: time.Now().UTC().Format(time.RFC3339Nano), Files: []File{}}
	data, err := json.Marshal(s.metadata)
	if err == nil {
		s.marker, err = s.writeFile(MarkerName, append(data, '\n'), nil)
	}
	if err != nil {
		s.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) check() error {
	if s.closed {
		return os.ErrClosed
	}
	if err := s.ns.check(); err != nil {
		return err
	}
	return sameDirectory(s.ns.base, filepath.Base(s.Directory), s.info)
}

func verifyFile(root *os.Root, record File, expected fs.FileInfo) (fs.FileInfo, error) {
	before, err := root.Lstat(record.Name)
	if err != nil {
		return nil, err
	}
	if !regular(before) || before.Size() != record.Size || uint32(before.Mode()) != record.Mode || expected != nil && !os.SameFile(before, expected) {
		return nil, fmt.Errorf("artifact file changed or is not regular: %s", record.Name)
	}
	f, err := openRegular(root, record.Name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !regular(opened) || !os.SameFile(before, opened) {
		return nil, fmt.Errorf("artifact file changed while opening: %s", record.Name)
	}
	hash := sha256.New()
	count, err := io.Copy(hash, io.LimitReader(f, record.Size+1))
	if err != nil {
		return nil, err
	}
	after, err := f.Stat()
	pathInfo, pathErr := root.Lstat(record.Name)
	if err != nil || pathErr != nil || !regular(pathInfo) || !os.SameFile(opened, pathInfo) ||
		count != record.Size || after.Size() != record.Size || uint32(after.Mode()) != record.Mode ||
		hex.EncodeToString(hash.Sum(nil)) != record.SHA256 {
		return nil, fmt.Errorf("artifact content, mode or identity changed: %s", record.Name)
	}
	return after, nil
}

// writeFile never opens an existing destination for truncation. Fresh leaves
// use exclusive creation, so a concurrently added file cannot be overwritten.
// Replacements stage new bytes before rechecking and renaming the owned leaf.
func (s *Store) writeFile(name string, data []byte, previous *fileState) (fileState, error) {
	if err := s.check(); err != nil {
		return fileState{}, err
	}
	if previous != nil {
		if _, err := verifyFile(s.root, previous.record, previous.info); err != nil {
			return fileState{}, err
		}
	} else if _, err := s.root.Lstat(name); !errors.Is(err, fs.ErrNotExist) {
		return fileState{}, fmt.Errorf("artifact destination is not a fresh owned leaf: %s", name)
	}
	temporary := name
	if previous != nil {
		nonce, err := randomNonce()
		if err != nil {
			return fileState{}, err
		}
		temporary = ".write-" + nonce
	}
	f, err := s.root.OpenFile(temporary, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return fileState{}, err
	}
	info, infoErr := f.Stat()
	defer func() {
		f.Close()
		// This only unlinks our still-identical temporary leaf, never a tree.
		if temporary != name {
			if current, e := s.root.Lstat(temporary); e == nil && regular(current) && info != nil && os.SameFile(info, current) {
				_ = s.root.Remove(temporary)
			}
		}
	}()
	if infoErr != nil {
		return fileState{}, infoErr
	}
	if _, err := io.Copy(f, bytes.NewReader(data)); err != nil {
		return fileState{}, err
	}
	if err := f.Sync(); err != nil {
		return fileState{}, err
	}
	info, err = f.Stat()
	if err != nil {
		return fileState{}, err
	}
	if err := f.Close(); err != nil {
		return fileState{}, err
	}
	if err := s.check(); err != nil {
		return fileState{}, err
	}
	if previous != nil {
		if _, err := verifyFile(s.root, previous.record, previous.info); err != nil {
			return fileState{}, err
		}
		if err := s.root.Rename(temporary, name); err != nil {
			return fileState{}, err
		}
	}
	hash := sha256.Sum256(data)
	record := File{Name: name, Size: int64(len(data)), SHA256: hex.EncodeToString(hash[:]), Mode: uint32(info.Mode())}
	verified, err := verifyFile(s.root, record, info)
	return fileState{record, verified}, err
}

// Write accepts only the four fixed artifact leaves. Failed and cancelled
// renders can write their raw streams/report and seal without generated YAML.
func (s *Store) Write(name string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !artifactNames[name] {
		return fmt.Errorf("unsupported render artifact leaf %q", name)
	}
	if s.metadata.Sealed {
		return fmt.Errorf("render artifact is already sealed")
	}
	if len(data) > maxFileBytes {
		return fmt.Errorf("render artifact exceeds %d bytes", maxFileBytes)
	}
	total := int64(len(data))
	for key, value := range s.files {
		if key != name {
			total += value.record.Size
		}
	}
	if total > maxTotalBytes {
		return fmt.Errorf("render artifact inventory exceeds %d bytes", maxTotalBytes)
	}
	var previous *fileState
	if prior, ok := s.files[name]; ok {
		previous = &prior
	}
	state, err := s.writeFile(name, data, previous)
	if err == nil {
		s.files[name] = state
	}
	return err
}

func namesIn(root *os.Root, limit int) ([]fs.DirEntry, error) {
	f, err := root.Open(".")
	if err != nil {
		return nil, err
	}
	defer f.Close()
	entries, err := f.ReadDir(limit + 1)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	if len(entries) > limit {
		return nil, fmt.Errorf("cache directory has more than %d entries", limit)
	}
	return entries, nil
}

func exactNames(root *os.Root, records []File) error {
	entries, err := namesIn(root, len(artifactNames)+1)
	if err != nil {
		return err
	}
	expected := map[string]bool{MarkerName: true}
	for _, record := range records {
		expected[record.Name] = true
	}
	if len(entries) != len(expected) {
		return fmt.Errorf("cache contains missing or extra files")
	}
	for _, entry := range entries {
		if !expected[entry.Name()] {
			return fmt.Errorf("cache contains an unowned file: %s", entry.Name())
		}
		info, err := root.Lstat(entry.Name())
		if err != nil || !regular(info) {
			return fmt.Errorf("cache contains a symlink, directory or nonregular file: %s", entry.Name())
		}
	}
	return nil
}

// Seal verifies the full current directory and publishes its removable
// inventory. It is idempotent only while those same sealed bytes remain intact.
func (s *Store) Seal() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.check(); err != nil {
		return err
	}
	records := make([]File, 0, len(s.files))
	for _, state := range s.files {
		if _, err := verifyFile(s.root, state.record, state.info); err != nil {
			return err
		}
		records = append(records, state.record)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Name < records[j].Name })
	if err := exactNames(s.root, records); err != nil {
		return err
	}
	if _, err := verifyFile(s.root, s.marker.record, s.marker.info); err != nil {
		return err
	}
	if s.metadata.Sealed {
		return nil
	}
	sealed := s.metadata
	sealed.Sealed, sealed.Files = true, records
	data, err := json.Marshal(sealed)
	if err != nil {
		return err
	}
	marker, err := s.writeFile(MarkerName, append(data, '\n'), &s.marker)
	if err != nil {
		return err
	}
	s.marker, s.metadata = marker, sealed
	return nil
}

// Close releases handles only. Interrupted/unsealed caches stay untouched.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	return errors.Join(s.root.Close(), s.ns.close())
}

func readManifest(root *os.Root) (manifest, fileState, error) {
	var result manifest
	info, err := root.Lstat(MarkerName)
	if err != nil || !regular(info) || info.Size() <= 0 || info.Size() > maxMarker {
		return result, fileState{}, fmt.Errorf("cache ownership marker is missing or not a bounded regular file")
	}
	f, err := openRegular(root, MarkerName)
	if err != nil {
		return result, fileState{}, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !regular(opened) || !os.SameFile(info, opened) {
		return result, fileState{}, fmt.Errorf("cache marker changed while opening")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxMarker+1))
	if err != nil || len(data) > maxMarker {
		return result, fileState{}, fmt.Errorf("cache marker is unreadable or exceeds its bound")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&result); err != nil {
		return result, fileState{}, fmt.Errorf("invalid cache marker: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return result, fileState{}, fmt.Errorf("cache marker contains trailing data")
	}
	hash := sha256.Sum256(data)
	record := File{Name: MarkerName, Size: int64(len(data)), SHA256: hex.EncodeToString(hash[:]), Mode: uint32(opened.Mode())}
	verified, err := verifyFile(root, record, opened)
	return result, fileState{record, verified}, err
}

type inspected struct {
	entry  Entry
	root   *os.Root
	info   fs.FileInfo
	marker fileState
	files  []fileState
}

func inspect(n *namespace, name string) (_ inspected, err error) {
	result := inspected{entry: Entry{Directory: filepath.Join(n.directory, name)}}
	if !strings.HasPrefix(name, "chart-") || !nonceValid(strings.TrimPrefix(name, "chart-")) {
		return result, fmt.Errorf("directory is not a program-allocated render entry")
	}
	if err := n.check(); err != nil {
		return result, err
	}
	result.root, result.info, err = childRoot(n.base, name, false)
	if err != nil {
		return result, err
	}
	defer func() {
		if err != nil {
			result.root.Close()
		}
	}()
	metadata, marker, err := readManifest(result.root)
	if err != nil {
		return result, err
	}
	_, identity, err := directoryInfo(result.root)
	if err != nil {
		return result, err
	}
	if metadata.SchemaVersion != 1 || metadata.Product != product || !metadata.Sealed ||
		metadata.Nonce != strings.TrimPrefix(name, "chart-") || metadata.Directory != result.entry.Directory || metadata.DirectoryIdentity != identity {
		return result, fmt.Errorf("cache marker is unsealed, copied or does not match this program/directory")
	}
	if _, err := time.Parse(time.RFC3339Nano, metadata.CreatedAt); err != nil {
		return result, fmt.Errorf("invalid cache creation identity")
	}
	if len(metadata.Files) > len(artifactNames) {
		return result, fmt.Errorf("cache inventory exceeds its fixed artifact set")
	}
	seen, total := map[string]bool{}, int64(0)
	for _, record := range metadata.Files {
		digest, hashErr := hex.DecodeString(record.SHA256)
		if !artifactNames[record.Name] || seen[record.Name] || record.Size < 0 || record.Size > maxFileBytes || hashErr != nil || len(digest) != sha256.Size || hex.EncodeToString(digest) != record.SHA256 {
			return result, fmt.Errorf("cache inventory contains an invalid or repeated artifact")
		}
		seen[record.Name], total = true, total+record.Size
		if total > maxTotalBytes {
			return result, fmt.Errorf("cache inventory exceeds its byte bound")
		}
		info, err := verifyFile(result.root, record, nil)
		if err != nil {
			return result, err
		}
		result.files = append(result.files, fileState{record, info})
	}
	if err := exactNames(result.root, metadata.Files); err != nil {
		return result, err
	}
	if err := sameDirectory(n.base, name, result.info); err != nil {
		return result, err
	}
	result.entry.Owned, result.entry.Files, result.entry.CreatedAt = true, metadata.Files, metadata.CreatedAt
	result.marker = marker
	return result, nil
}

// List inspects only immediate entries in the fixed program cache namespace.
// It does not create cache directories, follow foreign directories or delete.
func List() ([]Entry, error) {
	result := []Entry{}
	n, err := openNamespace(false)
	if errors.Is(err, fs.ErrNotExist) {
		return result, nil
	}
	if err != nil {
		return nil, err
	}
	defer n.close()
	entries, err := namesIn(n.base, maxEntries)
	if err != nil {
		return nil, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	for _, entry := range entries {
		value, err := inspect(n, entry.Name())
		if err != nil {
			value.entry.Reason = err.Error()
		} else {
			value.root.Close()
		}
		result = append(result, value.entry)
	}
	return result, nil
}

// Remove deletes one verified default artifact. Every unlink is relative to the
// held entry root. Added files are never swept: the final operation is rmdir,
// which fails if an unlisted entry appeared. No recursive deletion is used.
func Remove(directory string) error {
	if !filepath.IsAbs(directory) || filepath.Clean(directory) != directory {
		return fmt.Errorf("cache removal requires one absolute managed artifact directory")
	}
	n, err := openNamespace(false)
	if err != nil {
		return err
	}
	defer n.close()
	if filepath.Dir(directory) != n.directory {
		return fmt.Errorf("refusing to remove a directory outside the default program cache")
	}
	name := filepath.Base(directory)
	value, err := inspect(n, name)
	if err != nil {
		return err
	}
	defer value.root.Close()
	checkPlacement := func() error {
		if err := n.check(); err != nil {
			return err
		}
		return sameDirectory(n.base, name, value.info)
	}
	for _, state := range append(value.files, value.marker) {
		if err := checkPlacement(); err != nil {
			return err
		}
		if _, err := verifyFile(value.root, state.record, state.info); err != nil {
			return err
		}
		if err := value.root.Remove(state.record.Name); err != nil {
			return err
		}
	}
	if err := checkPlacement(); err != nil {
		return err
	}
	if entries, err := namesIn(value.root, len(artifactNames)+1); err != nil || len(entries) != 0 {
		return fmt.Errorf("cache directory changed during removal; unlisted entries were preserved")
	}
	// Close the child handle before the empty-directory removal for platforms
	// that restrict deleting an open directory; the parent handle remains held.
	if err := value.root.Close(); err != nil {
		return err
	}
	if err := checkPlacement(); err != nil {
		return err
	}
	return n.base.Remove(name)
}
