package main

import (
	"github.com/lazysnail1024/zed-argocd/internal/cli"
	"os"
)

func main() { os.Exit(cli.Run(os.Args[1:], os.Stdout, os.Stderr)) }
