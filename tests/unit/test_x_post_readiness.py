"""Offline checks for Home hydration, context ownership and safe composer scope."""

import json
from types import SimpleNamespace

import pytest

from camoufox_pm.actions.x_post import XPostWorker


class Element:
    def __init__(
        self, *, testid=None, label=None, role=None, editable=False, visible=True,
        enabled=True, area="home", text="", tag="div", placeholder=None,
    ):
        self.attrs = {
            "tag": tag, "role": role, "data_testid": testid, "aria_label": label,
            "contenteditable": "true" if editable else None, "placeholder": placeholder,
        }
        self.visible, self.editable, self.enabled = visible, editable, enabled
        self.area, self.text, self.container = area, text, None
        self.clicks = 0
        self.on_click = None


class Locator:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index):
        return Locator(self.nodes[index:index + 1])

    async def count(self):
        return len(self.nodes)

    async def is_visible(self):
        return bool(self.nodes and self.nodes[0].visible)

    async def is_enabled(self, **_):
        return bool(self.nodes and self.nodes[0].enabled)

    async def is_editable(self, **_):
        return bool(self.nodes and self.nodes[0].editable)

    async def fill(self, text):
        self.nodes[0].text = text

    async def text_content(self):
        return self.nodes[0].text if self.nodes else None

    async def get_attribute(self, name, **_):
        return "/demo" if name == "href" else self.nodes[0].attrs.get(name)

    async def evaluate(self, script, **_):
        assert "innerHTML" not in script and "innerText" not in script
        return dict(self.nodes[0].attrs)

    async def click(self):
        node = self.nodes[0]
        node.clicks += 1
        if node.on_click:
            node.on_click()

    def locator(self, selector):
        node = self.nodes[0] if self.nodes else None
        if selector == XPostWorker.composer_container_path:
            return Locator([node.container] if node and node.container else [])
        if selector.startswith("xpath=ancestor::"):
            return Locator([node] if node and node.area in {"article", "dialog", "dm", "search"} else [])
        if selector == ", ".join(XPostWorker.post_button_selectors):
            return Locator(node.buttons if node else [])
        raise AssertionError(f"unexpected container selector: {selector}")


class Page:
    def __init__(self, *, url="https://x.com/home", authenticated=True, login=False, challenge=False):
        self.url = url
        self.authenticated, self.login, self.challenge = authenticated, login, challenge
        self.elements = []
        self.gotos = []
        self.closed = False
        self.redirect = None
        self.error = ""
        self.title_error = False
        self.page_title = "Home / X"

    def is_closed(self):
        return self.closed

    async def title(self):
        if self.title_error:
            raise RuntimeError("detached page")
        return self.page_title

    async def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        self.url = self.redirect or url

    def add_editor(self, *, button=True, **kwargs):
        editor = Element(**kwargs)
        self.elements.append(editor)
        if button:
            editor.container = SimpleNamespace(buttons=[Element(testid="tweetButtonInline", tag="button")])
        return editor

    def locator(self, selector):
        if selector == XPostWorker.login_selector:
            return Locator([Element()] if self.login else [])
        if selector == XPostWorker.challenge_selector:
            return Locator([Element()] if self.challenge else [])
        if selector == XPostWorker.authenticated_selector:
            return Locator([Element()] if self.authenticated else [])
        if selector == XPostWorker.profile_link_selector:
            return Locator()
        if selector == XPostWorker.error_selector:
            return Locator([Element(text=self.error)] if self.error else [])
        if selector in XPostWorker.composer_selectors:
            nodes = [e for e in self.elements if e.area != "outside"]
            if "aria-label" in selector:
                nodes = [e for e in nodes if e.attrs["aria_label"] == "Post text" and e.editable]
            elif "data-testid*=" in selector:
                nodes = [e for e in nodes if "tweetTextarea" in (e.attrs["data_testid"] or "") and e.editable]
            else:
                nodes = [e for e in nodes if e.attrs["data_testid"] == "tweetTextarea_0"]
                if "contenteditable" in selector:
                    nodes = [e for e in nodes if e.editable]
            return Locator(nodes)
        if selector == XPostWorker.composer_selector:
            return Locator([e for e in self.elements if e.attrs["data_testid"] == "tweetTextarea_0"])
        if selector == "[contenteditable='true']":
            return Locator([e for e in self.elements if e.editable])
        if selector == "div[role='textbox']":
            return Locator([e for e in self.elements if e.attrs["role"] == "textbox"])
        if selector == "[aria-label='Post text']":
            return Locator([e for e in self.elements if e.attrs["aria_label"] == "Post text"])
        if selector == "article":
            return Locator([e for e in self.elements if e.area == "article"])
        if selector == XPostWorker.candidate_selector:
            return Locator(self.elements)
        raise AssertionError(f"unexpected page selector: {selector}")


class Context:
    def __init__(self, pages):
        self.pages = pages
        self.created = []

    async def new_page(self):
        page = Page(url="about:blank")
        self.pages.append(page)
        self.created.append(page)
        return page


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = None
        self.sleeps = 0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def worker(manager=None, *, clock=None):
    clock = clock or Clock()
    return XPostWorker(
        manager or SimpleNamespace(), home_ready_timeout=1.2,
        confirmation_timeout=0, poll_interval=0.4, sleep=clock.sleep, clock=clock,
    )


def editor(page, **kwargs):
    return page.add_editor(testid="tweetTextarea_0", role="textbox", editable=True, **kwargs)


@pytest.mark.asyncio
async def test_selects_x_page_in_persistent_context_instead_of_first_blank():
    blank, x_page = Page(url="about:blank"), Page()
    context = Context([blank, x_page])
    selected, diagnostics = await XPostWorker._select_page(context)
    assert selected is x_page and not context.created
    assert diagnostics["pages"] == 2
    assert diagnostics["initial_url"] == "https://x.com/home"


@pytest.mark.asyncio
async def test_prefers_existing_home_page_over_other_x_page_and_closed_page():
    closed, other, home = Page(), Page(url="https://x.com/messages"), Page()
    closed.closed = True
    selected, _ = await XPostWorker._select_page(Context([closed, other, home]))
    assert selected is home


@pytest.mark.asyncio
async def test_blank_context_creates_controlled_page_then_waits_for_home_editor():
    context = Context([Page(url="about:blank"), Page(url="https://example.com")])
    selected, diagnostics = await XPostWorker._select_page(context)
    assert selected is context.created[0] and selected.url == "about:blank"
    clock = Clock()
    # Drive through the actual navigation path, with a disabled button so this
    # readiness test can never dispatch an external action even in its fake.
    clock.on_sleep = lambda _: editor(selected) if not selected.elements else None
    original_add = selected.add_editor
    def add_disabled(**kwargs):
        node = original_add(**kwargs)
        node.container.buttons[0].enabled = False
        return node
    selected.add_editor = add_disabled
    result = await worker(clock=clock)._post(selected, "hello", page_selection=diagnostics)
    assert result.reason == "POST_BUTTON_NOT_READY"
    assert selected.gotos[0][0] == XPostWorker.home_url
    assert selected.gotos[0][1]["wait_until"] == "domcontentloaded"
    assert diagnostics["page_selection"] == "new_controlled_page"
    assert clock.sleeps == 1


@pytest.mark.asyncio
async def test_foreign_host_containing_x_com_is_not_reused():
    context = Context([Page(url="https://evil.example/x.com/home")])
    selected, _ = await XPostWorker._select_page(context)
    assert selected is context.created[0]


@pytest.mark.asyncio
async def test_authenticated_home_with_immediate_editor_is_ready_without_sleep():
    page, clock = Page(), Clock()
    expected = editor(page)
    timings = {}
    state, composer = await worker(clock=clock)._wait_for_home_ready(page, timings)
    assert state == "AUTHENTICATED" and composer[0].nodes[0] is expected
    assert timings["composer_ms"] == 0 and not clock.sleeps


@pytest.mark.asyncio
async def test_authenticated_navigation_before_editor_keeps_polling():
    page, clock = Page(), Clock()
    clock.on_sleep = lambda n: editor(page) if n == 2 else None
    timings = {}
    state, composer = await worker(clock=clock)._wait_for_home_ready(page, timings)
    assert state == "AUTHENTICATED" and composer is not None
    assert clock.sleeps == 2
    assert timings["authenticated_navigation_ms"] == 0
    assert timings["composer_ms"] == 800


@pytest.mark.asyncio
@pytest.mark.parametrize("classification,reason", [("login", "LOGIN_REQUIRED"), ("challenge", "CHALLENGE_REQUIRED")])
async def test_login_and_challenge_are_not_reported_as_missing_composer(classification, reason):
    page = Page(authenticated=False, **{classification: True})
    composer = editor(page)
    result = await worker()._post(page, "hello")
    assert result.reason == reason and result.diagnostics["session_classification"] == classification.upper()
    assert composer.text == "" and composer.container.buttons[0].clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect,reason", [
    ("https://x.com/i/flow/login", "LOGIN_REQUIRED"),
    ("https://x.com/account/access", "CHALLENGE_REQUIRED"),
])
async def test_session_redirect_classifies_before_fill(redirect, reason):
    page = Page(authenticated=False)
    page.redirect = redirect
    result = await worker()._post(page, "hello")
    assert result.reason == reason
    assert result.diagnostics["url"] == redirect


@pytest.mark.asyncio
async def test_primary_column_fallback_uses_button_from_same_composer_block():
    page = Page()
    expected = page.add_editor(role="textbox", editable=True, label="Post text")
    state, bundle = await worker()._wait_for_home_ready(page, {})
    assert state == "AUTHENTICATED" and bundle[2] == XPostWorker.composer_fallback
    button = await worker()._first_enabled(bundle[1])
    assert button.nodes[0] is expected.container.buttons[0]


@pytest.mark.asyncio
async def test_alternative_tweet_testid_uses_contextual_selector():
    page = Page()
    expected = page.add_editor(testid="tweetTextarea_1", role="textbox", editable=True)
    _, bundle = await worker()._wait_for_home_ready(page, {})
    assert bundle[0].nodes[0] is expected
    assert bundle[2] == XPostWorker.composer_selectors[2]


@pytest.mark.asyncio
async def test_non_editable_tweet_wrapper_does_not_abort_safe_editor_fallback():
    page = Page()
    page.add_editor(testid="tweetTextarea_0", editable=False)
    expected = page.add_editor(role="textbox", editable=True, label="Post text")
    _, bundle = await worker()._wait_for_home_ready(page, {})
    assert bundle[0].nodes[0] is expected


@pytest.mark.asyncio
async def test_multiple_textboxes_select_only_visible_home_post_editor():
    page = Page()
    editor(page, visible=False)
    page.add_editor(role="textbox", editable=True, area="search")
    page.add_editor(role="textbox", editable=True, label="Message", area="dm")
    expected = editor(page)
    _, bundle = await worker()._wait_for_home_ready(page, {})
    assert bundle[0].nodes[0] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["search", "dm", "article", "dialog", "outside"])
async def test_search_dm_article_dialog_and_outside_textboxes_are_never_chosen(area):
    page = Page()
    wrong = editor(page, area=area)
    result = await worker()._post(page, "hello")
    assert result.reason == "COMPOSER_NOT_FOUND"
    assert wrong.text == "" and wrong.container.buttons[0].clicks == 0


@pytest.mark.asyncio
async def test_fallback_without_composer_button_block_is_not_used():
    page = Page()
    page.add_editor(role="textbox", label="Post text", editable=True, button=False)
    result = await worker()._post(page, "hello")
    assert result.reason == "COMPOSER_NOT_FOUND"


@pytest.mark.asyncio
async def test_ambiguous_visible_home_editors_fail_closed():
    page = Page()
    one, two = editor(page), editor(page)
    result = await worker()._post(page, "hello")
    assert result.reason == "COMPOSER_NOT_FOUND"
    assert one.text == two.text == ""


@pytest.mark.asyncio
async def test_missing_composer_diagnostics_are_bounded_and_do_not_include_text_or_query():
    page = Page()
    page.redirect = "https://x.com/home?token=DO_NOT_LOG#SECRET"
    for _ in range(20):
        page.add_editor(role="textbox", editable=True, area="dm", text="PRIVATE DM", placeholder="Message")
    result = await worker()._post(page, "hello", page_selection={"pages": 2, "initial_url": "about:"})
    diagnostics = result.diagnostics
    assert diagnostics["url"] == "https://x.com/home"
    assert diagnostics["pages"] == 2 and diagnostics["title"] == "Home / X"
    assert diagnostics["tweetTextarea"] == 0
    assert diagnostics["contenteditables"] == diagnostics["role_textboxes"] == 20
    assert len(diagnostics["editor_candidates"]) == 12
    serialized = json.dumps(diagnostics)
    assert "PRIVATE DM" not in serialized and "DO_NOT_LOG" not in serialized and "SECRET" not in serialized
    assert diagnostics["timings"]["readiness_ms"] == 1200


@pytest.mark.asyncio
async def test_diagnostic_failure_keeps_known_pre_submit_failure():
    page = Page()
    page.title_error = True
    result = await worker()._post(page, "hello")
    assert result.reason == "COMPOSER_NOT_FOUND"
    assert result.diagnostics["diagnostic_read_error"] == "RuntimeError"


class Manager:
    def __init__(self, context):
        self.context = context
        self.receipts = []
        self.calls = []
        self.leased = False
        self.browser_sessions = SimpleNamespace(active_sessions={}, is_live=lambda _: False)
        self.storage = SimpleNamespace(
            get_external_action_receipt=self.get_receipt, save_external_action_receipt=self.save_receipt,
        )

    async def get_receipt(self, _):
        return None

    async def save_receipt(self, **receipt):
        self.receipts.append(receipt)

    async def get_profile(self, _):
        return SimpleNamespace(id="profile")

    async def launch_browser(self, profile_id, *, headless):
        assert headless is True
        self.calls.append("launch_headless")
        self.leased = True
        self.browser_sessions.active_sessions[profile_id] = SimpleNamespace(browser=self.context)

    async def close_browser(self, profile_id):
        self.calls.append("close_and_release")
        self.leased = False
        self.browser_sessions.active_sessions.pop(profile_id)
        for page in self.context.pages:
            page.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("page_error", [False, True])
async def test_headless_worker_uses_same_context_and_cleans_browser_lease_after_failure(page_error):
    blank, page = Page(url="about:blank"), Page()
    manager = Manager(Context([blank, page]))
    if page_error:
        async def fail_goto(*_, **__):
            raise RuntimeError("navigation failed")
        page.goto = fail_goto
    result = await worker(manager).execute(
        profile_id="profile", text="hello", idempotency_key="fixed", execution_task_id="task"
    )
    assert not blank.gotos
    assert result.reason == ("RuntimeError" if page_error else "COMPOSER_NOT_FOUND")
    assert manager.calls == ["launch_headless", "close_and_release"]
    assert not manager.leased and not manager.browser_sessions.active_sessions
    assert blank.closed and page.closed
    assert [r["state"] for r in manager.receipts] == ["IN_PROGRESS", result.state]
