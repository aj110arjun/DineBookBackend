import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.menu import Food, FoodVariant
from app.models.orders import FoodOrder
from app.models.restaurant import Restaurant
from app.models.reviews import Review, ReviewFlag, ReviewModerationEvent, ReviewReply
from app.models.user import User
from app.routers.customer_auth import current_customer
from app.routers.chef_auth import current_chef
from app.routers.manager_auth import current_manager
from app.routers.admin_auth import current_admin
from app.services.notifications import create_notification

customer_router = APIRouter(prefix="/api/customer/reviews", tags=["customer reviews"])
public_router = APIRouter(prefix="/api/reviews", tags=["reviews"])
chef_router = APIRouter(prefix="/api/chef/reviews", tags=["chef reviews"])
manager_router = APIRouter(prefix="/api/manager/reviews", tags=["manager reviews"])
admin_router = APIRouter(prefix="/api/admin/reviews", tags=["admin review moderation"])

class ReviewPayload(BaseModel):
    order_id: uuid.UUID
    food_id: uuid.UUID | None = None
    rating: int = Field(ge=1, le=5)
    content: str = Field(min_length=3, max_length=5000)

class FlagPayload(BaseModel):
    reason: str = Field(pattern="^(SPAM|ABUSIVE|HARASSMENT|MISLEADING|OTHER)$")
    explanation: str | None = Field(default=None, max_length=1000)

class ReplyPayload(BaseModel):
    content: str = Field(min_length=2, max_length=2000)

class ActionPayload(BaseModel):
    action: str = Field(pattern="^(APPROVE|REJECT|HIDE|RESTORE|DISMISS_FLAG)$")
    reason: str | None = Field(default=None, max_length=1000)
    flag_id: uuid.UUID | None = None

def clean(value: str) -> str:
    return " ".join(value.split())

def review_data(db: Session, review: Review, private: bool = False) -> dict:
    restaurant = db.get(Restaurant, review.restaurant_id)
    food = db.get(Food, review.food_id) if review.food_id else None
    reply = db.query(ReviewReply).filter(ReviewReply.review_id == review.id).first()
    customer = db.get(User, review.customer_id)
    data = {"id": str(review.id), "restaurant_id": str(review.restaurant_id), "restaurant_name": restaurant.name if restaurant else None,
        "food_id": str(review.food_id) if review.food_id else None, "food_name": food.name if food else None,
        "order_id": str(review.order_id), "rating": review.rating, "content": review.content,
        "created_at": review.created_at.isoformat(), "updated_at": review.updated_at.isoformat(),
        "reply": {"content": reply.content, "updated_at": reply.updated_at.isoformat()} if reply and review.status == "PUBLISHED" else None}
    if private:
        data.update(status=review.status, customer_name=customer.name if customer else "Customer", is_mine=True)
    return data

@customer_router.get("")
def my_reviews(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> list[dict]:
    reviews = db.query(Review).filter(Review.customer_id == customer.id).order_by(Review.created_at.desc()).all()
    return [review_data(db, review, True) for review in reviews]

@customer_router.post("", status_code=201)
def create_review(payload: ReviewPayload, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    content = clean(payload.content)
    if len(content) < 3:
        raise HTTPException(422, "Review must contain at least 3 non-space characters.")
    order = db.query(FoodOrder).filter(FoodOrder.id == payload.order_id, FoodOrder.customer_id == customer.id).with_for_update().first()
    if order is None:
        raise HTTPException(404, "Eligible order not found.")
    if order.status != "COMPLETED":
        raise HTTPException(409, "You can review an order after it is completed.")
    if db.query(Review.id).filter(Review.customer_id == customer.id, Review.restaurant_id == order.restaurant_id).first():
        raise HTTPException(409, "You have already reviewed this restaurant. Edit your existing review instead.")
    if payload.food_id:
        ordered_variant_ids = [line.get("variant_id") for line in (order.items or []) if isinstance(line, dict) and line.get("variant_id")]
        ordered_food = db.query(FoodVariant.food_id).join(Food, Food.id == FoodVariant.food_id).filter(
            FoodVariant.id.in_(ordered_variant_ids or [uuid.UUID(int=0)]), FoodVariant.food_id == payload.food_id,
            Food.restaurant_id == order.restaurant_id,
        ).first()
        if not ordered_food:
            raise HTTPException(422, "Food item was not included in this order.")
    review = Review(customer_id=customer.id, restaurant_id=order.restaurant_id, order_id=order.id, food_id=payload.food_id, rating=payload.rating, content=content)
    db.add(review)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "You have already reviewed this restaurant or order.") from exc
    create_notification(db, customer, notification_type="review.submitted", category="REVIEW",
        title="Review submitted", message="Your review was submitted successfully.", restaurant_id=order.restaurant_id,
        entity_type="review", entity_id=review.id, action_url="/customer/profile/reviews",
        dedupe_key=f"review:{review.id}:submitted:customer")
    restaurant = db.query(Restaurant).filter_by(id=order.restaurant_id).first()
    manager = db.query(User).filter_by(id=restaurant.manager_id).first() if restaurant else None
    if manager:
        create_notification(db, manager, notification_type="review.submitted", category="REVIEW",
            title="New customer review", message="A customer left a review for your restaurant.",
            restaurant_id=order.restaurant_id, entity_type="review", entity_id=review.id,
            action_url="/manager/reviews", dedupe_key=f"review:{review.id}:submitted")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You have already reviewed this restaurant or order.")
    db.refresh(review)
    return review_data(db, review, True)

@customer_router.patch("/{review_id}")
def update_review(review_id: uuid.UUID, payload: ReviewPayload, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    review = db.query(Review).filter(Review.id == review_id, Review.customer_id == customer.id).with_for_update().first()
    if review is None:
        raise HTTPException(404, "Review not found.")
    if review.status != "PUBLISHED":
        raise HTTPException(409, "This review is locked while it is under moderation or restricted.")
    order = db.query(FoodOrder).filter(FoodOrder.id == review.order_id, FoodOrder.customer_id == customer.id, FoodOrder.status == "COMPLETED").first()
    if order is None or payload.order_id != review.order_id:
        raise HTTPException(409, "The review's completed order cannot be changed.")
    if payload.food_id:
        ordered_variant_ids = [line.get("variant_id") for line in (order.items or []) if isinstance(line, dict) and line.get("variant_id")]
        ordered_food = db.query(FoodVariant.food_id).filter(FoodVariant.id.in_(ordered_variant_ids or [uuid.UUID(int=0)]), FoodVariant.food_id == payload.food_id).first()
        if not ordered_food:
            raise HTTPException(422, "Food item was not included in this order.")
    content = clean(payload.content)
    if len(content) < 3:
        raise HTTPException(422, "Review must contain at least 3 non-space characters.")
    review.food_id, review.rating, review.content = payload.food_id, payload.rating, content
    db.commit(); db.refresh(review)
    return review_data(db, review, True)

@customer_router.post("/{review_id}/flags", status_code=201)
def flag_review(review_id: uuid.UUID, payload: FlagPayload, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    target = db.query(Review).filter(Review.id == review_id, Review.status != "REJECTED").first()
    if target is None:
        raise HTTPException(404, "Review not found.")
    if target.customer_id == customer.id:
        raise HTTPException(409, "You cannot report your own review.")
    flag = ReviewFlag(review_id=review_id, customer_id=customer.id, reason=payload.reason, explanation=clean(payload.explanation) if payload.explanation else None)
    db.add(flag)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "You have already reported this review.") from exc
    for admin in db.query(User).filter(User.role == "ADMIN", User.is_active.is_(True)).all():
        create_notification(db, admin, notification_type="review.flagged", category="REVIEW",
            title="Review flagged for moderation", message="A customer report requires moderation.",
            restaurant_id=target.restaurant_id, entity_type="review", entity_id=target.id,
            action_url="/admin/reviews", priority="HIGH", dedupe_key=f"review-flag:{flag.id}:admin:{admin.id}")
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "You have already reported this review.")
    return {"id": str(flag.id), "status": flag.status, "message": "Report submitted for review."}

@public_router.get("/restaurants/{restaurant_id}")
def public_reviews(restaurant_id: uuid.UUID, db: Session = Depends(get_db), limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    q = db.query(Review).filter(Review.restaurant_id == restaurant_id, Review.status == "PUBLISHED")
    count, average = q.count(), db.query(func.avg(Review.rating)).filter(Review.restaurant_id == restaurant_id, Review.status == "PUBLISHED").scalar()
    rows = q.order_by(Review.created_at.desc()).offset(offset).limit(limit).all()
    return {"items": [review_data(db, r) for r in rows], "total": count, "average_rating": round(float(average), 2) if average else None}

@chef_router.get("")
def chef_reviews(chef: User = Depends(current_chef), db: Session = Depends(get_db), rating: int | None = Query(None, ge=1, le=5), food_id: uuid.UUID | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == chef.manager_id).first()
    if not restaurant: raise HTTPException(404, "No restaurant is assigned to this chef.")
    query = db.query(Review).filter(Review.restaurant_id == restaurant.id, Review.status == "PUBLISHED")
    if rating: query = query.filter(Review.rating == rating)
    if food_id: query = query.filter(Review.food_id == food_id)
    total = query.count(); items = query.order_by(Review.created_at.desc()).offset(offset).limit(limit).all()
    avg = db.query(func.avg(Review.rating)).filter(Review.restaurant_id == restaurant.id, Review.status == "PUBLISHED").scalar()
    return {"items": [review_data(db, r) for r in items], "total": total, "average_rating": round(float(avg), 2) if avg else None}

def manager_restaurant(manager: User, db: Session) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    if restaurant is None: raise HTTPException(404, "No restaurant is linked to this manager account.")
    return restaurant

@manager_router.get("")
def manager_reviews(manager: User = Depends(current_manager), db: Session = Depends(get_db), rating: int | None = Query(None, ge=1, le=5), food_id: uuid.UUID | None = None, date_from: datetime | None = None, date_to: datetime | None = None, status: str | None = None, replied: bool | None = None, search: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)) -> dict:
    restaurant = manager_restaurant(manager, db)
    query = db.query(Review).filter(Review.restaurant_id == restaurant.id)
    if rating: query = query.filter(Review.rating == rating)
    if food_id: query = query.filter(Review.food_id == food_id)
    if date_from: query = query.filter(Review.created_at >= date_from)
    if date_to: query = query.filter(Review.created_at <= date_to)
    if status: query = query.filter(Review.status == status.upper())
    if replied is not None:
        query = query.join(ReviewReply, ReviewReply.review_id == Review.id, isouter=True).filter(ReviewReply.id.is_not(None) if replied else ReviewReply.id.is_(None))
    if search: query = query.filter(Review.content.ilike(f"%{search[:100]}%"))
    total = query.count(); rows = query.order_by(Review.created_at.desc()).offset(offset).limit(limit).all()
    avg = db.query(func.avg(Review.rating)).filter(Review.restaurant_id == restaurant.id, Review.status == "PUBLISHED").scalar()
    return {"items": [review_data(db, r, True) for r in rows], "total": total, "average_rating": round(float(avg), 2) if avg else None}

@manager_router.put("/{review_id}/reply")
def save_reply(review_id: uuid.UUID, payload: ReplyPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = manager_restaurant(manager, db)
    review = db.query(Review).filter(Review.id == review_id, Review.restaurant_id == restaurant.id, Review.status == "PUBLISHED").with_for_update().first()
    if review is None: raise HTTPException(404, "Published review not found for your restaurant.")
    content = clean(payload.content)
    if len(content) < 2: raise HTTPException(422, "Reply must contain at least 2 non-space characters.")
    reply = db.query(ReviewReply).filter(ReviewReply.review_id == review.id).first()
    if reply:
        if reply.manager_id != manager.id: raise HTTPException(403, "You cannot edit this reply.")
        reply.content = content
    else:
        reply = ReviewReply(review_id=review.id, restaurant_id=restaurant.id, manager_id=manager.id, content=content); db.add(reply)
    db.flush()
    customer = db.query(User).filter_by(id=review.customer_id).first()
    if customer:
        create_notification(db, customer, notification_type="review.replied", category="REVIEW",
            title="The restaurant replied to your review", message=f"{restaurant.name} responded to your review.",
            restaurant_id=restaurant.id, entity_type="review", entity_id=review.id,
            action_url="/customer/profile/reviews", dedupe_key=f"review:{review.id}:reply:{reply.id if reply.id else 'updated'}")
    db.commit(); db.refresh(reply)
    return {"review_id": str(review.id), "content": reply.content, "updated_at": reply.updated_at.isoformat()}

@admin_router.get("")
def moderation_queue(admin: User = Depends(current_admin), db: Session = Depends(get_db), status: str = "PENDING", reason: str | None = None, rating: int | None = Query(None, ge=1, le=5), date_from: datetime | None = None, date_to: datetime | None = None, min_reports: int = Query(1, ge=1), search: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)) -> dict:
    query = db.query(ReviewFlag, Review).join(Review, Review.id == ReviewFlag.review_id)
    if status.upper() != "ALL": query = query.filter(ReviewFlag.status == status.upper())
    if reason: query = query.filter(ReviewFlag.reason == reason.upper())
    if rating: query = query.filter(Review.rating == rating)
    if date_from: query = query.filter(ReviewFlag.created_at >= date_from)
    if date_to: query = query.filter(ReviewFlag.created_at <= date_to)
    if min_reports > 1:
        report_counts = db.query(ReviewFlag.review_id).group_by(ReviewFlag.review_id).having(func.count(ReviewFlag.id) >= min_reports).subquery()
        query = query.filter(ReviewFlag.review_id.in_(db.query(report_counts.c.review_id)))
    if search: query = query.filter(Review.content.ilike(f"%{search[:100]}%"))
    total = query.count(); rows = query.order_by(ReviewFlag.created_at.asc()).offset(offset).limit(limit).all()
    items = []
    for flag, review in rows:
        items.append({"flag_id": str(flag.id), "reason": flag.reason, "explanation": flag.explanation, "flag_status": flag.status, "reported_at": flag.created_at.isoformat(), "report_count": db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id).count(), "review": review_data(db, review, True), "history": [{"action": e.action, "previous_status": e.previous_status, "new_status": e.new_status, "reason": e.reason, "created_at": e.created_at.isoformat()} for e in db.query(ReviewModerationEvent).filter(ReviewModerationEvent.review_id == review.id).order_by(ReviewModerationEvent.created_at).all()]})
    return {"items": items, "total": total}

@admin_router.post("/{review_id}/actions")
def moderate(review_id: uuid.UUID, payload: ActionPayload, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    action = payload.action
    reason = clean(payload.reason or "")
    if action in {"REJECT", "HIDE", "RESTORE"} and len(reason) < 8:
        raise HTTPException(422, "Provide a moderation reason of at least 8 characters.")
    review = db.query(Review).filter(Review.id == review_id).with_for_update().first()
    if review is None: raise HTTPException(404, "Review not found.")
    if action == "DISMISS_FLAG":
        if not payload.flag_id: raise HTTPException(422, "flag_id is required to dismiss a report.")
        flag = db.query(ReviewFlag).filter(ReviewFlag.id == payload.flag_id, ReviewFlag.review_id == review.id).with_for_update().first()
        if flag is None: raise HTTPException(404, "Report not found.")
        if flag.status != "PENDING": raise HTTPException(409, "This report has already been resolved.")
        flag.status, flag.resolution_note, flag.resolved_at = "DISMISSED", reason or None, datetime.now(timezone.utc)
        db.commit(); return {"message": "Report dismissed.", "review_status": review.status}
    previous = review.status
    target = {"APPROVE": "PUBLISHED", "REJECT": "REJECTED", "HIDE": "HIDDEN", "RESTORE": "PUBLISHED"}[action]
    allowed = {"APPROVE": {"PUBLISHED", "PENDING"}, "REJECT": {"PUBLISHED", "PENDING"}, "HIDE": {"PUBLISHED"}, "RESTORE": {"HIDDEN"}}
    if previous not in allowed[action]: raise HTTPException(409, f"Cannot {action.lower()} a review in {previous} status.")
    if action == "APPROVE" and previous == "PUBLISHED" and not db.query(ReviewFlag.id).filter(ReviewFlag.review_id == review.id, ReviewFlag.status == "PENDING").first():
        raise HTTPException(409, "This review is already approved and has no pending reports.")
    review.status = target
    db.add(ReviewModerationEvent(review_id=review.id, admin_id=admin.id, action=action, previous_status=previous, new_status=target, reason=reason or None))
    customer = db.query(User).filter_by(id=review.customer_id).first()
    if customer:
        create_notification(db, customer, notification_type="review.moderated", category="REVIEW",
            title="Review moderation update", message=f"Your review status is now {target.lower()}.",
            restaurant_id=review.restaurant_id, entity_type="review", entity_id=review.id,
            action_url="/customer/profile/reviews", priority="HIGH",
            dedupe_key=f"review:{review.id}:moderated:{target}")
    if action in {"APPROVE", "REJECT", "HIDE", "RESTORE"}:
        for flag in db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id, ReviewFlag.status == "PENDING").with_for_update().all():
            flag.status = "RESOLVED"; flag.resolved_at = datetime.now(timezone.utc); flag.resolution_note = f"Review {target.lower()}ed by platform moderation."
    db.commit()
    return {"message": f"Review {action.lower()}d successfully.", "previous_status": previous, "status": target}
