package cli

import (
	"fmt"
	"os/exec"
	"syscall"
	"unsafe"
)

// Start suspended, assign the process to a kill-on-close job, then resume it.
// This prevents descendants escaping between process start and job assignment.
func prepareCommand(cmd *exec.Cmd) (func() error, func() error, error) {
	kernel := syscall.NewLazyDLL("kernel32.dll")
	job, _, e := kernel.NewProc("CreateJobObjectW").Call(0, 0)
	if job == 0 {
		return nil, nil, e
	}
	type basicLimit struct {
		ProcessTime, JobTime int64
		Flags                uint32
		Min, Max             uintptr
		Active               uint32
		Affinity             uintptr
		Priority, Scheduling uint32
	}
	type ioCounters struct{ ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes uint64 }
	type extendedLimit struct {
		Basic                                                      basicLimit
		IO                                                         ioCounters
		ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory uintptr
	}
	info := extendedLimit{}
	info.Basic.Flags = 0x2000 // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	ok, _, e := kernel.NewProc("SetInformationJobObject").Call(job, 9, uintptr(unsafe.Pointer(&info)), unsafe.Sizeof(info))
	if ok == 0 {
		syscall.CloseHandle(syscall.Handle(job))
		return nil, nil, e
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000004} // CREATE_SUSPENDED
	closeJob := func() error { return syscall.CloseHandle(syscall.Handle(job)) }
	resume := func() error {
		const processSetQuota = 0x0100
		const processTerminate = 0x0001
		const processSuspendResume = 0x0800
		handle, e := syscall.OpenProcess(processSetQuota|processTerminate|processSuspendResume, false, uint32(cmd.Process.Pid))
		if e != nil {
			return e
		}
		defer syscall.CloseHandle(handle)
		ok, _, e := kernel.NewProc("AssignProcessToJobObject").Call(job, uintptr(handle))
		if ok == 0 {
			return e
		}
		status, _, _ := syscall.NewLazyDLL("ntdll.dll").NewProc("NtResumeProcess").Call(uintptr(handle))
		if status != 0 {
			return fmt.Errorf("resume job process failed: NTSTATUS %x", status)
		}
		return nil
	}
	return resume, closeJob, nil
}
