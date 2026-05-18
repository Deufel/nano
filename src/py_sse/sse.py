"""SSE wire format. Per the HTML spec (whatwg, section 9.2.5):
  - UTF-8 only
  - lines split by LF (or CRLF or CR)
  - blank line dispatches an event
  - fields: event, data, id, retry; ':' prefix = comment

The framework writes the bytes; this module just builds the strings.
"""

def event(data, event_type=None, event_id=None):
    """Build an SSE event string. data may contain newlines (one
    `data: ` per line per spec). Always terminated by a blank line."""
    lines = []
    if event_type: lines.append(f"event: {event_type}")
    if event_id is not None: lines.append(f"id: {event_id}")
    for ln in str(data).split("\n"):
        lines.append(f"data: {ln}")
    lines.append("")  # blank line dispatches
    lines.append("")
    return "\n".join(lines)

def keepalive():
    "A comment line. Used to keep TCP/proxies from killing idle connections."
    return ":\n\n"

def datastar_patch_elements(html_str):
    "Build a `datastar-patch-elements` event carrying the given HTML."
    return event(f"elements {html_str}", event_type="datastar-patch-elements")
