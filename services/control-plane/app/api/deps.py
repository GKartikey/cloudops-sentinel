"""Request-scoped dependencies: authentication and shared state access.

Auth model, and why it is shaped this way:

  * The token is read from the environment (CLOUDOPS_API_TOKEN), which is
    populated from a Kubernetes Secret in the manifests. There is no default
    token, no token in any config file, and no token in this repository.
  * When no token is configured the API is open. That is correct for a laptop
    demo and explicitly wrong for anything else, so /api/v1/system reports
    auth_enabled=false and the readiness payload says so too - the deployment is
    honest about being unauthenticated rather than quietly pretending otherwise.
  * Comparison uses hmac.compare_digest, not ==, so a wrong token cannot be
    recovered one character at a time by timing the response.
  * REQUIRE_TOKEN_FOR_WRITES lets reads stay open (a dashboard on a wall) while
    the state-changing endpoints - incident injection, alert acknowledgement -
    still demand a credential. Least privilege applied to an HTTP surface.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status


def get_state(request: Request):
    return request.app.state.ctx


def _extract(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return (x_api_key or "").strip()


def require_read(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = request.app.state.ctx.settings
    if not settings.api_token:
        return
    if settings.require_token_for_writes:
        # Writes-only mode: reads are intentionally public.
        return
    _check(settings.api_token, _extract(authorization, x_api_key))


def require_write(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = request.app.state.ctx.settings
    if not settings.api_token:
        return
    _check(settings.api_token, _extract(authorization, x_api_key))


def _check(expected: str, presented: str) -> None:
    if not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


ReadAuth = Depends(require_read)
WriteAuth = Depends(require_write)
