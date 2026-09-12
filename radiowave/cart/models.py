"""Virtual cart state at EPC level. No prices, payment or settlement in Foundation v0.

A cart is one *lifecycle* of one shopper track: it opens on the first attribution
and closes when the shopper exits with merchandise. A track that re-enters the
store after exiting gets a fresh cart (``<track>#2``, ``<track>#3``...); the
exited cart is preserved untouched as the settlement candidate.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from radiowave.contracts._base import ContractModel, FrozenModel, UtcDatetime
from radiowave.contracts.store import EPC


class CartStatus(StrEnum):
    OPEN = "OPEN"
    EXITED = "EXITED"


class CartLine(FrozenModel):
    epc: EPC
    gtin: str | None = None
    added_at: UtcDatetime
    source_event_id: str
    final_ownership_candidate: bool = Field(
        default=False, description="Set on EXIT_WITH_ITEM; preserved for future settlement"
    )
    exit_event_at: UtcDatetime | None = Field(
        default=None, description="Timestamp of the EXIT_WITH_ITEM event that froze the line"
    )


class Cart(ContractModel):
    cart_id: str = Field(
        min_length=1,
        description="Shopper track id for the first lifecycle, '<track>#<n>' for re-entries",
    )
    shopper_track_id: str = Field(min_length=1)
    status: CartStatus = CartStatus.OPEN
    lines: dict[str, CartLine] = Field(default_factory=dict, description="Keyed by EPC value")
    exited_at: UtcDatetime | None = None

    @property
    def epcs(self) -> set[str]:
        return set(self.lines)


class UnresolvedItem(FrozenModel):
    epc: EPC
    reason: str
    source_event_id: str
    timestamp: UtcDatetime


class CartState(ContractModel):
    carts: dict[str, Cart] = Field(default_factory=dict, description="Keyed by cart id")
    current_cart_ids: dict[str, str] = Field(
        default_factory=dict, description="Shopper track id -> its most recent cart id"
    )
    unresolved: dict[str, UnresolvedItem] = Field(default_factory=dict, description="Keyed by EPC")
    applied_event_ids: list[str] = Field(default_factory=list)

    def owner_of(self, epc: EPC) -> str | None:
        """Cart id currently holding the EPC (open or exited), if any."""
        for cart in self.carts.values():
            if epc.value in cart.lines:
                return cart.cart_id
        return None

    def current_cart(self, shopper_track_id: str) -> Cart | None:
        cart_id = self.current_cart_ids.get(shopper_track_id)
        return self.carts.get(cart_id) if cart_id else None

    def cart_for(self, shopper_track_id: str) -> Cart:
        """The shopper's open cart, opening a new lifecycle if the last one has exited."""
        current = self.current_cart(shopper_track_id)
        if current is not None and current.status == CartStatus.OPEN:
            return current
        lifecycles = sum(1 for c in self.carts.values() if c.shopper_track_id == shopper_track_id)
        cart_id = shopper_track_id if lifecycles == 0 else f"{shopper_track_id}#{lifecycles + 1}"
        cart = Cart(cart_id=cart_id, shopper_track_id=shopper_track_id)
        self.carts[cart_id] = cart
        self.current_cart_ids[shopper_track_id] = cart_id
        return cart

    def epcs_in(self, shopper_track_id: str) -> set[str]:
        """EPCs in the shopper's most recent cart (open or exited)."""
        cart = self.current_cart(shopper_track_id)
        return cart.epcs if cart else set()
