"""Pawabase for Python: the client, the function kit, the emulator and the ``pawabase`` command.

This package talks to a Pawabase installation only through its public API; it contains no server runtime.

* :mod:`pawabase.functions` ``@function``, the context a function receives, loading and input validation
* :mod:`pawabase.client`    ``Pawabase`` / ``AsyncPawabase``: data, functions, events, flows, deployments
* :mod:`pawabase.testing`   ``FakeRuntime`` and ``call`` for unit tests
* :mod:`pawabase.emulator`  ``pawabase emulate``
* :mod:`pawabase.cli`       ``pawabase deploy | emulate | invoke | trigger | logs …``
"""

from .client import AsyncPawabase, Pawabase, PawabaseError
from .functions import FunctionContext, FunctionError, function

__all__ = ["AsyncPawabase", "FunctionContext", "FunctionError", "Pawabase", "PawabaseError", "function"]
__version__ = "0.2.0"
