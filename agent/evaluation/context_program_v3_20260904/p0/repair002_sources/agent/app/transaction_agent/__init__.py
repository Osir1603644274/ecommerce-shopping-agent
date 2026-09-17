"""Transaction Agent package.

Only the read-only Shopping menu is public.  Transaction authority has no
public Python factory and remains unavailable until a real auth provider exists.
"""

from .capabilities import SHOPPING_TOOL_NAMES

__all__ = ["SHOPPING_TOOL_NAMES"]
