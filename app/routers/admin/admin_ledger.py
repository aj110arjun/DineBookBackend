from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.schemas.admin_ledger import AdminLedgerReport, LedgerExportQuery, LedgerQuery
from app.services.admin_ledger import ledger_report
from app.services.admin_ledger_exports import export_ledger

router = APIRouter(prefix="/api/admin/ledger", tags=["admin platform ledger"])


@contextmanager
def ledger_scope(db):
    bind = db.get_bind()
    db.rollback()
    with Session(bind, autoflush=False) as read:
        if bind.dialect.name == "postgresql":
            read.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
            read.execute(text("SET LOCAL statement_timeout = 5000"))
        yield read


@router.get("", response_model=AdminLedgerReport)
def platform_ledger(params: Annotated[LedgerQuery, Query()], response: Response,
                    admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    with ledger_scope(db) as read:
        return ledger_report(read, params)


@router.get("/export")
def download_ledger(params: Annotated[LedgerExportQuery, Query()],
                    admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    with ledger_scope(db) as read:
        report = ledger_report(read, params, export=True)
    body, content_type = export_ledger(report, params.format)
    filename = f"platform-ledger_{report.start_date}_{report.end_date}.{params.format}"
    return Response(body, media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
