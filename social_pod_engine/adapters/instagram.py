"""Instagram placeholder: deliberately registered but not operational."""

from ._placeholder import PlaceholderAdapter


class InstagramAdapter(PlaceholderAdapter):
    platform_id = "instagram"
    display_name = "Instagram"
    home_url = "https://www.instagram.com/"
