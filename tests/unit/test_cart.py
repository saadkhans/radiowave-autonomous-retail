from __future__ import annotations

from radiowave.cart.engine import InMemoryCartEngine
from radiowave.cart.models import CartStatus
from radiowave.contracts import EPC, CartEventType, RetailEvent, RetailEventType, make_event_id
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, GTIN_BLACK_SHIRT_L
from tests.conftest import at

A = EPC(value=EPC_SHIRT_A)
B = EPC(value=EPC_SHIRT_B)


def _event(
    kind: RetailEventType,
    epc: EPC,
    shopper: str | None,
    t: float,
    counterpart: str | None = None,
    event_id: str | None = None,
) -> RetailEvent:
    return RetailEvent(
        event_id=event_id or make_event_id(kind, epc, at(t), shopper),
        event_type=kind,
        timestamp=at(t),
        epc=epc,
        shopper_track_id=shopper,
        counterpart_track_id=counterpart,
        confidence=0.9,
    )


def _engine() -> InMemoryCartEngine:
    return InMemoryCartEngine({EPC_SHIRT_A: GTIN_BLACK_SHIRT_L, EPC_SHIRT_B: GTIN_BLACK_SHIRT_L})


def test_pick_is_idempotent_per_event_and_per_state() -> None:
    engine = _engine()
    pick = _event(RetailEventType.PICK, A, "P0001", 1.0)
    assert engine.apply(pick).cart_event_type == CartEventType.ADD
    assert engine.apply(pick).cart_event_type == CartEventType.NOOP_DUPLICATE
    again = _event(RetailEventType.PICK, A, "P0001", 2.0)  # different id, same fact
    assert engine.apply(again).cart_event_type == CartEventType.NOOP
    assert engine.state.epcs_in("P0001") == {EPC_SHIRT_A}
    assert engine.state.carts["P0001"].lines[EPC_SHIRT_A].gtin == GTIN_BLACK_SHIRT_L


def test_identical_sku_units_are_separate_cart_lines() -> None:
    engine = _engine()
    engine.apply(_event(RetailEventType.PICK, A, "P0001", 1.0))
    engine.apply(_event(RetailEventType.PICK, B, "P0002", 1.5))
    assert engine.state.epcs_in("P0001") == {EPC_SHIRT_A}
    assert engine.state.epcs_in("P0002") == {EPC_SHIRT_B}
    engine.apply(_event(RetailEventType.PUTBACK, B, "P0002", 3.0))
    assert engine.state.epcs_in("P0001") == {EPC_SHIRT_A}
    assert engine.state.epcs_in("P0002") == set()


def test_putback_and_misplace_remove_ownership() -> None:
    engine = _engine()
    engine.apply(_event(RetailEventType.PICK, A, "P0001", 1.0))
    assert engine.apply(_event(RetailEventType.PUTBACK, A, "P0001", 2.0)).cart_event_type == (
        CartEventType.REMOVE
    )
    assert engine.state.epcs_in("P0001") == set()
    # MISPLACE of an unattributed item is recorded as unresolved, not silently ignored.
    result = engine.apply(_event(RetailEventType.MISPLACE, B, None, 3.0))
    assert result.cart_event_type == CartEventType.UNRESOLVED
    assert EPC_SHIRT_B in engine.state.unresolved


def test_handoff_transfers_between_shoppers() -> None:
    engine = _engine()
    engine.apply(_event(RetailEventType.PICK, A, "P0001", 1.0))
    result = engine.apply(_event(RetailEventType.HANDOFF, A, "P0001", 5.0, counterpart="P0002"))
    assert result.cart_event_type == CartEventType.TRANSFER
    assert result.from_cart_id == "P0001" and result.cart_id == "P0002"
    assert engine.state.epcs_in("P0001") == set()
    assert engine.state.epcs_in("P0002") == {EPC_SHIRT_A}


def test_exit_preserves_final_ownership_candidate_without_settlement() -> None:
    engine = _engine()
    engine.apply(_event(RetailEventType.PICK, A, "P0001", 1.0))
    result = engine.apply(_event(RetailEventType.EXIT_WITH_ITEM, A, "P0001", 9.0))
    assert result.cart_event_type == CartEventType.EXIT_HOLD
    cart = engine.state.carts["P0001"]
    assert cart.status == CartStatus.EXITED
    assert cart.lines[EPC_SHIRT_A].final_ownership_candidate is True
    assert cart.exited_at == at(9.0)
    assert not any(field in cart.model_dump() for field in ("total", "payment", "charge"))


def test_replaying_the_same_event_stream_twice_is_a_noop() -> None:
    events = [
        _event(RetailEventType.PICK, A, "P0001", 1.0),
        _event(RetailEventType.PICK, B, "P0001", 2.0),
        _event(RetailEventType.HANDOFF, B, "P0001", 3.0, counterpart="P0002"),
        _event(RetailEventType.PUTBACK, A, "P0001", 4.0),
    ]
    engine = _engine()
    for event in events:
        engine.apply(event)
    snapshot = engine.state.model_dump()
    duplicates = [engine.apply(event).cart_event_type for event in events]
    assert duplicates == [CartEventType.NOOP_DUPLICATE] * 4
    assert engine.state.model_dump() == snapshot
    assert engine.state.epcs_in("P0002") == {EPC_SHIRT_B}
    assert engine.state.epcs_in("P0001") == set()
