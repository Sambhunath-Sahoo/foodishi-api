import enum


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY_FOR_PICKUP = "ready_for_pickup"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentMethod(enum.StrEnum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    COD = "cod"


class RefundStatus(enum.StrEnum):
    INITIATED = "initiated"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RefundReason(enum.StrEnum):
    CANCELLED_BY_USER = "cancelled_by_user"
    CANCELLED_BY_RESTAURANT = "cancelled_by_restaurant"
    ITEM_UNAVAILABLE = "item_unavailable"
    LATE_DELIVERY = "late_delivery"
    QUALITY_ISSUE = "quality_issue"


class DiscountType(enum.StrEnum):
    FLAT = "flat"
    PERCENT = "percent"


class CouponScope(enum.StrEnum):
    GLOBAL = "global"
    RESTAURANT = "restaurant"
    CUISINE = "cuisine"


class DeliveryStatus(enum.StrEnum):
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    FAILED = "failed"


class SpiceLevel(enum.StrEnum):
    NONE = "none"
    MILD = "mild"
    MEDIUM = "medium"
    HOT = "hot"


class ActorType(enum.StrEnum):
    USER = "user"
    RESTAURANT = "restaurant"
    SYSTEM = "system"
    AGENT = "agent"


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StaffRole(enum.StrEnum):
    """Who may act for one restaurant.

    Two tiers, collapsed from three. `owner` and `manager` were separate ranks
    and nothing in the product ever distinguished them usefully, so both became
    `admin`; the old values still exist in the Postgres enum type, unused, since
    dropping an enum value is not something Postgres offers.
    """

    STAFF = "staff"        # the order queue: accept, advance, hand over
    ADMIN = "admin"        # + the menu, this kitchen's coupons, and the roster


class PlatformRole(enum.StrEnum):
    """Who works for Foodishi itself. Unrelated to StaffRole, which is per-restaurant.

    One role today, deliberately. A read-only support tier and a coupons-and-
    onboarding ops tier were both sketched and removed: nobody holds them yet,
    and an unused privilege level is a check that has never been exercised.
    The rank ladder below is kept so adding one is a two-line change.
    """

    ADMIN = "admin"        # everything on the platform side


class SettlementStatus(enum.StrEnum):
    """Where a restaurant's payout has got to.

    There is no bank integration and no gateway payout call. `paid` means a row
    was stamped, not that money moved — the record is the deliverable, and the
    transfer is somebody else's system to build.
    """

    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"


class ModifierKind(enum.StrEnum):
    """How a group of choices behaves on a dish.

    VARIANT is one-of and must be answered — a biryani is a half plate or a
    full one, never neither and never both. ADDON is any-of and may be skipped.
    """

    VARIANT = "variant"
    ADDON = "addon"
