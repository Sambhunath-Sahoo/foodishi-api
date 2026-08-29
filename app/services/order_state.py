from app.models.enums import ActorType, OrderStatus

S = OrderStatus
A = ActorType

# Mirrors the lifecycle diagram in docs/DATABASE_DESIGN.md. Anything absent
# here is illegal, so new states fail loudly instead of slipping through.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.PENDING: frozenset({S.CONFIRMED, S.CANCELLED}),
    S.CONFIRMED: frozenset({S.PREPARING, S.CANCELLED}),
    S.PREPARING: frozenset({S.READY_FOR_PICKUP, S.CANCELLED}),
    S.READY_FOR_PICKUP: frozenset({S.OUT_FOR_DELIVERY, S.CANCELLED}),
    S.OUT_FOR_DELIVERY: frozenset({S.DELIVERED, S.CANCELLED}),
    S.DELIVERED: frozenset(),
    S.CANCELLED: frozenset(),
}

# Who may make each move. A customer cannot mark their own food delivered.
#
# Two things this table is load-bearing for, both easy to miss:
#
#   * (PENDING, CANCELLED) by RESTAURANT is a REJECTION. The lifecycle has no
#     reject state and cannot grow one without a migration, so refusing a
#     ticket the kitchen never accepted is spelled as this move --
#     app/services/ordering.py reject() is the named door onto it.
#   * A.AGENT appears in no entry, so every move claimed by an agent is
#     refused. That is now unreachable over HTTP rather than merely refused:
#     the order routes derive the actor from the authenticated caller
#     (restaurant staff -> RESTAURANT, the order's customer -> USER) and
#     delivery.py hard-codes SYSTEM, so nothing on the wire picks an actor any
#     more. Giving AGENT real entries would be a product decision, not a
#     wiring one.
ACTORS: dict[tuple[OrderStatus, OrderStatus], frozenset[ActorType]] = {
    (S.PENDING, S.CONFIRMED): frozenset({A.RESTAURANT, A.SYSTEM}),
    (S.PENDING, S.CANCELLED): frozenset({A.USER, A.RESTAURANT, A.SYSTEM}),
    (S.CONFIRMED, S.PREPARING): frozenset({A.RESTAURANT, A.SYSTEM}),
    (S.CONFIRMED, S.CANCELLED): frozenset({A.USER, A.RESTAURANT, A.SYSTEM}),
    (S.PREPARING, S.READY_FOR_PICKUP): frozenset({A.RESTAURANT, A.SYSTEM}),
    (S.PREPARING, S.CANCELLED): frozenset({A.USER, A.RESTAURANT, A.SYSTEM}),
    (S.READY_FOR_PICKUP, S.OUT_FOR_DELIVERY): frozenset({A.SYSTEM}),
    (S.READY_FOR_PICKUP, S.CANCELLED): frozenset({A.RESTAURANT, A.SYSTEM}),
    (S.OUT_FOR_DELIVERY, S.DELIVERED): frozenset({A.SYSTEM}),
    (S.OUT_FOR_DELIVERY, S.CANCELLED): frozenset({A.SYSTEM}),
}

TERMINAL = frozenset({S.DELIVERED, S.CANCELLED})

# Spelled out per actor rather than guessed from the first letter: this refusal
# is read by customers and operators, and both rules a guess could use get one
# of these wrong -- "a user" starts with a vowel and "an agent" would not
# survive the consonant rule. Four values, so the table is cheaper than the
# heuristic anyway.
ARTICLES: dict[ActorType, str] = {
    A.USER: "A",
    A.RESTAURANT: "A",
    A.SYSTEM: "A",
    A.AGENT: "An",
}


class TransitionError(ValueError):
    """An illegal move, or a legal move by the wrong actor. Surface as 409."""


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: OrderStatus, target: OrderStatus, actor: ActorType) -> None:
    if current in TERMINAL:
        raise TransitionError(f"Order is {current.value}; no further changes are possible")
    if not can_transition(current, target):
        raise TransitionError(f"Cannot move an order from {current.value} to {target.value}")
    if actor not in ACTORS[(current, target)]:
        raise TransitionError(
            f"{ARTICLES[actor]} {actor.value} may not move an order "
            f"from {current.value} to {target.value}"
        )
