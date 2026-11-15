package rendercache

import (
	"fmt"
	"os"
	"syscall"
)

func nativeDirectoryIdentity(file *os.File) (string, error) {
	var info syscall.ByHandleFileInformation
	if err := syscall.GetFileInformationByHandle(syscall.Handle(file.Fd()), &info); err != nil {
		return "", err
	}
	return fmt.Sprintf("windows:%x:%x:%x", info.VolumeSerialNumber, info.FileIndexHigh, info.FileIndexLow), nil
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	return root.Open(name)
}
