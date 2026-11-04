# Vendored YAML grammar and scanner patch

[UPSTREAM.json](UPSTREAM.json) records the source and unmodified file digests; [LICENSE](LICENSE) preserves the original MIT notice. `parser.c.inc`, `schema.core.c.inc`, and the `tree_sitter` headers come from the pinned upstream revision. Changes to `scanner.c.inc` are recorded in [PATCH.diff](PATCH.diff). The `.inc` suffix prevents cgo from compiling these files twice; `binding.go` includes them.

The original serialization header occupies 10 bytes and each indentation frame 4 bytes, but the loop only checks that the current size is below 1024. At size 1022 it still writes 4 bytes, overruns the buffer, and returns 1026, triggering a Tree-sitter assertion. Simply truncating the stack to the buffer size would corrupt inputs at the supported 256-level boundary.

The local patch widens source coordinates from 16 to 32 bits while retaining the 5 MiB input limit. It uses LEB128 to encode header fields and adjacent differences across the complete indentation stack, checking the fixed ABI buffer capacity before every write. The overflow flag is thread-local and reset at the start of each Parse. Go locks the OS thread while parsing, reading the flag, and closing the tree and parser; overflow returns an error. The existing semantic depth check of 256 remains in place after parsing.

The patch does not truncate valid stacks, change YAML implicit typing or the public Go API, or introduce another module. The external `budget` consumer covers 256 levels, wide indentation, quoted and block scalars, recovery after exceeding limits, and concurrent calls. Snapshots must include the C includes, headers, license, and provenance in this directory, not just Go files.
