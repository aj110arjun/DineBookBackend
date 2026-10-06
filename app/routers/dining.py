import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from decimal import Decimal
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.dining import DiningTable, DiningTableType, RestaurantFloor
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.admin_auth import current_admin
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager

manager_router = APIRouter(prefix="/api/manager", tags=["manager floors and tables"])
chef_router = APIRouter(prefix="/api/chef/floors", tags=["chef floors and tables"])
admin_router = APIRouter(prefix="/api/admin/restaurants", tags=["admin floor and table oversight"])
customer_router = APIRouter(prefix="/api/customer/restaurants", tags=["customer floor and table availability"])

class FloorCreate(BaseModel):
    floor_number: int = Field(gt=0)
    floor_name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("floor_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Floor name is required.")
        return value


class FloorUpdate(BaseModel):
    floor_number: int | None = Field(default=None, gt=0)
    floor_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None

    @field_validator("floor_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Floor name is required.")
        return value


class TableCreate(BaseModel):
    floor_id: uuid.UUID
    table_number: str = Field(min_length=1, max_length=30)
    capacity: int = Field(gt=0, le=50)
    reservation_fee: Decimal = Field(default=Decimal("250.00"), ge=0, le=100000)
    table_type: DiningTableType = DiningTableType.ROUND
    status: str = Field(default="available", pattern="^(available|reserved|occupied|maintenance)$")

    @field_validator("table_number")
    @classmethod
    def clean_number(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Table number is required.")
        return value


class TableUpdate(BaseModel):
    floor_id: uuid.UUID | None = None
    table_number: str | None = Field(default=None, min_length=1, max_length=30)
    capacity: int | None = Field(default=None, gt=0, le=50)
    reservation_fee: Decimal | None = Field(default=None, ge=0, le=100000)
    table_type: DiningTableType | None = None
    status: str | None = Field(default=None, pattern="^(available|reserved|occupied|maintenance)$")

    @field_validator("table_number")
    @classmethod
    def clean_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Table number is required.")
        return value


def get_manager_restaurant(manager: User, db: Session) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this manager account.")
    return restaurant


def floor_data(floor: RestaurantFloor, tables: list[DiningTable] | None = None) -> dict:
    return {
        "id": str(floor.id), "restaurant_id": str(floor.restaurant_id),
        "floor_number": floor.floor_number, "floor_name": floor.floor_name,
        "name": floor.floor_name, "description": floor.description,
        "is_active": floor.is_active,
        "tables": [table_data(table) for table in (tables if tables is not None else floor.tables)],
    }


def table_data(table: DiningTable) -> dict:
    return {
        "id": str(table.id), "floor_id": str(table.floor_id),
        "table_number": table.table_number, "capacity": table.capacity,
        "seats": table.capacity, "table_type": table.table_type.value, "status": table.status,
        "reservation_fee": float(table.reservation_fee or 0),
    }


def list_floor_data(restaurant_id: uuid.UUID, db: Session) -> list[dict]:
    floors = (db.query(RestaurantFloor).filter(
        RestaurantFloor.restaurant_id == restaurant_id, RestaurantFloor.is_active.is_(True)
    ).order_by(RestaurantFloor.floor_number).all())
    return [floor_data(floor) for floor in floors]


def owned_floor(floor_id: uuid.UUID, restaurant_id: uuid.UUID, db: Session, active: bool = True) -> RestaurantFloor:
    query = db.query(RestaurantFloor).filter(RestaurantFloor.id == floor_id, RestaurantFloor.restaurant_id == restaurant_id)
    if active:
        query = query.filter(RestaurantFloor.is_active.is_(True))
    floor = query.first()
    if floor is None:
        raise HTTPException(status_code=404, detail="Floor not found for this restaurant.")
    return floor


def owned_table(table_id: uuid.UUID, restaurant_id: uuid.UUID, db: Session) -> DiningTable:
    table = db.query(DiningTable).filter(DiningTable.id == table_id, DiningTable.restaurant_id == restaurant_id).first()
    if table is None:
        raise HTTPException(status_code=404, detail="Table not found for this restaurant.")
    return table


def duplicate_error(exc: IntegrityError) -> HTTPException:
    message = str(getattr(exc, "orig", exc)).lower()
    if "uq_floors_restaurant_number" in message:
        return HTTPException(status_code=409, detail="This floor number is already used in your restaurant.")
    if "uq_dining_tables_floor_number" in message or "unique" in message:
        return HTTPException(status_code=409, detail="This table number is already used on the selected floor.")
    return HTTPException(status_code=409, detail="The requested floor or table conflicts with existing data.")


@manager_router.get("/floors")
def manager_floors(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = get_manager_restaurant(manager, db)
    return list_floor_data(restaurant.id, db)


@manager_router.post("/floors", status_code=status.HTTP_201_CREATED)
def create_floor(payload: FloorCreate, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    floor = RestaurantFloor(restaurant_id=restaurant.id, **payload.model_dump())
    db.add(floor)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise duplicate_error(exc)
    db.refresh(floor)
    return floor_data(floor, [])


@manager_router.get("/floors/{floor_id}")
def get_floor(floor_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    return floor_data(owned_floor(floor_id, restaurant.id, db))


@manager_router.patch("/floors/{floor_id}")
@manager_router.put("/floors/{floor_id}")
def update_floor(floor_id: uuid.UUID, payload: FloorUpdate, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    floor = owned_floor(floor_id, restaurant.id, db, active=False)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(floor, key, value)
    floor.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise duplicate_error(exc)
    db.refresh(floor)
    return floor_data(floor)


@manager_router.delete("/floors/{floor_id}")
def delete_floor(floor_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    floor = owned_floor(floor_id, restaurant.id, db, active=False)
    if db.query(DiningTable.id).filter(DiningTable.floor_id == floor.id).first():
        raise HTTPException(status_code=409, detail="Move or delete this floor's tables before deactivating the floor.")
    floor.is_active = False
    floor.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Floor deactivated."}


@manager_router.get("/tables")
def manager_tables(floor_id: uuid.UUID | None = Query(default=None), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = get_manager_restaurant(manager, db)
    query = db.query(DiningTable).join(RestaurantFloor, DiningTable.floor_id == RestaurantFloor.id).filter(
        DiningTable.restaurant_id == restaurant.id, RestaurantFloor.is_active.is_(True)
    )
    if floor_id is not None:
        owned_floor(floor_id, restaurant.id, db)
        query = query.filter(DiningTable.floor_id == floor_id)
    return [table_data(table) for table in query.order_by(DiningTable.table_number).all()]


@manager_router.post("/tables", status_code=status.HTTP_201_CREATED)
@manager_router.post("/floors/tables", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_table(payload: TableCreate, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    owned_floor(payload.floor_id, restaurant.id, db)
    table = DiningTable(restaurant_id=restaurant.id, **payload.model_dump())
    db.add(table)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise duplicate_error(exc)
    db.refresh(table)
    return table_data(table)


@manager_router.get("/tables/{table_id}")
def get_table(table_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    return table_data(owned_table(table_id, restaurant.id, db))


@manager_router.patch("/tables/{table_id}")
@manager_router.put("/tables/{table_id}")
@manager_router.patch("/floors/tables/{table_id}", include_in_schema=False)
def update_table(table_id: uuid.UUID, payload: TableUpdate, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    table = owned_table(table_id, restaurant.id, db)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_active") is False and db.query(DiningTable.id).filter(DiningTable.floor_id == floor.id).first():
        raise HTTPException(status_code=409, detail="Move or delete this floor's tables before deactivating the floor.")
    target_floor = changes.get("floor_id", table.floor_id)
    owned_floor(target_floor, restaurant.id, db)
    for key, value in changes.items():
        setattr(table, key, value)
    table.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise duplicate_error(exc)
    db.refresh(table)
    return table_data(table)


@manager_router.delete("/tables/{table_id}")
def delete_table(table_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = get_manager_restaurant(manager, db)
    table = owned_table(table_id, restaurant.id, db)
    db.delete(table)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="This table is referenced by existing records and cannot be deleted.") from exc
    return {"message": "Table deleted."}


@chef_router.get("")
def chef_floors(chef: User = Depends(current_chef), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == chef.manager_id).first()
    return list_floor_data(restaurant.id, db) if restaurant else []


@customer_router.get("/{restaurant_id}/floors")
def customer_floors(restaurant_id: uuid.UUID, db: Session = Depends(get_db)) -> list[dict]:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return list_floor_data(restaurant.id, db)


@admin_router.get("/{restaurant_id}/floors")
def admin_floors(restaurant_id: uuid.UUID, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    return list_floor_data(restaurant.id, db)
