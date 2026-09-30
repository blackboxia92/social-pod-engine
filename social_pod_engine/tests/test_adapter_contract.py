import pytest

from social_pod_engine.adapters import FacebookAdapter, InstagramAdapter, XAdapter
from social_pod_engine.adapters._placeholder import PlatformAdapterNotImplemented
from social_pod_engine.adapters.base import (
    BaseSocialAdapter,
    Capability,
    CapabilityNotSupported,
    ChallengeType,
    ExecutionContext,
)
from social_pod_engine.domain import HealthStatus, SessionStatus


class FakeLocator:
    def __init__(self, count=0, href=None):
        self.count_value = count
        self.href = href

    async def count(self):
        return self.count_value

    async def get_attribute(self, name):
        assert name == "href"
        return self.href

    async def inner_text(self):
        return ""


class FakePage:
    def __init__(self, url, selectors=None):
        self.url = url
        self.selectors = selectors or {}

    async def goto(self, url):
        self.url = url

    def locator(self, selector):
        return self.selectors.get(selector, FakeLocator())


def test_all_adapters_formally_implement_the_base_contract():
    adapters = [XAdapter(), InstagramAdapter(), FacebookAdapter()]
    assert all(isinstance(adapter, BaseSocialAdapter) for adapter in adapters)
    assert [adapter.platform_name for adapter in adapters] == ["x", "instagram", "facebook"]


@pytest.mark.asyncio
async def test_x_exposes_only_read_only_capabilities_and_reports_session_health():
    page = FakePage(
        "https://x.com/home",
        {"[data-testid='SideNav_AccountSwitcher_Button']": FakeLocator(1)},
    )
    adapter = XAdapter()
    context = ExecutionContext(page=page, upstream_profile_id="cpm-x-1")

    assert adapter.get_supported_capabilities() == {
        Capability.SESSION_HEALTH,
        Capability.READ_PROFILE,
        Capability.POST,
    }
    assert await adapter.validate_session(context) is SessionStatus.VALID
    signal = await adapter.check_health(context)
    assert signal.health_status is HealthStatus.HEALTHY
    assert await adapter.detect_challenge(context) is ChallengeType.NONE


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [Capability.REPLY, Capability.LIKE, Capability.FOLLOW, Capability.REPOST])
async def test_x_rejects_every_write_capability(capability):
    adapter = XAdapter()
    with pytest.raises(CapabilityNotSupported):
        await adapter.execute_capability(capability, {}, ExecutionContext(page=FakePage("https://x.com/home")))


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [InstagramAdapter, FacebookAdapter])
async def test_platform_placeholders_have_no_enabled_capabilities(adapter_type):
    adapter = adapter_type()
    assert adapter.get_supported_capabilities() == frozenset()
    with pytest.raises(CapabilityNotSupported):
        await adapter.execute_capability(Capability.POST, {}, ExecutionContext())
    with pytest.raises(PlatformAdapterNotImplemented):
        await adapter.validate_session(ExecutionContext())
