"""Server-side entitlement checks shared by every app route and service."""

from accalab.entitlements import allowed


class AccessDenied(Exception):
    """Raised when a role requests data outside its configured access."""


def require_entitlement(role: str, entitlement: str) -> None:
    if not allowed(role, entitlement):
        raise AccessDenied(f"Role {role!r} cannot access {entitlement!r}.")
