//go:build !windows

package cli

import (
	"os/exec"
	"syscall"
)

// A separate process group owns descendants even after the direct child exits.
func prepareCommand(cmd *exec.Cmd) (func() error, func() error, error) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	kill := func() error {
		if cmd.Process == nil {
			return nil
		}
		e := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if e == syscall.ESRCH {
			return nil
		}
		return e
	}
	return func() error { return nil }, kill, nil
}
