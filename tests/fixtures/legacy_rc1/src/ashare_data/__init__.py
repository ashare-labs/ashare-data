"""A数达, a local snapshot kernel. Importing this package never accesses a data store."""
from .model import DataError
from .storage import Store

__all__ = ["Store", "DataError"]
__version__ = "0.1.0"
