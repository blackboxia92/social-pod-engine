import pytest
from social_pod_engine import SessionState, SocialPodEngine
from social_pod_engine.adapters._placeholder import PlatformAdapterNotImplemented
from social_pod_engine.adapters.x import XAdapter


class FakeLocator:
    def __init__(self, count: int = 0, href: str | None = None) -> None:
        self._count = count
        self._href = href

    async def count(self) -> int:
        return self._count

    async def get_attribute(self, name: str) -> str | None:
        assert name == "href"
        return self._href

    async def inner_text(self) -> str:
        return ""


class FakePage:
    def __init__(self, url: str, selectors: dict[str, FakeLocator] | None = None) -> None:
        self.url = url
        self.selectors = selectors or {}
        self.navigated_to: str | None = None

    async def goto(self, url: str) -> None:
        self.navigated_to = url
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return self.selectors.get(selector, FakeLocator())


@pytest.mark.asyncio
async def test_x_opens_its_home_without_touching_upstream() -> None:
    page = FakePage("about:blank")
    await XAdapter().open_home(page)
    assert page.navigated_to == "https://x.com/home"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "selectors", "expected"),
    [
        ("https://x.com/i/flow/login", {}, SessionState.SIGNED_OUT),
        (
            "https://x.com/home",
            {"[data-testid='ocfEnterTextTextInput']": FakeLocator(1)},
            SessionState.CHALLENGE,
        ),
        (
            "https://x.com/home",
            {"[data-testid='SideNav_AccountSwitcher_Button']": FakeLocator(1)},
            SessionState.ACTIVE,
        ),
        ("https://x.com/home", {}, SessionState.UNKNOWN),
    ],
)
async def test_x_session_states_are_conservative(url, selectors, expected) -> None:
    health = await XAdapter().check_session(FakePage(url, selectors))
    assert health.state is expected


@pytest.mark.asyncio
async def test_x_reads_handle_without_persisting_it() -> None:
    page = FakePage(
        "https://x.com/home",
        {"a[data-testid='AppTabBar_Profile_Link']": FakeLocator(1, "/blackboxia92")},
    )
    profile = await XAdapter().get_profile(page, profile_id="cpm-profile-7")
    assert profile.handle == "blackboxia92"
    assert profile.profile_id == "cpm-profile-7"


def test_registry_exposes_the_three_platforms() -> None:
    engine = SocialPodEngine.with_builtin_adapters()
    assert engine.registry.platform_ids() == ("facebook", "instagram", "x")
    assert engine.registry.get("x").get_capabilities().supports("session_health")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_id", ["instagram", "facebook"])
async def test_placeholders_fail_explicitly(platform_id: str) -> None:
    engine = SocialPodEngine.with_builtin_adapters()
    with pytest.raises(PlatformAdapterNotImplemented):
        await engine.registry.get(platform_id).check_session(FakePage("https://example.test/"))
