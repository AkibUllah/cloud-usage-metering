"""Shared route dependencies."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from .database import get_db
from .models import Tenant

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(request: Request, api_key: str | None = Security(_api_key_header)) -> None:
    """Enforce the shared API key when one is configured; a no-op otherwise."""
    expected = request.app.state.settings.api_key
    if not expected:
        return
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def get_tenant(tenant_id: str, db: Session = Depends(get_db)) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Tenant {tenant_id!r} not found")
    return tenant
