"""Read-only X adapter implemented against Playwright-compatible pages."""

from urllib.parse import urlparse

from ..contracts import SocialPage, SocialPlatformAdapter
from ..models import AccountProfile, HealthReport, PlatformCapabilities, SessionState


class XAdapter(SocialPlatformAdapter):
    platform_id = "x"
    display_name = "X"
    home_url = "https://x.com/home"

    _challenge_selectors = (
        "input[name='challenge_response']",
        "[data-testid='ocfEnterTextTextInput']",
        "[data-testid='ocfEnterTextNextButton']",
    )
    _authenticated_selectors = (
        "[data-testid='SideNav_AccountSwitcher_Button']",
        "a[data-testid='AppTabBar_Profile_Link']",
    )

    async def open_home(self, page: SocialPage) -> None:
        await page.goto(self.home_url)

    async def check_session(self, page: SocialPage) -> HealthReport:
        path = urlparse(page.url).path.lower()
        if any(token in path for token in ("/i/flow/login", "/login", "/signup")):
            return HealthReport(self.platform_id, SessionState.SIGNED_OUT, ("login URL detected",))
        if await self._has_any(page, self._challenge_selectors):
            return HealthReport(self.platform_id, SessionState.CHALLENGE, ("challenge UI detected",))
        if await self._has_any(page, self._authenticated_selectors):
            return HealthReport(self.platform_id, SessionState.ACTIVE)
        return HealthReport(self.platform_id, SessionState.UNKNOWN, ("no stable session marker found",))

    @staticmethod
    async def _has_any(page: SocialPage, selectors: tuple[str, ...]) -> bool:
        """Check selectors sequentially so ordinary Playwright pages work unchanged."""
        for selector in selectors:
            if await page.locator(selector).count():
                return True
        return False

    async def get_profile(self, page: SocialPage, *, profile_id: str | None = None) -> AccountProfile:
        profile_link = page.locator("a[data-testid='AppTabBar_Profile_Link']")
        href = await profile_link.get_attribute("href") if await profile_link.count() else None
        handle = href.strip("/").split("/")[0] if href else None
        return AccountProfile(platform_id=self.platform_id, handle=handle, profile_id=profile_id)

    def get_capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            enabled=frozenset({"session_health", "read_profile"}),
            planned=frozenset({"post", "reply", "like", "repost", "follow", "unfollow"}),
        )
