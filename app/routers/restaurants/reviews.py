import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.menu import Food
from app.models.orders import FoodOrder
from app.models.restaurant import Restaurant
from app.models.reviews import Review, ReviewFlag, ReviewModerationEvent, ReviewReply
from app.models.user import User
from app.routers.auth.customer_auth import current_customer
from app.routers.auth.chef_auth import current_chef
from app.routers.auth.manager_auth import current_manager
from app.routers.auth.admin_auth import current_admin
from app.services.notifications import create_notification

customer_router = APIRouter(prefix="/api/customer/reviews", tags=["customer reviews"])
public_router = APIRouter(prefix="/api/reviews", tags=["reviews"])
chef_router = APIRouter(prefix="/api/chef/reviews", tags=["chef reviews"])
manager_router = APIRouter(prefix="/api/manager/reviews", tags=["manager reviews"])
admin_router = APIRouter(prefix="/api/admin/reviews", tags=["admin review moderation"])

class ReviewPayload(BaseModel):
    restaurant_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    food_id: uuid.UUID | None = None
    rating: int = Field(ge=1, le=5)
    content: str = Field(min_length=3, max_length=5000)

class FlagPayload(BaseModel):
    reason: str = Field(pattern="^(SPAM|ABUSIVE|HARASSMENT|MISLEADING|OTHER)$")
    explanation: str | None = Field(default=None, max_length=1000)

class ActionPayload(BaseModel):
    action: str = Field(pattern="^(APPROVE|REJECT|HIDE|RESTORE|DISMISS_FLAG|DELETE)$")
    reason: str | None = Field(default=None, max_length=1000)
    flag_id: uuid.UUID | None = None

def clean(value: str) -> str:
    return " ".join(value.split())

def review_data(db: Session, review: Review, private: bool = False) -> dict:
    restaurant = db.get(Restaurant, review.restaurant_id)
    food = db.get(Food, review.food_id) if review.food_id else None
    customer = db.get(User, review.customer_id)
    data = {"id": str(review.id), "restaurant_id": str(review.restaurant_id), "restaurant_name": restaurant.name if restaurant else None,
        "food_id": str(review.food_id) if review.food_id else None, "food_name": food.name if food else None,
        "order_id": str(review.order_id) if review.order_id else None, "rating": review.rating, "content": review.content,
        "customer_name": customer.name if customer else "Customer",
        "created_at": review.created_at.isoformat(), "updated_at": review.updated_at.isoformat()}
    if private:
        data.update(status=review.status, is_mine=True)
        manager_flag = db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id, ReviewFlag.manager_id.is_not(None)).order_by(ReviewFlag.created_at.desc()).first()
        if manager_flag:
            flagged_at = manager_flag.created_at
            if flagged_at.tzinfo is None:
                flagged_at = flagged_at.replace(tzinfo=timezone.utc)
            data["manager_flagged_until"] = (flagged_at + timedelta(hours=24)).isoformat()
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
    order = None
    restaurant_id = payload.restaurant_id
    if payload.order_id:
        order = db.query(FoodOrder).filter(FoodOrder.id == payload.order_id, FoodOrder.customer_id == customer.id).with_for_update().first()
        if order is None:
            raise HTTPException(404, "Eligible order not found.")
        if order.status != "COMPLETED":
            raise HTTPException(409, "You can review a restaurant after your visit is completed.")
        if restaurant_id and restaurant_id != order.restaurant_id:
            raise HTTPException(422, "The order does not belong to this restaurant.")
        restaurant_id = order.restaurant_id
    if restaurant_id is None:
        raise HTTPException(422, "Choose a restaurant to review.")
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if restaurant is None:
        raise HTTPException(404, "Restaurant not found.")
    cooldown_start = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_manager_flag = db.query(ReviewFlag.id).join(Review, Review.id == ReviewFlag.review_id).filter(
        Review.customer_id == customer.id,
        Review.restaurant_id == restaurant_id,
        ReviewFlag.manager_id.is_not(None),
        ReviewFlag.created_at >= cooldown_start,
    ).first()
    if recent_manager_flag:
        raise HTTPException(429, "You cannot submit a review for 24 hours after a review is flagged by the restaurant manager.")
    if db.query(Review.id).filter(Review.customer_id == customer.id, Review.restaurant_id == restaurant_id, Review.status != "REMOVED").first():
        raise HTTPException(409, "You have already reviewed this restaurant. Edit your existing review instead.")
    if payload.food_id:
        raise HTTPException(422, "Reviews are for the restaurant, not individual menu items.")
    review = Review(customer_id=customer.id, restaurant_id=restaurant_id, order_id=order.id if order else None, food_id=payload.food_id, rating=payload.rating, content=content)
    db.add(review)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "You have already reviewed this restaurant or order.") from exc
    create_notification(db, customer, notification_type="review.submitted", category="REVIEW",
        title="Review submitted", message="Your review was submitted successfully.", restaurant_id=restaurant_id,
        entity_type="review", entity_id=review.id, action_url="/customer/profile/reviews",
        dedupe_key=f"review:{review.id}:submitted:customer")
    manager = db.query(User).filter_by(id=restaurant.manager_id).first()
    if manager:
        create_notification(db, manager, notification_type="review.submitted", category="REVIEW",
            title="New customer review", message="A customer left a review for your restaurant.",
            restaurant_id=restaurant_id, entity_type="review", entity_id=review.id,
            action_url="/manager/reviews", dedupe_key=f"review:{review.id}:submitted")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You have already reviewed this restaurant.")
    db.refresh(review)
    return review_data(db, review, True)

@customer_router.patch("/{review_id}")
def update_review(review_id: uuid.UUID, payload: ReviewPayload, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    review = db.query(Review).filter(Review.id == review_id, Review.customer_id == customer.id).with_for_update().first()
    if review is None:
        raise HTTPException(404, "Review not found.")
    if review.status != "PUBLISHED":
        raise HTTPException(409, "This review is locked while it is under moderation or restricted.")
    if payload.restaurant_id and payload.restaurant_id != review.restaurant_id:
        raise HTTPException(409, "A review cannot be moved to another restaurant.")
    if payload.food_id:
        raise HTTPException(422, "Reviews are for the restaurant, not individual menu items.")
    content = clean(payload.content)
    if len(content) < 3:
        raise HTTPException(422, "Review must contain at least 3 non-space characters.")
    review.food_id, review.rating, review.content = payload.food_id, payload.rating, content
    db.commit(); db.refresh(review)
    return review_data(db, review, True)

@customer_router.delete("/{review_id}")
def delete_own_review(review_id: uuid.UUID, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    review = db.query(Review).filter(Review.id == review_id, Review.customer_id == customer.id).with_for_update().first()
    if review is None:
        raise HTTPException(404, "Review not found.")
    db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id).delete(synchronize_session=False)
    db.query(ReviewReply).filter(ReviewReply.review_id == review.id).delete(synchronize_session=False)
    db.query(ReviewModerationEvent).filter(ReviewModerationEvent.review_id == review.id).delete(synchronize_session=False)
    db.delete(review)
    db.commit()
    return {"message": "Your review was deleted."}

@customer_router.post("/{review_id}/flags", status_code=201)
def flag_review(review_id: uuid.UUID, payload: FlagPayload, customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> dict:
    target = db.query(Review).filter(Review.id == review_id, Review.status == "PUBLISHED").first()
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

@manager_router.post("/{review_id}/flags", status_code=201)
def manager_flag_review(review_id: uuid.UUID, payload: FlagPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = manager_restaurant(manager, db)
    target = db.query(Review).filter(Review.id == review_id, Review.restaurant_id == restaurant.id, Review.status == "PUBLISHED").first()
    if target is None:
        raise HTTPException(404, "Published review not found for your restaurant.")
    flag = ReviewFlag(review_id=review_id, manager_id=manager.id, reason=payload.reason,
        explanation=clean(payload.explanation) if payload.explanation else None)
    db.add(flag)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "You have already flagged this review for admin review.") from exc
    for admin in db.query(User).filter(User.role == "ADMIN", User.is_active.is_(True)).all():
        create_notification(db, admin, notification_type="review.flagged", category="REVIEW",
            title="Manager flagged a restaurant review", message=f"{restaurant.name} requested admin review of a customer comment.",
            restaurant_id=restaurant.id, entity_type="review", entity_id=target.id,
            action_url="/admin/reviews", priority="HIGH", dedupe_key=f"review-manager-flag:{flag.id}:admin:{admin.id}")
    db.commit()
    return {"id": str(flag.id), "status": flag.status, "message": "Review flagged for administrator review."}

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
    query = db.query(Review).filter(Review.restaurant_id == restaurant.id, Review.status != "REMOVED")
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
    items = []
    for review in rows:
        item = review_data(db, review, True)
        item["manager_flag_pending"] = bool(db.query(ReviewFlag.id).filter(
            ReviewFlag.review_id == review.id, ReviewFlag.manager_id == manager.id, ReviewFlag.status == "PENDING",
        ).first())
        items.append(item)
    return {"items": items, "total": total, "average_rating": round(float(avg), 2) if avg else None}

@admin_router.get("")
def moderation_queue(admin: User = Depends(current_admin), db: Session = Depends(get_db), status: str = "PENDING", reason: str | None = None, flagged_by: str | None = None, rating: int | None = Query(None, ge=1, le=5), date_from: datetime | None = None, date_to: datetime | None = None, min_reports: int = Query(1, ge=1), search: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)) -> dict:
    query = db.query(ReviewFlag, Review).join(Review, Review.id == ReviewFlag.review_id)
    if status.upper() != "ALL": query = query.filter(ReviewFlag.status == status.upper())
    if reason: query = query.filter(ReviewFlag.reason == reason.upper())
    if flagged_by and flagged_by.upper() == "MANAGER": query = query.filter(ReviewFlag.manager_id.is_not(None))
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
        reporter = db.get(User, flag.manager_id or flag.customer_id)
        items.append({"flag_id": str(flag.id), "reason": flag.reason, "explanation": flag.explanation, "flag_status": flag.status, "flagged_by": "MANAGER" if flag.manager_id else "CUSTOMER", "reporter_name": reporter.name if reporter else "Reporter", "reported_at": flag.created_at.isoformat(), "report_count": db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id).count(), "review": review_data(db, review, True), "history": [{"action": e.action, "previous_status": e.previous_status, "new_status": e.new_status, "reason": e.reason, "created_at": e.created_at.isoformat()} for e in db.query(ReviewModerationEvent).filter(ReviewModerationEvent.review_id == review.id).order_by(ReviewModerationEvent.created_at).all()]})
    return {"items": items, "total": total}

@admin_router.post("/{review_id}/actions")
def moderate(review_id: uuid.UUID, payload: ActionPayload, admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    action = payload.action
    reason = clean(payload.reason or "")
    if action in {"REJECT", "HIDE", "RESTORE"} and len(reason) < 8:
        raise HTTPException(422, "Provide a moderation reason of at least 8 characters.")
    review = db.query(Review).filter(Review.id == review_id).with_for_update().first()
    if review is None: raise HTTPException(404, "Review not found.")
    if action == "DELETE":
        if len(reason) < 8:
            raise HTTPException(422, "Provide a reason of at least 8 characters before deleting a review.")
        manager_flag_query = db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id, ReviewFlag.manager_id.is_not(None), ReviewFlag.status == "PENDING")
        if payload.flag_id:
            manager_flag_query = manager_flag_query.filter(ReviewFlag.id == payload.flag_id)
        manager_flag = manager_flag_query.with_for_update().first()
        if manager_flag is None:
            raise HTTPException(409, "Only a review with a pending manager flag can be deleted by an administrator.")
        if review.status not in {"PUBLISHED", "HIDDEN"}:
            raise HTTPException(409, f"Cannot delete a review in {review.status} status.")
        previous = review.status
        review.status = "REMOVED"
        db.add(ReviewModerationEvent(review_id=review.id, admin_id=admin.id, action="DELETE", previous_status=previous, new_status="REMOVED", reason=reason))
        for flag in db.query(ReviewFlag).filter(ReviewFlag.review_id == review.id, ReviewFlag.status == "PENDING").with_for_update().all():
            flag.status = "RESOLVED"; flag.resolved_at = datetime.now(timezone.utc); flag.resolution_note = f"Review removed by platform moderation: {reason}"
        customer = db.query(User).filter_by(id=review.customer_id).first()
        if customer:
            create_notification(db, customer, notification_type="review.moderated", category="REVIEW",
                title="Review removed after moderation", message="Your restaurant review was removed after administrator review.",
                restaurant_id=review.restaurant_id, entity_type="review", entity_id=review.id,
                action_url="/customer/profile/reviews", priority="HIGH", dedupe_key=f"review:{review.id}:moderated:removed")
        db.commit()
        return {"message": "Review removed from public view.", "previous_status": previous, "status": "REMOVED"}
    if action == "REJECT" and not db.query(ReviewFlag.id).filter(
        ReviewFlag.review_id == review.id, ReviewFlag.manager_id.is_not(None), ReviewFlag.status == "PENDING",
    ).first():
        raise HTTPException(409, "A review can only be rejected for removal after its manager flags it.")
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
