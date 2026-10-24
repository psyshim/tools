"""What the host is allowed to render. Everything is bounded, stripped of control characters and copied: the host never
holds a reference to server-controlled objects, and untrusted text still needs escaping at the rendering boundary (`html_text`)."""
import html
from dataclasses import dataclass, field

MAX_TEXT = 20000
MAX_DEPTH = 6
MAX_ITEMS = 100

def clean_text(value, limit=MAX_TEXT):
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127 and not 0x80 <= ord(ch) < 0xA0 and not 0xD800 <= ord(ch) <= 0xDFFF))
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"

def clean_data(value, depth=0):
    """Deep copy with strings cleaned, containers bounded; anything exotic becomes its cleaned string form."""
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (list, tuple)):
        return [clean_data(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {clean_text(k, 200): clean_data(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    return clean_text(value)

@dataclass(frozen=True)
class SafeDisplayEvent:
    task_id: str
    event_id: int          # 0 for synthetic events (gap markers)
    type: str              # a protocol event type, or "gap"
    data: dict = field(default_factory=dict)
    terminal: bool = False
    synthetic: bool = False

    @property
    def text(self):
        """Best single line/paragraph to show for this event (cleaned, not escaped)."""
        d = self.data
        for key in ("text", "detail", "message"):
            if isinstance(d.get(key), str) and d[key]:
                return d[key]
        if self.type == "state":
            return str(d.get("state", ""))
        if self.type == "proposal_completed":
            return "proposal ready (nothing was changed): " + ", ".join(str(f.get("path")) for f in d.get("files", []) if isinstance(f, dict))
        if self.type == "mutation_applied":
            return "change applied"
        if self.type == "gap":
            return "some earlier activity is no longer available"
        return self.type

    @property
    def html_text(self):
        """`text` escaped for insertion into HTML."""
        return html.escape(self.text, quote=True)
