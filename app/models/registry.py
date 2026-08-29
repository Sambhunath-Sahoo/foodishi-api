"""Single import point that registers every model on Base.metadata.

Alembic's autogenerate and create_all only see mapped classes that have been
imported. Importing them here means one line in env.py and one in main.py,
instead of a list that silently goes stale when a model is added.
"""

from app.db import Base
from app.models.address import Address
from app.models.application import RestaurantApplication
from app.models.catalog import (
    Cuisine,
    MenuCategory,
    MenuItem,
    Restaurant,
    RestaurantPolicy,
    restaurant_cuisines,
)
from app.models.coupon import Coupon, CouponRedemption
from app.models.delivery import Delivery, DeliveryPartner
from app.models.finance import Settlement
from app.models.media import MenuItemImage
from app.models.modifiers import (
    MenuItemModifierGroup,
    MenuItemModifierOption,
    OrderItemModifier,
    menu_item_modifier_links,
)
from app.models.order import Order, OrderItem, OrderStatusEvent
from app.models.payment import Payment, Refund
from app.models.platform import PlatformStaff
from app.models.review import Review
from app.models.settings import PlatformSettings
from app.models.staff import RestaurantStaff
from app.models.support import Conversation, Message
from app.models.user import User

__all__ = [
    "Address",
    "Base",
    "Conversation",
    "Coupon",
    "CouponRedemption",
    "Cuisine",
    "Delivery",
    "DeliveryPartner",
    "MenuCategory",
    "MenuItem",
    "MenuItemImage",
    "MenuItemModifierGroup",
    "MenuItemModifierOption",
    "Message",
    "Order",
    "OrderItem",
    "OrderItemModifier",
    "OrderStatusEvent",
    "Payment",
    "PlatformSettings",
    "PlatformStaff",
    "Refund",
    "Restaurant",
    "RestaurantApplication",
    "RestaurantPolicy",
    "RestaurantStaff",
    "Review",
    "Settlement",
    "User",
    "menu_item_modifier_links",
    "restaurant_cuisines",
]
