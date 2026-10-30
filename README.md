# pawabase-python

The Python **client SDK** for [Pawabase](https://github.com/Pawabase/pawabase), in [`client/`](client): records, auth, storage, flows, realtime,
user administration (`pb.admin`) and environment management (`pb.manage`). Blocking and async.

The function kit and the `pawabase` command (`pawabase-functions`, `../python-functions`) are for a Functions feature the server no longer has, so they
do not work with the current server. The JavaScript counterpart is `@pawabase/client` (`../js`).

```bash
pip install -e ./client
python -m pytest client/tests
```

The management and admin clients are written by hand against the server's routes; `client/tests/test_manage_admin.py` pins their paths and bodies.

BSD 3-Clause licensed.
