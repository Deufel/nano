"""End-to-end browser tests for py_sse.

Each test loads the fixture app in real Chromium via pytest-playwright,
waits for Datastar to open the SSE stream and morph the page, then
asserts on the post-morph DOM.

Fixtures (from conftest.py): `url` (the server's URL), `server` (handle
with .capacity, .db, .url for direct server-side assertions), `capacity`
(shortcut for server.capacity). Plus `page` / `context` from pytest-playwright.
"""
import time
import pytest


# ─── helpers ──────────────────────────────────────────────────────────

def wait_text(page, selector, contains, timeout=5000):
    """Wait until selector's text contains `contains`.

    If this times out, the most likely culprit is Datastar failing to load
    from the CDN (no internet, blocked, etc). When that happens the SSE
    stream never opens, and the page stays in its initial server-rendered
    state. Surface that hint clearly in the failure.
    """
    try:
        page.wait_for_function(
            f"() => document.querySelector({selector!r})?.innerText.includes({contains!r})",
            timeout=timeout,
        )
    except Exception:
        # Check whether Datastar loaded at all.
        ds = page.evaluate("() => !!document.querySelector('script[src*=\"datastar\"]')")
        errors = page.evaluate("""() => {
            // Best-effort: console errors via a globally-installed listener
            return window.__datastarErrors || [];
        }""")
        hint = ""
        if not ds:
            hint = " (no datastar <script> tag in page)"
        else:
            # See if any data-* attributes are still raw (Datastar would have processed them)
            unprocessed = page.evaluate("() => document.querySelectorAll('[data-init]').length")
            if unprocessed > 0:
                hint = (f" — found {unprocessed} unprocessed [data-init] elements; "
                        "Datastar script likely failed to execute (CDN blocked? CORS?)")
        raise AssertionError(
            f"Timed out waiting for {selector!r} to contain {contains!r}{hint}"
        )


def value(page):
    """Current numeric counter value visible on page."""
    return int(page.locator("#value").inner_text().strip())


def mode(page):
    """Current mode tag visible in the footer (live / poll / static)."""
    return page.locator("#mode").inner_text().strip()


def viewers(page):
    """Current 'watching N' viewer count visible in the footer."""
    txt = page.locator("#viewers").inner_text().strip()
    # 'watching 3' → 3
    return int(txt.split()[-1])


# ─── tests ────────────────────────────────────────────────────────────

def test_initial_load_renders_zero(page, url):
    """A fresh page shows value 0 and the live-root wrapper."""
    page.goto(url + "/")
    page.wait_for_selector("#value")
    assert value(page) == 0
    # live-root wrapper must be present; Datastar uses it as the morph anchor.
    assert page.locator("#live-root").count() == 1


def test_sse_opens_and_updates_viewer_count(page, url, capacity):
    """After Datastar opens the SSE stream, the footer morphs to show
    'watching 1' (the one tab itself)."""
    page.goto(url + "/")
    # Wait for the first SSE frame to morph in 'watching 1'.
    wait_text(page, "#viewers", "watching 1")
    assert viewers(page) == 1
    assert mode(page) == "live"
    # Server-side capacity should agree.
    assert capacity.count("counter") == 1


def test_click_increments_value(page, url):
    """Clicking + posts to /inc, server notifies, SSE frame morphs in new value."""
    page.goto(url + "/")
    wait_text(page, "#viewers", "watching 1")  # let SSE settle
    assert value(page) == 0

    page.locator("#inc").click()
    page.wait_for_function("() => document.querySelector('#value').innerText.trim() === '1'")
    assert value(page) == 1

    page.locator("#inc").click()
    page.locator("#inc").click()
    page.wait_for_function("() => document.querySelector('#value').innerText.trim() === '3'")
    assert value(page) == 3


def test_two_tabs_see_each_others_clicks(context, url):
    """Open two tabs on /. Click in one → the other's value morphs.

    Both tabs should also see 'watching 2' shortly after the second tab
    opens, because the framework notifies on join.
    """
    a = context.new_page(); a.goto(url + "/")
    b = context.new_page(); b.goto(url + "/")
    # Both tabs morph to 'watching 2' because join-notify wakes peer streams.
    wait_text(a, "#viewers", "watching 2", timeout=8000)
    wait_text(b, "#viewers", "watching 2", timeout=8000)

    a.locator("#inc").click()
    a.wait_for_function("() => document.querySelector('#value').innerText.trim() === '1'")
    b.wait_for_function("() => document.querySelector('#value').innerText.trim() === '1'")
    assert value(a) == 1
    assert value(b) == 1


def test_topic_isolation(context, url, capacity):
    """Counter 'red' and counter 'blue' have independent state and viewers."""
    red  = context.new_page(); red.goto(url  + "/c/red")
    blue = context.new_page(); blue.goto(url + "/c/blue")
    wait_text(red,  "#viewers", "watching 1")
    wait_text(blue, "#viewers", "watching 1")

    # Increment red — blue should NOT change.
    red.locator("#inc").click()
    red.wait_for_function("() => document.querySelector('#value').innerText.trim() === '1'")
    assert value(red)  == 1
    # Give blue a moment in case a spurious notify came through.
    blue.wait_for_timeout(300)
    assert value(blue) == 0

    # Capacity counts must reflect distinct topics.
    assert capacity.count("counter.red")  == 1
    assert capacity.count("counter.blue") == 1
    assert capacity.count("counter") == 0


def test_topic_format_expansion(page, url, capacity):
    """@live(topic='counter.{id}') substitutes {id} from URL params."""
    page.goto(url + "/c/abc123")
    wait_text(page, "#viewers", "watching 1")
    # The footer's #topic span should show the expanded topic name.
    assert page.locator("#topic").inner_text().strip() == "counter.abc123"
    assert capacity.count("counter.abc123") == 1


def test_degradation_third_viewer_polls(context, url):
    """soft_cap=2: viewers 1-2 get SSE, viewer 3 gets data-on-interval."""
    a = context.new_page(); a.goto(url + "/")
    b = context.new_page(); b.goto(url + "/")
    # Wait for join-notify to propagate so both tabs see 'watching 2'.
    # This also confirms server-side count reached 2 before viewer 3 arrives.
    wait_text(a, "#viewers", "watching 2", timeout=8000)

    c = context.new_page(); c.goto(url + "/")
    c.wait_for_load_state("domcontentloaded")
    c.wait_for_timeout(300)
    root_attrs = c.evaluate("""() => {
        const r = document.getElementById('live-root');
        return Object.fromEntries(Array.from(r.attributes).map(a => [a.name, a.value]));
    }""")
    attr_names = set(root_attrs.keys())
    assert any(n.startswith("data-on-interval") for n in attr_names), \
        f"viewer 3 should be polled, attrs={attr_names}"
    assert "data-init" not in attr_names, \
        f"viewer 3 should NOT have data-init, attrs={attr_names}"
    assert mode(c) == "poll"


def test_degradation_fifth_viewer_static(context, url):
    """hard_cap=4: viewers 5+ get neither data-init nor data-on-interval."""
    # Open 4 SSE viewers; the join-notify lets each waiting tab confirm
    # the new count, so we can safely sequence the opens by waiting on the
    # first tab's footer text.
    a = context.new_page(); a.goto(url + "/")
    wait_text(a, "#viewers", "watching 1")
    b = context.new_page(); b.goto(url + "/")
    wait_text(a, "#viewers", "watching 2")
    c = context.new_page(); c.goto(url + "/")
    wait_text(a, "#viewers", "watching 3")
    d = context.new_page(); d.goto(url + "/")
    wait_text(a, "#viewers", "watching 4")

    # Viewer 5 → static (count >= hard_cap=4).
    static_page = context.new_page()
    static_page.goto(url + "/")
    static_page.wait_for_load_state("domcontentloaded")
    static_page.wait_for_timeout(300)
    root_attrs = static_page.evaluate("""() => {
        const r = document.getElementById('live-root');
        return Object.fromEntries(Array.from(r.attributes).map(a => [a.name, a.value]));
    }""")
    attr_names = set(root_attrs.keys())
    assert "data-init" not in attr_names
    assert not any(n.startswith("data-on-interval") for n in attr_names), \
        f"viewer 5 should be static (no transport attr), attrs={attr_names}"
    assert mode(static_page) == "static"


def test_live_restore_after_capacity_drops(context, url, server):
    """A polled viewer's next refetch finds capacity below soft_cap and
    morphs its mode tag back to 'live'.

    Important framework behavior: when a polled viewer's data-on-interval
    timer fires, Datastar issues a GET with SSE Accept headers, which the
    framework treats as a real SSE stream open. So the polled viewer
    eventually has its own capacity slot too. We can't expect capacity to
    drop to 0 after closing a and b — c still holds a slot. We just need
    it below soft_cap so c's next render returns 'live' instead of 'poll'.
    """
    a = context.new_page(); a.goto(url + "/")
    wait_text(a, "#viewers", "watching 1")
    b = context.new_page(); b.goto(url + "/")
    wait_text(a, "#viewers", "watching 2", timeout=8000)

    c = context.new_page(); c.goto(url + "/")
    c.wait_for_load_state("domcontentloaded")
    c.wait_for_timeout(300)
    attrs_polled = c.evaluate("""() => Object.fromEntries(
        Array.from(document.getElementById('live-root').attributes).map(a => [a.name, a.value]))""")
    assert any(n.startswith("data-on-interval") for n in attrs_polled), \
        f"viewer 3 should be polled, attrs={list(attrs_polled.keys())}"
    assert mode(c) == "poll"

    # Close a, b and fire notifies in a loop to flush their dead sockets.
    # c keeps a slot via its own polling-turned-SSE stream, so we expect
    # capacity to settle somewhere ≥ 1, not 0. We just need it < soft_cap=2.
    a.close()
    b.close()
    deadline = time.time() + 5
    while server.capacity.count("counter") > 1 and time.time() < deadline:
        server.db.changes.notify("counter")
        time.sleep(0.3)
    assert server.capacity.count("counter") < 2, (
        f"capacity didn't drop below soft_cap after closing a,b: "
        f"{server.capacity.count('counter')}"
    )

    # c is polling; its next refetch within max_poll_ms=2000 should see
    # capacity below soft_cap and morph mode to 'live'.
    c.wait_for_function(
        "() => document.querySelector('#mode')?.innerText.trim() === 'live'",
        timeout=5000)
    assert mode(c) == "live"
