# Changelog

## 0.2.0

- `@function` kit: `FunctionContext`, `FunctionError`, `input_fields` validation, loading helper packages.
- `pawabase` command: `login`, `link`, `init`, `whoami`, `deploy` (per environment and branch, reproducible bundle, credential check, rollback), `emulate`,
  `invoke`, `trigger`, `functions`, `deployments`, `branches`, `logs`, `test`.
- `pawabase.emulator`: run functions locally against a real deployment; everything else is forwarded.
- `pawabase.testing`: `FakeRuntime` (real SQL on SQLite) and `call`.
- `Pawabase` / `AsyncPawabase`: data, functions, events, flows, schedules, deployments, runtime over HTTP; retries reads, waits on `429`.

## 0.1.0

- First client and `deploy` command.
