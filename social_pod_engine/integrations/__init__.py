"""Public-service integrations owned by Social Pod.

Integrations in this package speak only documented remote interfaces.  They do
not import, construct, or otherwise depend on upstream implementation classes.
"""

from .camoufox_http import (
    DEFAULT_CAMOUFOX_BASE_URL,
    CamoufoxHttpClient,
    CamoufoxHttpError,
    CamoufoxHttpGateway,
    CamoufoxHttpUnavailable,
    CamoufoxPageAccessUnavailable,
)

__all__ = [
    "DEFAULT_CAMOUFOX_BASE_URL",
    "CamoufoxHttpClient",
    "CamoufoxHttpError",
    "CamoufoxHttpGateway",
    "CamoufoxHttpUnavailable",
    "CamoufoxPageAccessUnavailable",
]
