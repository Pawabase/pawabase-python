# Changelog

## 0.8.0

- **Mail:** `pb.manage.mail` gains `send`, `message`, `retry`, `cancel`, `stats` and `template_usage`; `log` takes `template`, `q` and `offset`.
- **Admin:** `pb.admin.orgs.update` takes `app_metadata`, `max_members` (`None` removes the limit), `require_mfa` and `suspended`.
- **Records:** `similar(where={...})` narrows the candidates like a list read.
- **Storage:** `transform_url` takes `rotate`; its docs list the fits the server supports (`contain`, `cover`, `fill`).
- **Errors:** `error.code` is also read from a body's `code` field, so `mfa_setup_required` and similar refusals carry their code.
- **Fixed:** on the async client, a query with an empty `in_()` list crashed instead of returning an empty page.
- Docs: realtime examples use `on(None, …)` for every message and `is.false` for boolean live-query filters; `status_page.configure`
  replaces the whole setup.

## 0.7.0

- **Bot protection:** `pb.captcha.verify(pass_, action=..., remoteip=...)` checks a pass your form received (server, secret key), with
  Turnstile's answer shape plus `score` and `same_address`. `pb.captcha.protect(action)` earns a pass the way the widget does: the built-in
  check is solved here, a hosted provider's widget is shown through `get_token=`. Protected auth calls (`sign_up`, `sign_in_with_password`,
  `sign_in_with_magic_link`, `send_email_code`, `request_password_reset`, `resend_verification`, `sign_in_anonymously`) take `captcha_token=`.
- **Organizations:** `pb.auth.orgs` gains `roles`, `permissions`, `save_role`, `delete_role`, `transfer`, `update_team`, `delete_team`,
  `team_members`, `domains`, `add_domain`, `verify_domain`, `update_domain`, `remove_domain`, `set_security`, `audit`, `joinable` and `join`;
  `create_team` takes `description` and `role`.
- **Search:** `.search("red mug")` on a query (or a resource) runs full-text search over the table's search fields (`?q=`).

## 0.6.0

- **Matches what the server returns.** An agent run's `status` is `done` (not `finished`) when it completes, and `stream` yields the server's own `done`
  event (the end of the stream is marked internally, so the real event reaches you). Job `status` is `completed` (not `succeeded`).
- **`replace` is documented as it behaves:** `PUT` is handled like `update` by the server, so fields you leave out are kept, not cleared.

## 0.5.0

- **Live queries:** `pb.realtime.live(resource, filter=..., sort=...)` keeps a query's whole result up to date over a WebSocket and gives it to you
  again whenever it changes (async client; needs `pip install "pawabase[realtime]"`).

## 0.4.0

- **Agents and knowledge:** `pb.ai.agents` (`list`, `run`, `stream`, `get`, `decide`, `wait`) and `pb.ai.knowledge` (`search`, `add_document`,
  `add_file`, `document`) run the server's agents and search its knowledge bases. `stream` and `wait` work on the async client too.
- **Management:** `pb.manage.agents`, `agent_runs` (list, decide, tools), `knowledge_bases` (documents, reindex, search, Notion sync) and
  `connections` (with `test`).
- **Versions:** `pb.manage.versions` (`list`, `check`, `release`, `retire`) replaces `pb.manage.releases`; `environments.promote(dry_run=True)`.
- **Also:** bucket folders (`buckets.create_folder` / `delete_folder`), saved SQL queries (`database.saved_queries`…), the mail brand kit
  (`mail.kit` / `save_kit`) and `mail_templates.rename`, and `observability.bandwidth`.
- **Fixed:** `cache.stats()`, `observability.workers()` and `capacity()` call the project-wide routes they belong to.
- **Removed what the server no longer has:** `pb.manage.firewall`, `transformers`, `runtime`, `releases` (branches, revisions, releases,
  deployments), and the `branch=` argument on definitions.

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
