"""Built-in platform adapters."""

from .facebook import FacebookAdapter
from .instagram import InstagramAdapter
from .x import XAdapter

__all__ = ["FacebookAdapter", "InstagramAdapter", "XAdapter"]
