"""LAN discovery limits and the command line."""

import pytest
from essl_agent import __main__ as cli
from essl_agent import discovery
from essl_agent.store import Store

from .conftest import Terminal

INI = """[agent]
server_url = https://api.flarize.test
token = fl_abc_secret
queue_path = {queue}
"""


class TestDiscovery:
    def test_only_a_private_lan_of_reasonable_size(self):
        network, targets = discovery.targets_for("192.168.5.0/24", None)
        assert str(network) == "192.168.5.0/24" and len(targets) == 254
        assert len(discovery.targets_for(None, "10.1.2.3", prefix=28)[1]) == 14
        with pytest.raises(RuntimeError, match="not a private network"):
            discovery.targets_for("8.8.8.0/24", None)
        with pytest.raises(RuntimeError, match="Narrow it"):
            discovery.targets_for("10.0.0.0/16", None)
        with pytest.raises(RuntimeError, match="No local IPv4"):
            discovery.targets_for(None, None)

    def test_identify_reports_errors_instead_of_guessing(self, lan):
        lan.place("192.168.1.60", Terminal("NCD8252101212", mac="001761 12F2D1"))
        found = discovery.identify("192.168.1.60")
        assert found["serial_number"] == "NCD8252101212" and found["mac_address"] == "00:17:61:12:f2:d1" and found["platform"] == "ZAM180_TFT"
        assert "error" in discovery.identify("192.168.1.61")

    def test_scan_of_a_subnet(self, lan):
        lan.place("192.168.1.60", Terminal("NCD8252101212"))
        result = discovery.discover(subnet="192.168.1.0/24")
        assert (result["subnet"], result["hosts_scanned"], result["hosts_open"], result["gateway"], result["agent_ip"]) == ("192.168.1.0/24", 254, 1, "192.168.1.1", "192.168.1.5")

    def test_real_port_probe_of_a_closed_port(self):
        assert discovery._port_open("127.0.0.1", 9, 0.2) is None and discovery.network_for("192.168.1.77").prefixlen == 24


class TestCli:
    def test_status_and_test_device(self, tmp_path, lan, capsys):
        ini = tmp_path / "agent.ini"
        ini.write_text(INI.format(queue=tmp_path / "q.sqlite3"), encoding="utf-8")
        store = Store(tmp_path / "q.sqlite3")
        store.log("INFO", "started")
        store.close()
        assert cli.main(["--config", str(ini), "status"]) == 0
        assert '"queue_pending": 0' in capsys.readouterr().out
        lan.place("192.168.1.209", Terminal("NCD1"))
        assert cli.main(["--config", str(ini), "test-device", "--ip", "192.168.1.209"]) == 0
        assert "NCD1" in capsys.readouterr().out
        assert cli.main(["--config", str(ini), "test-device", "--ip", "192.168.1.250"]) == 1
        assert cli.main(["--config", str(ini), "test-device"]) == 2  # nothing configured

    def test_discover_without_reporting(self, tmp_path, lan, capsys):
        ini = tmp_path / "agent.ini"
        ini.write_text(INI.format(queue=tmp_path / "q.sqlite3"), encoding="utf-8")
        lan.place("192.168.1.60", Terminal("NCD8252101212"))
        assert cli.main(["--config", str(ini), "discover", "--subnet", "192.168.1.0/24", "--no-report"]) == 0
        assert "serial=NCD8252101212" in capsys.readouterr().out
        assert cli.main(["--config", str(ini), "discover", "--subnet", "8.8.8.0/24", "--no-report"]) == 2
