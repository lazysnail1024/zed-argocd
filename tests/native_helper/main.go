// This executable is a task-owned adversarial renderer fixture, not product code.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"time"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "child" {
		marker := os.Getenv("CD_HELPER_CHILD")
		for until := time.Now().Add(60 * time.Second); time.Now().Before(until); {
			if _, err := os.Stat(os.Getenv("CD_HELPER_STOP")); err == nil {
				return
			}
			_ = os.WriteFile(marker, []byte(time.Now().UTC().Format(time.RFC3339Nano)), 0600)
			time.Sleep(30 * time.Millisecond)
		}
		return
	}
	mode := os.Getenv("CD_HELPER_MODE")
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println("v3.19.0-test")
		return
	}
	fmt.Println("renderer-started")
	fmt.Fprintln(os.Stderr, "renderer-stderr")
	if mode == "nonzero" {
		os.Exit(7)
	}
	child := exec.Command(os.Args[0], "child")
	child.Stdout = os.Stdout
	child.Stderr = os.Stderr
	if err := child.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(9)
	}
	data, _ := json.Marshal([]int{os.Getpid(), child.Process.Pid})
	_ = os.WriteFile(os.Getenv("CD_HELPER_PIDS"), data, 0600)
	if mode == "parent-exit" {
		return
	}
	if mode == "output" {
		for i := 0; i < 256; i++ {
			_, _ = os.Stdout.Write(make([]byte, 65536))
			_, _ = os.Stderr.Write(make([]byte, 65536))
		}
	}
	for until := time.Now().Add(60 * time.Second); time.Now().Before(until); {
		if _, err := os.Stat(os.Getenv("CD_HELPER_STOP")); err == nil {
			return
		}
		time.Sleep(30 * time.Millisecond)
	}
}
