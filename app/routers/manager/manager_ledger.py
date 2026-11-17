from typing import Annotated
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.models.restaurant import Restaurant
from app.routers.auth.manager_auth import current_manager
from app.schemas.manager_ledger import LedgerExportQuery, LedgerQuery, LedgerReport
from app.services.manager_ledger import ledger_report
from app.services.manager_ledger_exports import export_ledger

router = APIRouter(prefix="/api/manager/ledger", tags=["manager restaurant ledger"])


@contextmanager
def ledger_scope(manager, db):
    owner, bind = manager.id, db.get_bind()
    db.rollback()  # Release the authentication transaction before the report snapshot.
    with Session(bind, autoflush=False) as read:
        if bind.dialect.name == "postgresql":
            read.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
            read.execute(text("SET LOCAL statement_timeout = 5000"))
        restaurant = read.query(Restaurant).filter(Restaurant.manager_id == owner).first()
        if restaurant is None:
            raise HTTPException(404, "No restaurant is linked to this manager account.")
        yield read, restaurant


@router.get("", response_model=LedgerReport)
def restaurant_ledger(params: Annotated[LedgerQuery, Query()], response: Response, manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with ledger_scope(manager, db) as (read, restaurant):
        return ledger_report(read, restaurant, params)


@router.get("/export")
def download_ledger(params: Annotated[LedgerExportQuery, Query()], manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    with ledger_scope(manager, db) as (read, restaurant):
        report = ledger_report(read, restaurant, params, export=True)
    body, content_type = export_ledger(report, params.format)
    filename = f"restaurant-ledger_{report.start_date}_{report.end_date}.{params.format}"
    return Response(body, media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
