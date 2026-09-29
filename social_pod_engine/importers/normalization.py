"""Canonical import identity functions, deliberately separate from parsers."""

from ..domain import SocialPlatform, normalize_account_username

_PLATFORM_ALIASES = {
    "x": SocialPlatform.X,
    "twitter": SocialPlatform.X,
    "twitter.com": SocialPlatform.X,
    "instagram": SocialPlatform.INSTAGRAM,
    "instagram.com": SocialPlatform.INSTAGRAM,
    "facebook": SocialPlatform.FACEBOOK,
    "facebook.com": SocialPlatform.FACEBOOK,
}


def normalize_platform(raw_platform: str) -> SocialPlatform:
    if not isinstance(raw_platform, str):
        raise TypeError("platform must be a string")
    try:
        return _PLATFORM_ALIASES[raw_platform.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported platform: {raw_platform!r}") from exc


def normalize_username(platform: SocialPlatform, raw_username: str) -> str:
    """Platform hook point; all Phase 2 platforms share the current rule."""
    SocialPlatform(platform)  # validates a caller supplied enum/string at the boundary
    return normalize_account_username(raw_username)


def account_identity_key(platform: SocialPlatform, username: str) -> tuple[SocialPlatform, str]:
    return platform, normalize_username(platform, username)
