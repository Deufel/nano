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

def datastar_redirect(url):
    """Build an SSE event that tells the browser to navigate to `url`.

    Used by responses.ds_redirect() for auth-flow navigation (sign-in,
    sign-out). The browser receives an SSE patch-elements event that
    appends a <script> tag setting window.location.

    The setTimeout wrapper avoids a Firefox quirk where script-driven
    location changes replace history instead of pushing.

    This is the only intended use of script-execution from the server.
    Writes (event creation, deletion, etc.) should notify a topic and
    let the live() region re-render; they should not redirect.
    """
    # Escape so embedded quotes can't break out of the JS string.
    safe = url.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
    payload = (f"mode append\n"
               f"selector body\n"
               f"elements <script>setTimeout(() => "
               f'window.location = "{safe}")</script>')
    return event(payload, event_type="datastar-patch-elements")
