import json
import logging

from tradingdesk.logging_setup import JsonFormatter, log_event, redact


def test_redacts_tokens_and_secrets():
    text = redact("Authorization: Bearer abc.DEF-123 password=hunter2 token: xyz")
    assert "abc.DEF-123" not in text and "hunter2" not in text and "xyz" not in text
    assert text.count("[REDACTADO]") == 3


def test_masks_ibkr_account_ids():
    assert redact("cuenta DU1234567") == "cuenta DU****567"


def test_json_formatter_redacts_fields(caplog):
    logger = logging.getLogger("t")
    with caplog.at_level(logging.INFO, logger="t"):
        log_event(logger, "conectado a U9876543", token="secreto-largo")
    line = JsonFormatter().format(caplog.records[0])
    payload = json.loads(line)
    assert payload["msg"] == "conectado a U****543"
    assert "secreto-largo" not in line


def test_paper_account_ids_with_letter_suffix_are_masked():
    from tradingdesk.logging_setup import redact
    assert redact("cuenta DUR272106") == "cuenta DUR***106"
