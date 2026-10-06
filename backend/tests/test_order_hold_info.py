"""Order list exposes hold state: hold_json is carried on AlgoOrderInfo and defaults to None."""
import msgspec

from backend.api.routes.orders_helpers import AlgoOrderInfo


def test_hold_json_defaults_to_none_for_unheld_orders():
    assert "hold_json" in AlgoOrderInfo.__struct_fields__
    fields = {f: None for f in AlgoOrderInfo.__struct_fields__}
    fields["id"] = 1
    assert AlgoOrderInfo(**fields).hold_json is None


def test_hold_json_round_trips_through_the_list_payload():
    raw = '{"category": "template_exit", "reason": "exit", "price_policy": "CHASE_MED"}'
    fields = {f: None for f in AlgoOrderInfo.__struct_fields__}
    fields.update({"id": 1, "hold_json": raw})
    encoded = msgspec.json.encode(AlgoOrderInfo(**fields))
    decoded = msgspec.json.decode(encoded)
    assert decoded["hold_json"] == raw
