//go:build !unix && !windows

package rendercache

import (
	"fmt"
	"os"
	"runtime"
)

func nativeDirectoryIdentity(*os.File) (string, error) {
	return "", fmt.Errorf("owned render cache is unsupported on %s", runtime.GOOS)
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	return nil, fmt.Errorf("owned render cache is unsupported on %s", runtime.GOOS)
}
