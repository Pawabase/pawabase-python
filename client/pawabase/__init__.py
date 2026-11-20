"""Pawabase for Python: the client SDK.

Talk to a Pawabase environment from an app, a script or a server:

    from pawabase import Pawabase

    pb = Pawabase("https://api.example.com", "pb_pk_…")          # AsyncPawabase for asyncio
    pb.auth.sign_in_with_password(email="ada@example.com", password="…")
    posts = pb.from_("posts").eq("status", "live").order("-created_at").limit(10).execute()
    pb.storage.from_("avatars").upload("ada.png", data)
    pb.flows.invoke("welcome", email="ada@example.com")

* ``pb.from_(resource)``    records: queries, create, update, delete       (:mod:`pawabase.data`)
* ``pb.auth``               accounts, sessions, MFA, devices, API keys, organizations (:mod:`pawabase.auth`)
* ``pb.storage``            buckets, objects, signed links, image transforms (:mod:`pawabase.storage`)
* ``pb.flows`` / ``pb.routes``  call what you have defined (:mod:`pawabase.callables`)
* ``pb.bachs``             take payments with the Bachs integration         (:mod:`pawabase.bachs`)
* ``pawabase.cloud``       connect a product to Pawabase: OAuth and the Management API (:mod:`pawabase.cloud`)
* ``pb.realtime``           channels, presence, live record changes        (:mod:`pawabase.realtime`)
* ``pb.admin``              manage users with a secret key                  (:mod:`pawabase.admin`)
* ``pb.manage``             configure the environment with a secret key     (:mod:`pawabase.manage`)

"""

from __future__ import annotations

import pkgutil

# ``pawabase-functions`` installs its modules (``pawabase.functions``…) into this same package.
__path__ = pkgutil.extend_path(__path__, __name__)

from .auth import FileSessionStore, MemorySessionStore, Session, SessionStore, SignInResult
from .client import AsyncPawabase, Pawabase
from .data import Page, QueryError
from .errors import (
    AuthenticationError,
    ConfigError,
    ConflictError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    PawabaseError,
    QuotaExceededError,
    RateLimitedError,
    RealtimeError,
    ServerError,
    ValidationError,
)

__version__ = "0.12.0"
__all__ = [
    "AsyncPawabase",
    "AuthenticationError",
    "ConfigError",
    "ConflictError",
    "FileSessionStore",
    "ForbiddenError",
    "MemorySessionStore",
    "NetworkError",
    "NotFoundError",
    "Page",
    "Pawabase",
    "PawabaseError",
    "QueryError",
    "QuotaExceededError",
    "RateLimitedError",
    "RealtimeError",
    "ServerError",
    "Session",
    "SessionStore",
    "SignInResult",
    "ValidationError",
]
