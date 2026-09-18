"""Test bootstrap.

The app package imports flask at module level, but these tests only need the
pure service/db layers (no Flask installed in this environment). A minimal
flask stub satisfies the import; db.py's own flask uses are already guarded by
try/except ImportError.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_flask_stub = types.ModuleType("flask")


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Dummy()

    def __getattr__(self, name):
        return _Dummy()


_flask_stub.Blueprint = _Dummy
_flask_stub.Flask = _Dummy
_flask_stub.render_template = lambda *args, **kwargs: ""
sys.modules.setdefault("flask", _flask_stub)
