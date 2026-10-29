# Changelog

## 0.3.0

- **Split into two packages.** `pawabase` (this repository, `client/`) is the client SDK; the function kit and the `pawabase` command
  moved to `pawabase-functions` ([Pawabase/pawabase-functions-python](https://github.com/Pawabase/pawabase-functions-python)). Both
  install into the same `pawabase` namespace, so imports do not change.
- Client: records with a query builder (`pb.from_`), auth (sessions, MFA, devices, user API keys, organizations), storage (public and
  transform URLs, signed URLs, share links), functions (`invoke`, and `request` for HTTP functions), flows, routes, realtime (async).
- `pb.manage` (the management plane, hand-written: data, automation, files, mail, access, observability, lifecycle) and `pb.admin`
  (user administration), with a secret key, scoped to one environment.

## 0.2.0

- `@function` kit: `FunctionContext`, `FunctionError`, `input_fields` validation, loading helper packages.
- `pawabase` command: `login`, `link`, `init`, `whoami`, `deploy` (per environment and branch, reproducible bundle, credential check, rollback), `emulate`,
  `invoke`, `trigger`, `functions`, `deployments`, `branches`, `logs`, `test`.
- `pawabase.emulator`: run functions locally against a real deployment; everything else is forwarded.
- `pawabase.testing`: `FakeRuntime` (real SQL on SQLite) and `call`.
- `Pawabase` / `AsyncPawabase`: data, functions, events, flows, schedules, deployments, runtime over HTTP; retries reads, waits on `429`.

## 0.1.0

- First client and `deploy` command.
