import os as _os

# Make Dash aware that this package's directory contains static assets
# (the compiled JS bundle).  This must happen before any app.layout is built.
_PACKAGE_DIR = _os.path.dirname(_os.path.abspath(__file__))

from .PersonCard import PersonCard  # noqa: F401, E402

__version__ = "0.1.0"
__all__ = ["PersonCard"]
