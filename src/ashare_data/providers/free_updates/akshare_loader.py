"""Load reviewed SDK modules without eagerly importing every optional JS engine.

Only used in the disposable provider worker. The installed distribution remains
unchanged. Missing dependencies fail the affected module, never produce data.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys
import types

from ashare_data.providers.free_updates import catalog


class ReviewedAKShare:
    def __init__(self):
        distribution = importlib.metadata.distribution("akshare")
        self.version = distribution.version
        self.root = Path(distribution.locate_file("akshare")).resolve()
        self.manifest = json.loads(Path(__file__).with_name("akshare-reviewed-modules.json").read_text())
        if "akshare" not in sys.modules:
            package = types.ModuleType("akshare")
            package.__path__ = [str(self.root)]
            package.__file__ = str(self.root / "__init__.py")
            package.__package__ = "akshare"
            package.__version__ = self.version
            package.__getattr__ = self.__getattr__
            sys.modules["akshare"] = package

    def __getattr__(self, name):
        entry = self.manifest["functions"].get(name)
        if entry is None:
            raise AttributeError("function not in reviewed SDK manifest: " + name)
        module = importlib.import_module("akshare." + entry["path"][:-3].replace("/", "."))
        return getattr(module, name)
