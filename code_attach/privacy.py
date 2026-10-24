"""Nothing that identifies the person or the machine may leave the process: workspace path, home directory, user name."""
import getpass
import re
from pathlib import Path

from coding_agent.path_spellings import spellings

BS = chr(92)

class Scrubber:
    def __init__(self, root):
        self.root = Path(root)

    def scrub(self, text):
        """Replace the workspace path, home directory and OS user name in text that leaves this process.
        Handles plain, forward-slash and escaped-backslash (repr) spellings, case-insensitively."""
        if not isinstance(text, str):
            return text
        home = Path.home()
        for base, label in ((self.root, "<workspace>"), (home, "~")):
            pattern = "|".join(re.escape(v) for v in spellings(base))
            text = re.sub(pattern, lambda m, label=label: label, text, flags=re.IGNORECASE)
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        if len(user) >= 2:
            sep = "[" + re.escape(BS) + "/]+"
            text = re.sub("(" + sep + "(?:users|home)" + sep + ")" + re.escape(user) + "(?=" + sep + "|['\" ]|$)", lambda m: m.group(1) + "<user>", text, flags=re.IGNORECASE)
        return text
