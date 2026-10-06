"""Typographic punctuation becomes ASCII; the rupee symbol is kept."""
import logging

from backend.shared.helpers.text_clean import to_plain
from backend.shared.helpers.error_alerts import clean_message
from backend.shared.helpers.ramboq_logger import _PlainTextFilter


def test_typographic_punctuation_becomes_ascii():
    assert to_plain("a — b → c · d ’e’ “f” …") == "a - b -> c | d 'e' \"f\" ..."


def test_rupee_symbol_is_kept():
    assert to_plain("P&L ₹1,250") == "P&L ₹1,250"


def test_alert_message_is_plain():
    assert clean_message("Order — filled · ok") == "Order - filled | ok"


def test_log_filter_rewrites_message_in_place():
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad — value %s", ("→",), None)
    assert _PlainTextFilter().filter(rec) is True
    assert rec.getMessage() == "bad - value ->"
