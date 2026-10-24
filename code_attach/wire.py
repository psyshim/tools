"""Moved to coding_service.wire (the service owns it); this alias keeps older imports and monkeypatches working."""
import sys

from coding_service import wire as _moved

sys.modules[__name__] = _moved
