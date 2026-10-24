"""Moved to coding_service.loop (the service owns it); this alias keeps older imports and monkeypatches working."""
import sys

from coding_service import loop as _moved

sys.modules[__name__] = _moved
