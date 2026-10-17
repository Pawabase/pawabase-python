# pawabase-python

The Python **client SDK** for [Pawabase](https://github.com/Pawabase/pawabase), in [`client/`](client): records, auth, storage, functions, flows, realtime,
user administration (`pb.admin`) and environment management (`pb.manage`). Blocking and async.

The function kit and the `pawabase` command are a separate package in their own repository, `pawabase-functions` (`../python-functions` here). It depends on
this one, and both install into the same `pawabase` namespace, so an app that only calls Pawabase installs just `pawabase`.

The JavaScript counterparts are separate packages too: `@pawabase/client` (`../js`) and `@pawabase/functions` (`../js-functions`).

```bash
pip install -e ./client
python -m pytest client/tests
```

The management and admin clients are written by hand against the server's routes; `client/tests/test_manage_admin.py` pins their paths and bodies.

BSD 3-Clause licensed.
