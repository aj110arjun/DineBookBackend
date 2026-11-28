"""Purge expired AI history only: python -m app.services.customer_ai_maintenance --purge."""
import argparse
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import engine
from app.models.customer_ai import CustomerAIConversation
from app.services.customer_ai_conversations import ConversationRepository, now


def purge_expired(bind, execute=False, limit=500):
    with Session(bind) as db:
        rows = db.query(CustomerAIConversation.id).filter(
            CustomerAIConversation.updated_at < now() - timedelta(days=settings.ai_support_retention_days),
        ).order_by(CustomerAIConversation.updated_at).limit(limit).all()
        if execute:
            ConversationRepository.delete_ids(db, [row.id for row in rows])
            db.commit()
        return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove expired customer AI conversations; defaults to a dry run.")
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    count = purge_expired(engine, execute=args.purge)
    print(f"{'Purged' if args.purge else 'Would purge'} {count} expired conversations (batch limit 500).")
