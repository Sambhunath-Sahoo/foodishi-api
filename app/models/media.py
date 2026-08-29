from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin

MAX_IMAGES_PER_ITEM = 7
STORAGE_BUCKET = "menu-images"


class MenuItemImage(Base, TimestampMixin):
    """Up to seven photos per dish, ordered. Position 0 is the cover.

    The seven-image cap is structural: sort_order is constrained to 0..6 and is
    unique per dish, so the database itself cannot hold an eighth row. No trigger,
    and no service check that a future endpoint can forget to call.
    """

    __tablename__ = "menu_item_images"
    __table_args__ = (
        CheckConstraint(
            f"sort_order BETWEEN 0 AND {MAX_IMAGES_PER_ITEM - 1}",
            name="ck_menu_item_images_max_seven",
        ),
        # DEFERRABLE because reordering is a permutation of 0..n-1: every
        # intermediate state collides, and sort_order is also CHECKed to 0..6 so
        # there is nowhere out of range to park rows. Deferring to COMMIT still
        # guarantees no duplicate position survives a transaction.
        UniqueConstraint(
            "menu_item_id",
            "sort_order",
            name="uq_menu_item_images_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_menu_item_images_item", "menu_item_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE")
    )
    # A path, not a URL: the CDN host can change without rewriting every row.
    storage_path: Mapped[str] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
