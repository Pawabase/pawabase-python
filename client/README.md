# pawabase

The Python client SDK for [Pawabase](https://github.com/Pawabase/pawabase): records, auth, storage, flows, realtime, user administration
and environment management, for apps, scripts and servers. Blocking and `asyncio` clients with the same methods.

```bash
pip install pawabase                     # httpx only
pip install "pawabase[realtime]"         # + websockets, for live channels
```


```python
from pawabase import Pawabase

pb = Pawabase("https://api.example.com", "pb_pk_…")      # AsyncPawabase for asyncio: the same methods, awaited
```

A **publishable** key (`pb_pk_…`) is what an app holds: policies decide what it can do, and a signed-in user's token goes on top. A **secret** key
(`pb_sk_…`) belongs on servers only: it bypasses policies and unlocks `pb.admin` and `pb.manage`.

## Records

```python
posts = pb.from_("posts")

page = posts.eq("status", "live").gte("views", 10).order("-created_at").select("id", "title").expand("author").per_page(20).execute()
page.data, page.total, page.has_next

for post in posts.eq("status", "live").iterate():          # every row, page after page
    ...
posts.where(status="live", views={"gte": 10}).all()          # a list
posts.eq("slug", "hello").single()                           # NotFoundError if none
posts.eq("status", "draft").count()

row = posts.create(title="Hello")
posts.get(row["id"], expand=["author"])
posts.update(row["id"], title="Hello, world")                # PATCH
posts.delete(row["id"])
posts.create_many(rows)                                      # failures are collected, not raised
```

Operators: `eq neq gt gte lt lte like ilike in_ not_in is_ is_not`. The server applies **one filter per field**; a second filter on a field raises `QueryError`
before anything is sent, instead of silently dropping one. `eq(col, None)` and `eq(col, True)` become `is`, because `eq.false` matches true rows on the server.

## Accounts

```python
pb.auth.sign_up(email="ada@example.com", password="…", name="Ada")
result = pb.auth.sign_in_with_password(email="ada@example.com", password="…")
if result.status == "mfa_required":
    result = result.verify(input("Code: "))
pb.auth.get_user(); pb.auth.update_user(name="Ada L.")
pb.auth.sign_out()                                           # scope="global" signs out everywhere
```

The session is kept in memory (pass `session_store=FileSessionStore("~/.myapp/session.json")` to stay signed in between runs), its token goes on every
request, and an expiring or rejected token is renewed with the refresh token. Also: magic links, social sign-in (`oauth_url`, `exchange_code`), email
verification, password reset, linked identities, `pb.auth.mfa` (authenticator app, emailed codes, recovery codes), `pb.auth.sessions`, `pb.auth.devices`,
`pb.auth.api_keys` (keys a user makes for themselves), `pb.auth.machine` and `pb.as_machine(...)` (a service or AI agent signing in with its client id and key), and `pb.auth.orgs` (organizations, members, invitations, teams).

More ways in: guest sessions (`sign_in_anonymously()`, then `upgrade_anonymous(email=…, password=…)` keeps what the guest made), emailed codes
(`send_email_code(email=…)` then `verify_email_code(email=…, code=…)`), and passkeys (`pb.auth.passkeys`: `register_options` / `register_verify` and
`authenticate_options` / `authenticate_verify` carry the WebAuthn ceremony; the device does the signing). `issue_token("billing")` trades the session for
a short-lived token for another service, and `jwks()` returns the public keys a server checks such tokens with.

A web server acts as the visiting user without sharing a connection's credentials:

```python
with pb.as_user(request_token) as user_client:               # same connection pool, this user's token
    user_client.from_("orders").execute()
```

## Files

```python
avatars = pb.storage.from_("avatars")
avatars.upload("ada.png", open("ada.png", "rb"), content_type="image/png")
data = avatars.download("ada.png")
avatars.list(prefix="2026/"); list(avatars.list_all())
avatars.public_url("ada.png")                                # public buckets: no key needed
avatars.transform_url("ada.png", width=200, height=200, fit="cover", format="webp")
avatars.create_signed_url("report.pdf", expires_in=600)      # a temporary link anyone can read
avatars.create_signed_upload_url("in/x.png", max_bytes=1_000_000)   # a browser uploads straight to storage
avatars.create_share_link("report.pdf", expires_in=3600)     # for people with no account (secret key)
```

## Flows and routes

```python
pb.flows.invoke("welcome", email="ada@example.com")
pb.routes.post("orders/42/pay", method="card")
```

## Realtime

```python
async with AsyncPawabase(url, key) as pb:
    room = pb.realtime.channel("chat:lobby", presence={"name": "Ada"})
    room.on("message", lambda m: print(m["payload"]))
    await room.subscribe()
    await room.send("message", {"text": "hello"})
    async for change in pb.realtime.channel("resource:posts"):   # live record changes, as you may read them
        print(change["event"], change["payload"]["record"])
```

Publishing, presence and history also work over plain HTTP on the blocking client: `pb.realtime.publish(...)`, `.presence(channel)`, `.history(channel)`.

Without a socket: `pb.realtime.sse(["chat:lobby"])` listens over Server-Sent Events (a generator on the sync client, an async generator on the async one), and
`pb.realtime.publish(...)` sends. Channel rules and dashboards are managed with `pb.manage.realtime_channels` and `pb.manage.dashboards`.

## Administering users and the environment (secret key)

```python
admin = Pawabase(url, "pb_sk_…", environment="production")

admin.admin.users.create(email="ada@example.com", password="…", roles=["editor"])
admin.admin.users.update(user_id, disabled=True)
admin.admin.orgs.add_member("acme", email="ada@example.com", role="admin")

# What a person did (sign-ins, account changes and requests), and data requests about them
admin.admin.users.activity(user_id)                              # per-day counts by kind, plus a summary
admin.admin.users.timeline(user_id, type="account", limit=20)    # newest first; pass next_before as before=
admin.admin.users.export_data(user_id)                           # everything the auth service holds about them (JSON)
admin.admin.users.erase_data(user_id, confirm="ada@example.com") # final: deletes them and clears what could identify them
admin.admin.privacy.requests()                                   # the exports and erasures handled so far

admin.manage.secrets.set("STRIPE_KEY", "sk_live_…")
admin.manage.firewall.ban("203.0.113.7", seconds=3600)
admin.manage.jobs.replay(job="send_mail", since_minutes=60, dry_run=True)
admin.manage.observability.logs(q="timeout", minutes=15)
admin.manage.backups.snapshot("before-launch")
admin.manage.environment("staging").flows.list()      # the same API for another environment the key may manage
```

`pb.admin` administers the people who sign in to your application: `users`, `roles`, `orgs`, `invitations`, `sessions`, `api_keys`, `machines`, `devices`, `insight`
(events, statistics, identity backup, hosted pages). `pb.manage` is the environment's management plane, in four families:

| Family | Services |
| --- | --- |
| Data | `resources`, `schema`, `policies`, `schemas`, `transformers`, `routes`, `database`, `runtime` |
| Automation | `flows`, `schedules`, `subscriptions`, `events`, `webhooks`, `inbound_hooks`, `jobs` |
| Files and mail | `buckets`, `cache`, `mail`, `mail_templates` |
| Access | `keys`, `secrets`, `domains`, `firewall` |
| Observe | `observability`, `alarms`, `status_page`, `health` (service health, plan usage, audit) |
| Lifecycle | `environments` (previews, promote, blueprints), `releases` (branches, revisions, API versions), `backups` (backups, snapshots, restore) |

Definitions (`flows`, `policies`, `webhooks`…) share `list`, `get_one`, `create`, `replace` and `remove`, and take `branch=` to work on a feature branch.
Both are scoped to the client's environment; a key manages only its own.

## Errors and retries

Every failure raises `PawabaseError` (`.status_code`, `.code`, `.message`, `.details`, `.request_id`) or a subclass: `AuthenticationError` (401), `ForbiddenError`
(403), `QuotaExceededError` (403, the plan's limit: `details` has `limit`, `allowed`, `used`), `NotFoundError`, `ConflictError` (409), `ValidationError` (400/422),
`RateLimitedError`, `ServerError`, `NetworkError`. Reads are retried on connection errors and `502/503/504`; a `429` waits as the gateway asks.

BSD 3-Clause licensed.
