import pytest

from tradingdesk.config import ConfigError, Settings


def test_defaults_are_paper_mock_localhost(tmp_path):
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path)})
    assert (s.mode, s.data_source, s.host) == ("paper", "mock", "127.0.0.1")


@pytest.mark.parametrize("env", [
    {"TRADINGDESK_MODE": "live"},
    {"TRADINGDESK_MODE": "real"},
    {"TRADINGDESK_DATA_SOURCE": "ibkr"},
    {"TRADINGDESK_HOST": "0.0.0.0"},
    {"TRADINGDESK_HOST": "192.168.1.10"},
    {"TRADINGDESK_PORT": "80"},
    {"TRADINGDESK_PORT": "abc"},
])
def test_unsafe_configuration_is_rejected(tmp_path, env):
    with pytest.raises(ConfigError):
        Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), **env})


def test_ipv6_loopback_allowed(tmp_path):
    assert Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), "TRADINGDESK_HOST": "::1"}).host == "::1"
