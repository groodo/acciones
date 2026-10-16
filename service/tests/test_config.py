import pytest

from tradingdesk.config import ConfigError, Settings


def test_defaults_are_paper_mock_localhost(tmp_path):
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path)})
    assert (s.mode, s.data_source, s.host) == ("paper", "mock", "127.0.0.1")


@pytest.mark.parametrize("env", [
    {"TRADINGDESK_MODE": "live"},
    {"TRADINGDESK_MODE": "real"},
    {"TRADINGDESK_DATA_SOURCE": "yahoo"},
    {"TRADINGDESK_DATA_SOURCE": "ibkr", "TRADINGDESK_IB_PORT": "4001"},
    {"TRADINGDESK_DATA_SOURCE": "ibkr", "TRADINGDESK_IB_PORT": "7496"},
    {"TRADINGDESK_DATA_SOURCE": "ibkr", "TRADINGDESK_IB_HOST": "10.0.0.5"},
    {"TRADINGDESK_DATA_SOURCE": "ibkr", "TRADINGDESK_MODE": "live"},
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


def test_ibkr_paper_defaults_to_gateway_paper_port(tmp_path):
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), "TRADINGDESK_DATA_SOURCE": "ibkr"})
    assert (s.data_source, s.mode, s.ib_host, s.ib_port) == ("ibkr", "paper", "127.0.0.1", 4002)


def test_real_account_is_read_only_and_uses_live_gateway_port(tmp_path):
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), "TRADINGDESK_DATA_SOURCE": "ibkr",
                           "TRADINGDESK_IB_ACCOUNT": "real-readonly", "TRADINGDESK_PAPER_ORDERS": "1"})
    assert s.real_account and s.paper_orders is False and s.ib_port == 4001 and s.mode == "paper"


def test_real_account_can_be_chosen_with_a_file_in_the_data_dir(tmp_path):
    (tmp_path / "ib-account").write_text("real-readonly\n")
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), "TRADINGDESK_DATA_SOURCE": "ibkr",
                           "TRADINGDESK_IB_PORT": "7496"})
    assert s.real_account and s.ib_port == 7496 and not s.paper_orders
    # Sin fuente IBKR el archivo no tiene efecto.
    assert not Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path)}).real_account


@pytest.mark.parametrize("kwargs", [
    {"ib_account": "real"},
    {"ib_account": "real-readonly", "paper_orders": True},
])
def test_real_account_never_with_orders(tmp_path, kwargs):
    with pytest.raises(ConfigError):
        Settings(data_dir=tmp_path, data_source="ibkr", **kwargs).validate()
