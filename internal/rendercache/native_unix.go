//go:build unix

package rendercache

import (
	"fmt"
	"os"
	"syscall"
)

func nativeDirectoryIdentity(file *os.File) (string, error) {
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", fmt.Errorf("directory filesystem identity is unavailable")
	}
	return fmt.Sprintf("unix:%x:%x", stat.Dev, stat.Ino), nil
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	// A race replacing a checked regular file with a FIFO must not block cache
	// inspection. The caller verifies the opened file and its path identity.
	return root.OpenFile(name, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW, 0)
}
