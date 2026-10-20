from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..database import get_db
from ..metrics import METRICS
from ..schemas import HealthOut, MetricOut

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # pragma: no cover
        database = "error"
    return HealthOut(
        status="ok" if database == "ok" else "degraded", version=__version__, database=database
    )


@router.get("/v1/metrics", response_model=list[MetricOut])
def list_metrics() -> list[MetricOut]:
    return [
        MetricOut(
            pattern=m.pattern,
            kind=m.kind.value,
            unit=m.unit,
            rated_unit=m.rated_unit,
            description=m.description,
        )
        for m in METRICS
    ]
