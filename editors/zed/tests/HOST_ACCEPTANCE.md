# Actual Zed host acceptance

This is a manual evidence procedure, not an automated success check. Copy
`fixtures/host-smoke` into an isolated workspace. Fix one Zed version and display
backend; start Zed with an isolated user-data directory and foreground logs.
Install the candidate dev extension through Zed. Record the actual loaded WASM
and launched native path, SHA-256, product/version and source commit. A ZIP that
Zed rebuilds remains `source-dev-host`, not `release-package-host`.

Record each case as pass, fail or blocked:

1. `fresh-activation`: no old window/server is reused; candidate loads and starts.
2. `diagnostic`: app.yaml shows only the expected unresolved-project diagnostic.
3. `repair`: replace missing with team; old diagnostic clears and final bytes
   equal expected-app.yaml.txt. Preserve the original fixture in the evidence.
4. `navigation`: navigate project team from app.yaml to project.yaml name team.
5. `edit`: invoke a completion or code action, apply it through Zed and retain
   before/after bytes plus screenshot of the offered operation.
6. `ordinary-yaml`: ordinary.yaml has no Argo CD domain diagnostic.
7. `restart-cache`: restart the isolated instance; verify the same intended
   adapter/native version and bytes are active and repeat diagnostic/repair.
8. `cleanup`: close Zed and verify its owned native process exited; remove the
   isolated user data only after logs/screenshots/final diff are retained.

Use host-smoke.schema.json as the record format. Evidence must identify the
operator, time, exact candidate, display and each observation. Missing loaded-byte
identity, missing cases or blocked observations cannot yield a passed result.
Windows historical-cache activation is additionally required on Windows.
Automating this procedure requires ten clean repetitions and detection of an
activation or diagnostic defect before adding a permanent host workflow.
