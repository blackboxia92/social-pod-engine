"""API models for Camoufox Profile Manager."""

from .groups import (
    GroupCreateRequest,
    GroupListResponse,
    GroupResponse,
    GroupUpdateRequest,
)
from .profiles import (
    ProfileCloneRequest,
    ProfileCreateRequest,
    ProfileLaunchRequest,
    ProfileLaunchResponse,
    ProfileListResponse,
    ProfileRemoteLaunchResponse,
    ProfileResponse,
    ProfileStatsResponse,
    ProfileUpdateRequest,
    RemoteControlResponse,
    RemotePageOperationRequest,
    RemotePageOperationResponse,
    XPostActionRequest,
    XPostActionResponse,
)
from .system import (
    ApiResponse,
    ErrorResponse,
    SystemStatusResponse,
)

__all__ = [
    "ProfileCreateRequest",
    "ProfileUpdateRequest",
    "ProfileResponse",
    "ProfileListResponse",
    "ProfileStatsResponse",
    "ProfileCloneRequest",
    "ProfileLaunchRequest",
    "ProfileLaunchResponse",
    "ProfileRemoteLaunchResponse",
    "RemoteControlResponse",
    "RemotePageOperationRequest",
    "RemotePageOperationResponse",
    "XPostActionRequest",
    "XPostActionResponse",
    "GroupCreateRequest",
    "GroupUpdateRequest",
    "GroupResponse",
    "GroupListResponse",
    "SystemStatusResponse",
    "ApiResponse",
    "ErrorResponse",
]
