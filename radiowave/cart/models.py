"""Virtual cart state at EPC level. No prices, payment or settlement in Foundation v0."""

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


class Cart(ContractModel):
    cart_id: str = Field(min_length=1, description="Equals the shopper track id in v0")
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
    carts: dict[str, Cart] = Field(default_factory=dict)
    unresolved: dict[str, UnresolvedItem] = Field(default_factory=dict, description="Keyed by EPC")
    applied_event_ids: list[str] = Field(default_factory=list)

    def owner_of(self, epc: EPC) -> str | None:
        for cart in self.carts.values():
            if epc.value in cart.lines:
                return cart.cart_id
        return None

    def cart_for(self, shopper_track_id: str) -> Cart:
        cart = self.carts.get(shopper_track_id)
        if cart is None:
            cart = Cart(cart_id=shopper_track_id)
            self.carts[shopper_track_id] = cart
        return cart

    def epcs_in(self, shopper_track_id: str) -> set[str]:
        cart = self.carts.get(shopper_track_id)
        return cart.epcs if cart else set()
