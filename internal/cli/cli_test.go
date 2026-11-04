package cli

import (
	"bytes"
	"testing"
)

func TestInvalidCommandsFail(t *testing.T) {
	for _, args := range [][]string{{"lint-official"}, {"check", "--format", "xml"}, {"check", "--unsupported"}, {"version", "surprise"}} {
		var out, errOut bytes.Buffer
		if got := Run(args, &out, &errOut); got != 2 {
			t.Fatalf("%v: %d", args, got)
		}
	}
}
