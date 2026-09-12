"""In-memory EPC-level cart engine with idempotent event application."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from radiowave.cart.models import Cart, CartLine, CartState, CartStatus, UnresolvedItem
from radiowave.contracts.events import CartEvent, CartEventType, RetailEvent, RetailEventType


class InMemoryCartEngine:
    """Applies committed retail events to carts.

    Idempotency: an ``event_id`` is applied at most once; a PICK of an EPC already
    owned by the same shopper, or a removal of an EPC not in any cart, is a no-op.
    Carts are per shopper *lifecycle*: after EXIT_WITH_ITEM the cart is frozen and
    any later attribution to the same track opens a new cart.
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
        if owner is not None and self._shopper_of(owner) == event.shopper_track_id:
            return self._emit(event, CartEventType.NOOP, cart_id=owner, note="already in cart")
        if owner is not None:
            del self.state.carts[owner].lines[event.epc.value]
        cart_id = self._add_line(event, event.shopper_track_id)
        self.state.unresolved.pop(event.epc.value, None)
        return self._emit(event, CartEventType.ADD, cart_id=cart_id)

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
        cart_id = self._add_line(event, receiver)
        return self._emit(
            event, CartEventType.TRANSFER, cart_id=cart_id, from_cart_id=owner, note=note
        )

    def _exit(self, event: RetailEvent) -> CartEvent:
        owner = self.state.owner_of(event.epc)
        shopper = event.shopper_track_id or (self._shopper_of(owner) if owner else None)
        if shopper is None:
            return self._unresolved(event, "item exited without an attributed shopper")
        if owner is None or self._shopper_of(owner) != shopper:
            if owner is not None:
                del self.state.carts[owner].lines[event.epc.value]
            owner = self._add_line(event, shopper, for_exit=True)
        cart = self.state.carts[owner]
        line = cart.lines[event.epc.value]
        # Item-level fact only: the line is frozen as the settlement candidate. The cart
        # itself closes through close_cart() when the shopper's session exits.
        cart.lines[event.epc.value] = line.model_copy(
            update={"final_ownership_candidate": True, "exit_event_at": event.timestamp}
        )
        return self._emit(event, CartEventType.EXIT_HOLD, cart_id=cart.cart_id)

    def close_carts_with_exit_candidates(self, shopper_gone: Callable[[str], bool]) -> None:
        """Freeze open carts holding an EXIT_WITH_ITEM line whose shopper is no longer seen.

        Used only when the shopper's session never ended (track lost at the door). The
        preferred stamp is the latest exit event, but only when it is not older than the
        newest line (otherwise the exit evidence is inconsistent with the cart). A gone
        shopper must still reach a terminal state even then: the cart still closes, stamped
        with the later of the newest line and the latest exit event, so a stale exit does
        not leave the cart open forever.
        """
        for cart in self.state.carts.values():
            if cart.status != CartStatus.OPEN or not shopper_gone(cart.shopper_track_id):
                continue
            if not any(line.final_ownership_candidate for line in cart.lines.values()):
                continue
            stamp = self._consistent_exit_stamp(cart)
            if stamp is None:
                exits = [line.exit_event_at for line in cart.lines.values() if line.exit_event_at]
                newest_line = max(line.added_at for line in cart.lines.values())
                stamp = max(newest_line, *exits) if exits else newest_line
            cart.status = CartStatus.EXITED
            cart.exited_at = stamp

    def close_cart(self, shopper_track_id: str, session_ended_at: datetime) -> None:
        """Freeze the shopper's CURRENT cart because their session ended.

        The cart is stamped with the session end; item-level exit times live on the
        lines (``exit_event_at``). For direct callers/tests only; a caller that knows
        which session ended (the pipeline) should use ``close_cart_by_id`` instead, since
        the shopper's *current* cart may already belong to a later, still-active session.
        """
        cart = self.state.current_cart(shopper_track_id)
        if cart is None or cart.status != CartStatus.OPEN:
            return
        cart.status = CartStatus.EXITED
        cart.exited_at = session_ended_at

    def exit_stamp_or(self, shopper_track_id: str, fallback: datetime) -> datetime:
        """Latest consistent EXIT_WITH_ITEM time of the shopper's CURRENT cart, else fallback."""
        cart = self.state.current_cart(shopper_track_id)
        if cart is None:
            return fallback
        stamp = self._consistent_exit_stamp(cart)
        return stamp if stamp is not None else fallback

    def close_cart_by_id(self, cart_id: str, session_ended_at: datetime) -> None:
        """Freeze one specific cart (by id) because the session it belongs to ended."""
        cart = self.state.carts.get(cart_id)
        if cart is None or cart.status != CartStatus.OPEN:
            return
        cart.status = CartStatus.EXITED
        cart.exited_at = session_ended_at

    def exit_stamp_or_by_id(self, cart_id: str, fallback: datetime) -> datetime:
        """Latest consistent EXIT_WITH_ITEM time of one specific cart, else fallback."""
        cart = self.state.carts.get(cart_id)
        if cart is None:
            return fallback
        stamp = self._consistent_exit_stamp(cart)
        return stamp if stamp is not None else fallback

    @staticmethod
    def _consistent_exit_stamp(cart: Cart) -> datetime | None:
        exits = [line.exit_event_at for line in cart.lines.values() if line.exit_event_at]
        if not exits:
            return None
        stamp = max(exits)
        newest_line = max(line.added_at for line in cart.lines.values())
        return stamp if stamp >= newest_line else None

    # --- helpers ---------------------------------------------------------------
    def _shopper_of(self, cart_id: str) -> str:
        return self.state.carts[cart_id].shopper_track_id

    def _add_line(self, event: RetailEvent, shopper_track_id: str, for_exit: bool = False) -> str:
        cart = self.state.current_cart(shopper_track_id) if for_exit else None
        if cart is None or cart.status != CartStatus.OPEN:
            cart = self.state.cart_for(shopper_track_id)
        cart.lines[event.epc.value] = CartLine(
            epc=event.epc,
            gtin=self._gtin_lookup.get(event.epc.value),
            added_at=event.timestamp,
            source_event_id=event.event_id,
        )
        return cart.cart_id

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
