# Changelog

## 0.3.1

- **Profile pictures:** `pb.auth.set_avatar(image)` and `pb.auth.remove_avatar()`. The server stores the picture in a public bucket and sets `avatar_url`.
- **Optional paging:** `.paginate(False)` on a query, or `list(..., paginate=False)`, returns every match in one response (up to 5000). `Page.page` and
  `Page.per_page` are `None` for such a result.
- **AI gateway and vector search:** `pb.ai` (chat, streaming, embeddings, models), `similar()` on records, and `pb.manage.ai`. Error codes are read from
  OpenAI-style error bodies.
- **Machine identities:** machine sign-in with `client_credentials`, `pb.as_machine`, and `pb.admin.machines`.
- **Privacy requests and user activity** in `pb.admin`: `users.export_data()` and `users.erase_data(confirm=…)` (data-access and erasure
  requests; the export covers what the auth service holds, not your own tables), `privacy.requests()`, and `users.activity()` /
  `users.timeline()` (sign-ins, account changes and requests, per person).
- **Removed `pb.functions` and `pb.manage.functions`.** The server no longer has `/functions/v1` or the functions management routes;
  use flows (`pb.flows`) and routes (`pb.routes`). The `pawabase-functions` package is not compatible with the current server.

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
