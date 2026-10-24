"""Moved to coding_service.providers (the service owns it); this alias keeps older imports and monkeypatches working."""
import sys

from coding_service import providers as _moved

sys.modules[__name__] = _moved
