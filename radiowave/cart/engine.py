"""In-memory EPC-level cart engine with idempotent event application."""

from __future__ import annotations

from radiowave.cart.models import CartLine, CartState, CartStatus, UnresolvedItem
from radiowave.contracts.events import CartEvent, CartEventType, RetailEvent, RetailEventType


class InMemoryCartEngine:
    """Applies committed retail events to carts.

    Idempotency: an ``event_id`` is applied at most once; a PICK of an EPC already
    in the same cart, or a removal of an EPC not in any cart, is a no-op.
    """

    def __init__(self, gtin_lookup: dict[str, str] | None = None) -> None:
        self.state = CartState()
        self._applied: set[str] = set()
        self._gtin_lookup = gtin_lookup or {}
        self.cart_events: list[CartEvent] = []

    def apply(self, event: RetailEvent) -> CartEvent:
        if event.event_id in self._applied:
            return self._emit(event, CartEventType.NOOP_DUPLICATE, note="event already applied")
        self._applied.add(event.event_id)
        self.state.applied_event_ids.append(event.event_id)
        handler = {
            RetailEventType.PICK: self._pick,
            RetailEventType.CARRY: self._carry,
            RetailEventType.PUTBACK: self._remove,
            RetailEventType.MISPLACE: self._misplace,
            RetailEventType.HANDOFF: self._handoff,
            RetailEventType.EXIT_WITH_ITEM: self._exit,
        }[event.event_type]
        return handler(event)

    # --- handlers ------------------------------------------------------------
    def _pick(self, event: RetailEvent) -> CartEvent:
        if event.shopper_track_id is None:
            return self._unresolved(event, "PICK without an attributed shopper")
        owner = self.state.owner_of(event.epc)
        if owner == event.shopper_track_id:
            return self._emit(event, CartEventType.NOOP, cart_id=owner, note="already in cart")
        if owner is not None:
            del self.state.carts[owner].lines[event.epc.value]
        self._add_line(event, event.shopper_track_id)
        self.state.unresolved.pop(event.epc.value, None)
        return self._emit(event, CartEventType.ADD, cart_id=event.shopper_track_id)

    def _carry(self, event: RetailEvent) -> CartEvent:
        owner = self.state.owner_of(event.epc)
        return self._emit(event, CartEventType.NOOP, cart_id=owner, note="carry acknowledged")

    def _remove(self, event: RetailEvent) -> CartEvent:
        owner = self.state.owner_of(event.epc)
        if owner is None:
            self.state.unresolved.pop(event.epc.value, None)
            return self._emit(event, CartEventType.NOOP, note="item was not in any cart")
        del self.state.carts[owner].lines[event.epc.value]
        return self._emit(event, CartEventType.REMOVE, cart_id=owner)

    def _misplace(self, event: RetailEvent) -> CartEvent:
        owner = self.state.owner_of(event.epc)
        if owner is None:
            return self._unresolved(event, "misplaced item had no attributed carrier")
        del self.state.carts[owner].lines[event.epc.value]
        return self._emit(event, CartEventType.REMOVE, cart_id=owner, note="misplaced")

    def _handoff(self, event: RetailEvent) -> CartEvent:
        receiver = event.counterpart_track_id
        if receiver is None:
            return self._unresolved(event, "HANDOFF without a receiving shopper")
        owner = self.state.owner_of(event.epc)
        note = ""
        if owner is not None:
            del self.state.carts[owner].lines[event.epc.value]
        else:
            note = "item was not in the giver's cart; added to receiver"
        self._add_line(event, receiver)
        return self._emit(
            event, CartEventType.TRANSFER, cart_id=receiver, from_cart_id=owner, note=note
        )

    def _exit(self, event: RetailEvent) -> CartEvent:
        owner = self.state.owner_of(event.epc)
        shopper = event.shopper_track_id or owner
        if shopper is None:
            return self._unresolved(event, "item exited without an attributed shopper")
        if owner != shopper:
            if owner is not None:
                del self.state.carts[owner].lines[event.epc.value]
            self._add_line(event, shopper)
        cart = self.state.cart_for(shopper)
        line = cart.lines[event.epc.value]
        cart.lines[event.epc.value] = line.model_copy(update={"final_ownership_candidate": True})
        cart.status = CartStatus.EXITED
        cart.exited_at = event.timestamp
        return self._emit(event, CartEventType.EXIT_HOLD, cart_id=shopper)

    # --- helpers ---------------------------------------------------------------
    def _add_line(self, event: RetailEvent, cart_id: str) -> None:
        cart = self.state.cart_for(cart_id)
        cart.lines[event.epc.value] = CartLine(
            epc=event.epc,
            gtin=self._gtin_lookup.get(event.epc.value),
            added_at=event.timestamp,
            source_event_id=event.event_id,
        )

    def _unresolved(self, event: RetailEvent, reason: str) -> CartEvent:
        self.state.unresolved[event.epc.value] = UnresolvedItem(
            epc=event.epc, reason=reason, source_event_id=event.event_id, timestamp=event.timestamp
        )
        return self._emit(event, CartEventType.UNRESOLVED, note=reason)

    def _emit(
        self,
        event: RetailEvent,
        kind: CartEventType,
        cart_id: str | None = None,
        from_cart_id: str | None = None,
        note: str = "",
    ) -> CartEvent:
        cart_event = CartEvent(
            cart_event_id=f"cart_{event.event_id}_{kind.value.lower()}",
            cart_event_type=kind,
            timestamp=event.timestamp,
            epc=event.epc,
            cart_id=cart_id,
            from_cart_id=from_cart_id,
            source_event_id=event.event_id,
            note=note,
        )
        self.cart_events.append(cart_event)
        return cart_event
