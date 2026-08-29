"""Support threads — conversations and the messages in them.

Both tables were empty. `conversations` and `messages` are listed in the
seeder's own truncate inventory, which reads as though something filled them;
nothing did.

The `messages` table is shaped for an ASSISTANT, not just a human agent: it
carries a `MessageRole` of user/assistant/tool plus `tool_name` and a JSONB
`tool_payload`. So a thread here is not a chat log with two speakers — it is a
record of an assistant answering a customer and, in one case, calling a tool to
do it. Seeding only user-and-assistant text would leave the tool columns empty
and the shape of the table unexplained.

Every thread is attached to a real order belonging to the customer who opened it,
because that is the only kind that exercises the join: `conversations.order_id`
is `SET NULL` on delete, and a thread with no order is the "general question"
case, which is seeded too but is the minority.
"""

import logging
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MessageRole, OrderStatus
from app.models.order import Order
from app.models.support import Conversation, Message

logger = logging.getLogger(__name__)

#: How many threads to open. Small on purpose: support volume is a fraction of
#: order volume, and a hundred identical threads teach a reviewer nothing.
THREAD_COUNT = 14

#: The share opened without an order behind them — "how do I change my number".
NO_ORDER_SHARE = 0.2

#: Threads left open rather than resolved, so both states render.
OPEN_SHARE = 0.3

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

#: (opening question, assistant reply, optional (tool_name, tool_payload))
#:
#: Written against the statuses the seeded orders actually reach, so a thread
#: never asks about something that could not have happened. The tool call is on
#: the refund thread because that is the one where an assistant genuinely has to
#: go and look something up rather than answer from the message.
ORDER_THREADS: tuple[tuple[str, str, tuple[str, dict] | None], ...] = (
    (
        "My order is late. Where is the rider?",
        "The rider has picked up your order and is about eight minutes away. "
        "I have shared your building note with them.",
        ("lookup_delivery", {"fields": ["status", "eta_minutes", "partner_name"]}),
    ),
    (
        "One item is missing from what arrived.",
        "Sorry about that. I have raised a refund for the missing item — it "
        "should be back on your original payment method within 48 hours.",
        ("initiate_refund", {"reason": "item_unavailable", "scope": "line_item"}),
    ),
    (
        "Can I change the delivery address? I am at work.",
        "The kitchen has already started cooking, so the address is locked for "
        "this order. I can cancel it free of charge if you would rather reorder.",
        None,
    ),
    (
        "The food arrived cold.",
        "That should not happen. I have passed this to the restaurant and "
        "applied a credit to your account for the inconvenience.",
        None,
    ),
    (
        "I was charged twice for this order.",
        "I can see one authorization and one capture — the first is a hold your "
        "bank will release within three working days, not a second charge.",
        ("lookup_payments", {"fields": ["status", "amount", "authorized_at"]}),
    ),
    (
        "Why was my coupon not applied?",
        "The code had already been used on an earlier order, and it is limited "
        "to one per customer. I have sent you a fresh one.",
        ("lookup_coupon", {"fields": ["usage_limit_per_user", "times_used"]}),
    ),
)

GENERAL_THREADS: tuple[tuple[str, str], ...] = (
    (
        "How do I change the phone number on my account?",
        "You can update it under Profile. Orders already placed keep the number "
        "they were placed with, so the rider still reaches you.",
    ),
    (
        "Do you deliver to Whitefield?",
        "Not yet from the kitchens near you. New areas are added as restaurants "
        "come on board.",
    ),
)


async def build(session: AsyncSession, rng: random.Random) -> dict[str, int]:
    """Open a handful of threads, most of them against a real order."""
    # Settled orders only: a support thread is written after something happened,
    # and "the food arrived cold" needs an order that actually arrived.
    orders = list(
        await session.scalars(
            select(Order)
            .where(
                Order.status.in_(
                    [OrderStatus.DELIVERED, OrderStatus.CANCELLED]
                )
            )
            .order_by(Order.id)
        )
    )
    if not orders:
        logger.warning("No settled orders — skipping support threads.")
        return {"conversations": 0, "messages": 0, "tool_calls": 0}

    threads = messages = tool_calls = 0
    used_orders: set[int] = set()

    for _ in range(THREAD_COUNT):
        wants_order = rng.random() > NO_ORDER_SHARE

        if wants_order:
            # One thread per order, so a customer is never shown two threads
            # about the same thing.
            candidates = [o for o in orders if o.id not in used_orders]
            if not candidates:
                break
            order = rng.choice(candidates)
            used_orders.add(order.id)
            question, answer, tool = rng.choice(ORDER_THREADS)
            user_id, order_id = order.user_id, order.id
        else:
            order = rng.choice(orders)
            question, answer = rng.choice(GENERAL_THREADS)
            tool = None
            # A general question still belongs to a real customer; only the
            # order link is absent.
            user_id, order_id = order.user_id, None

        conversation = Conversation(
            user_id=user_id,
            order_id=order_id,
            status=STATUS_OPEN if rng.random() < OPEN_SHARE else STATUS_RESOLVED,
        )
        session.add(conversation)
        await session.flush()  # the messages need the generated id
        threads += 1

        # Order matters and is the only thing that orders a thread: `messages`
        # has no sort column, so a reader goes by created_at, and these are added
        # in sequence within one flush. Ids ascend with insertion, which is what
        # any listing will fall back on.
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=question,
            )
        )
        messages += 1

        if tool is not None:
            tool_name, payload = tool
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=MessageRole.TOOL,
                    # A tool message carries no prose: the payload IS the
                    # content, and a reader renders it as a step rather than a
                    # sentence.
                    content=None,
                    tool_name=tool_name,
                    tool_payload={**payload, "order_id": order_id},
                )
            )
            messages += 1
            tool_calls += 1

        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=answer,
            )
        )
        messages += 1

    await session.flush()
    return {
        "conversations": threads,
        "messages": messages,
        "tool_calls": tool_calls,
    }
