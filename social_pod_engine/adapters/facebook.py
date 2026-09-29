"""Facebook placeholder: deliberately registered but not operational."""

from ._placeholder import PlaceholderAdapter


class FacebookAdapter(PlaceholderAdapter):
    platform_id = "facebook"
    display_name = "Facebook"
    home_url = "https://www.facebook.com/"
