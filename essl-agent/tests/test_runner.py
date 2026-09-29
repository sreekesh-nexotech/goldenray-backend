"""The agent's promises: identity before anything, nothing lost, nothing sent twice, no needless requests."""

from datetime import datetime, timedelta

from essl_agent.store import Store
from essl_agent.uploader import AuthRejected, Refused, ServerUnavailable
from essl_agent.zk_reader import READ_CALLS

from .conftest import Terminal, User, mars

SERIAL = "NCD8253601138"


def terminal_at(lan, serial=SERIAL, ip="192.168.1.209", punches=3):
    terminal = lan.place(ip, Terminal(serial))
    terminal.users = [User(1, "1", "Asha", privilege=14), User(2, "2", "Binu", password="1234")]
    for uid in range(1, punches + 1):
        terminal.punch(uid, str(uid % 2 + 1), datetime(2026, 9, 22, 9, 0) + timedelta(minutes=uid))
    return terminal


class TestIdentity:
    def test_the_right_terminal_is_read_and_delivered(self, make_agent, lan, platform):
        terminal = terminal_at(lan)
        agent = make_agent(mars())
        result = agent.run_once()
        assert result["batches"] == 1 and result["uploaded"] == 3 and result["announced"] == 1
        assert platform.names() == ["announce", "users", "attendance"]
        announce = platform.calls[0][1]
        assert announce["serial_number"] == SERIAL and announce["mac_address"] == "00:17:61:aa:aa:01" and announce["device_time"] == "2026-09-22T09:00:00"
        records = platform.calls[2][1]["records"]
        assert records[0] == {"device_record_uid": 1, "pin": "2", "device_time": "2026-09-22T09:01:00", "status": 15, "punch": 0, "raw_payload": records[0]["raw_payload"]}
        assert platform.calls[2][1]["device"] == platform.devices[SERIAL]
        assert set(terminal.calls) <= READ_CALLS  # read-only: nothing else was ever called on the terminal

    def test_another_serial_is_blocked_reported_and_the_block_survives_a_restart(self, make_agent, lan, platform, tmp_path):
        terminal_at(lan)
        agent = make_agent(mars())
        agent.run_once()
        platform.calls.clear()
        lan.place("192.168.1.209", Terminal("NCD0000000999"))  # someone swapped the hardware at this address
        agent.store.enqueue_many(SERIAL, [{"device_record_uid": 99, "pin": "7", "device_time": "2026-09-22T10:00:00"}])
        assert agent.read_device(agent.cfg.devices[0]) is False
        assert platform.names() == ["identity_mismatch"]
        report = platform.calls[0][1]
        assert (report["expected_serial"], report["reported_serial"], report["ip_address"]) == (SERIAL, "NCD0000000999", "192.168.1.209")
        assert agent.deliver()["batches"] == 0 and agent.store.pending_count(SERIAL) == 1  # held, not sent
        assert agent.device_reports()[0]["reachable"] is False and "IDENTITY_MISMATCH" in agent.device_reports()[0]["last_error"]

        agent.store.close()
        restarted = make_agent(mars(), store=Store(tmp_path / "queue.sqlite3"))
        assert restarted.cfg.devices[0].identity_blocked is True
        assert restarted.deliver()["batches"] == 0 and restarted.store.pending_count(SERIAL) == 1

        lan.place("192.168.1.209", terminal_at(lan))  # the right terminal is back
        restarted.run_once()
        assert restarted.cfg.devices[0].identity_blocked is False and restarted.store.pending_count(SERIAL) == 0

    def test_a_mac_objection_blocks_too(self, make_agent, lan, platform):
        terminal_at(lan)
        agent = make_agent(mars(expected_mac="00:17:61:bb:bb:02"))
        assert agent.read_device(agent.cfg.devices[0]) is False and "expected MAC" in agent.cfg.devices[0].identity_error

    def test_an_unpinned_terminal_is_trusted_on_first_contact_only(self, make_agent, lan):
        terminal_at(lan)
        agent = make_agent(mars(expected_serial=None))
        assert agent.read_device(agent.cfg.devices[0]) is True
        lan.place("192.168.1.209", Terminal("NCD0000000999"))
        assert agent.read_device(agent.cfg.devices[0]) is False and agent.cfg.devices[0].identity_blocked

    def test_no_serial_is_a_fault(self, make_agent, lan):
        lan.place("192.168.1.209", Terminal(""))
        agent = make_agent(mars())
        assert agent.read_device(agent.cfg.devices[0]) is False and "no serial" in agent.cfg.devices[0].identity_error


class TestQueueAndCursor:
    def test_rereading_enqueues_nothing_twice_even_after_purge(self, make_agent, lan, platform):
        terminal = terminal_at(lan)
        agent = make_agent(mars())
        agent.run_once()
        agent.store.purge_sent(0, now=datetime.now().astimezone() + timedelta(days=1))  # delivered rows are gone
        platform.calls.clear()
        agent.read_device(agent.cfg.devices[0])
        assert agent.store.pending_count() == 0
        terminal.punch(4, "1", datetime(2026, 9, 22, 11, 0))
        agent.read_device(agent.cfg.devices[0])
        assert [row["device_record_uid"] for row in agent.store.pending()] == [4]

    def test_restarted_numbering_falls_back_to_time(self, make_agent, lan):
        terminal = terminal_at(lan, punches=5)
        agent = make_agent(mars(), cursor_overlap_hours=1)
        agent.read_device(agent.cfg.devices[0])
        agent.store.mark_sent([row["id"] for row in agent.store.pending()])
        terminal.logs = []  # the log was cleared on the terminal: numbering starts again
        terminal.punch(1, "1", datetime(2026, 9, 22, 12, 0))
        terminal.punch(2, "1", datetime(2026, 9, 20, 8, 0))  # older than the overlap: already delivered long ago
        agent.read_device(agent.cfg.devices[0])
        assert [row["device_time"] for row in agent.store.pending()] == ["2026-09-22T12:00:00"]

    def test_outage_keeps_everything_and_backs_off(self, make_agent, lan, platform, clock):
        terminal_at(lan)
        agent = make_agent(mars(), retry_base_seconds=15)
        agent.read_device(agent.cfg.devices[0])
        platform.failure = ServerUnavailable("HTTP 503")
        assert agent.deliver()["batches"] == 0 and agent.store.pending_count() == 3 and agent.last_error.startswith("server unavailable")
        platform.failure = None
        calls = len(platform.calls)
        agent.deliver()
        assert len(platform.calls) == calls  # still inside the backoff window: no request
        clock.advance(16)
        assert agent.deliver()["uploaded"] == 3 and agent.store.pending_count() == 0 and agent.last_error is None

    def test_a_revoked_token_holds_the_queue(self, make_agent, lan, platform):
        terminal_at(lan)
        agent = make_agent(mars())
        agent.read_device(agent.cfg.devices[0])
        platform.failure = AuthRejected("HTTP 401")
        agent.deliver()
        assert agent.store.pending_count() == 3 and "credential refused" in agent.last_error

    def test_batches_are_at_most_200_with_stable_idempotency_keys(self, make_agent, lan, platform, clock):
        terminal_at(lan, punches=450)
        agent = make_agent(mars())
        agent.read_device(agent.cfg.devices[0])
        platform.failure = None
        original = platform.upload_attendance
        seen = []

        def flaky(device, serial, records, *, batch_id, idempotency_key):
            seen.append(idempotency_key)
            if len(seen) == 2:
                raise ServerUnavailable("connection reset")
            return original(device, serial, records, batch_id=batch_id, idempotency_key=idempotency_key)

        platform.upload_attendance = flaky
        agent.deliver()
        clock.advance(100)
        agent.deliver()
        sizes = [len(payload["records"]) for name, payload in platform.calls if name == "attendance"]
        assert sizes == [200, 200, 50] and seen[1] == seen[2] and seen[1].startswith("att-")  # the retried batch reuses its key
        assert agent.store.pending_count() == 0 and len(platform.stored) == 450


class TestFewerRequests:
    def test_nothing_to_send_means_no_request(self, make_agent, lan, platform, clock):
        terminal_at(lan)
        agent = make_agent(mars())
        agent.run_once()
        platform.calls.clear()
        agent.run_once()  # same table, no new punch, announce not due
        assert platform.calls == []
        clock.advance(3600)
        agent.run_once()
        assert platform.names() == ["announce"]  # hourly

    def test_users_only_when_changed_requested_or_stale(self, make_agent, lan, platform, clock):
        terminal = terminal_at(lan)
        agent = make_agent(mars())
        agent.run_once()
        platform.calls.clear()
        terminal.users.append(User(3, "EMP004", "Chitra"))
        agent.run_once()
        assert platform.names() == ["users"] and {user["pin"] for user in platform.calls[0][1]["users"]} == {"1", "2", "EMP004"}
        platform.calls.clear()
        uid = platform.devices[SERIAL]
        platform.config["devices"] = [
            {"uid": uid, "name": "MARS-01", "expected_serial": SERIAL, "ip_address": "192.168.1.209", "port": 4370, "users_read_requested_at": datetime.now().astimezone().isoformat()}
        ]
        agent.heartbeat()
        platform.calls.clear()
        agent.run_once()
        assert platform.names() == ["users"]  # the platform asked for a read
        platform.calls.clear()
        agent.run_once()
        assert platform.calls == []

    def test_nothing_is_announced_for_a_terminal_this_process_has_not_reached(self, make_agent, lan, platform, tmp_path):
        """An announce is contact (the platform moves last_seen_at and measures the clock from it): after a restart with
        a backlog and the terminal down, the stored identity of an earlier run must not be announced as seen now."""
        terminal = terminal_at(lan)
        first = make_agent(mars())
        first.read_device(first.cfg.devices[0])  # read, then the process stops before delivering
        first.store.close()
        terminal.online = False
        restarted = make_agent(mars(), store=Store(tmp_path / "queue.sqlite3"))
        result = restarted.run_once()
        assert "announce" not in platform.names() and result["announced"] == 0
        assert result["uploaded"] == 3 and restarted.store.pending_count(SERIAL) == 0  # the backlog was read earlier: it is delivered
        terminal.online = True
        restarted.run_once()
        announce = [payload for name, payload in platform.calls if name == "announce"]
        assert len(announce) == 1 and announce[0]["observed_at"] is not None

    def test_announce_refusal_holds_that_terminal_only(self, make_agent, lan, platform, clock):
        terminal_at(lan)
        terminal_at(lan, serial="NCD8252101212", ip="192.168.1.60")
        platform.refuse[SERIAL] = Refused(409, "device_bound_elsewhere", "bound to OFFICE-002-AGENT")
        agent = make_agent(mars(), mars(name="SALES-01", ip="192.168.1.60", expected_serial="NCD8252101212"))
        result = agent.run_once()
        assert result["batches"] == 1 and agent.store.pending_count(SERIAL) == 3 and agent.store.pending_count("NCD8252101212") == 0
        platform.calls.clear()
        agent.deliver()
        assert "announce" not in platform.names()  # not retried before the next announce interval
        del platform.refuse[SERIAL]
        clock.advance(3601)
        agent.deliver()
        assert agent.store.pending_count(SERIAL) == 0

    def test_an_upload_refusal_holds_that_terminal_until_the_next_announce(self, make_agent, lan, platform, clock):
        """A refused upload (``device_inactive``: deactivated centrally) used to be re-sent on every 2-second delivery loop."""
        terminal_at(lan)
        agent = make_agent(mars())
        platform.refuse_uploads[SERIAL] = Refused(409, "device_inactive", "deactivated on the platform")
        agent.run_once()
        assert agent.store.pending_count(SERIAL) == 3
        platform.calls.clear()
        agent.deliver()
        agent.deliver()
        assert platform.calls == []  # held, not re-sent every loop
        del platform.refuse_uploads[SERIAL]
        clock.advance(3601)
        agent.deliver()
        assert platform.names()[0] == "announce" and agent.store.pending_count(SERIAL) == 0

    def test_a_device_the_platform_lost_is_not_asked_for_every_loop_while_its_terminal_is_down(self, make_agent, lan, platform, clock, tmp_path):
        """``device_not_found`` (rehomed or deleted centrally) re-announces; with the terminal down there is nothing to
        announce, and the backlog of an earlier run must not be re-sent every 2 seconds meanwhile."""
        terminal = terminal_at(lan)
        first = make_agent(mars())
        first.read_device(first.cfg.devices[0])
        first.store.close()
        terminal.online = False
        restarted = make_agent(mars(), store=Store(tmp_path / "queue.sqlite3"))
        platform.refuse_uploads[SERIAL] = Refused(404, "device_not_found", "No device is bound to this agent with that identity.")
        restarted.run_once()
        platform.calls.clear()
        restarted.run_once()
        restarted.run_once()
        assert platform.calls == [] and restarted.store.pending_count(SERIAL) == 3
        del platform.refuse_uploads[SERIAL]
        terminal.online = True
        clock.advance(3601)
        restarted.run_once()
        assert "announce" in platform.names() and restarted.store.pending_count(SERIAL) == 0


class TestHeartbeat:
    def test_reachability_is_the_truth(self, make_agent, lan, platform, clock):
        terminal = terminal_at(lan)
        agent = make_agent(mars())
        agent.heartbeat()
        assert platform.calls[-1][1]["devices"][0]["reachable"] is False  # never verified yet
        agent.run_once()
        agent.heartbeat()
        report = platform.calls[-1][1]["devices"][0]
        assert report["reachable"] is True and report["device"] == platform.devices[SERIAL] and report["device_time"] == "2026-09-22T09:00:00"
        terminal.online = False
        agent.heartbeat()
        assert platform.calls[-1][1]["devices"][0]["reachable"] is False
        terminal.online = True
        clock.advance(2 * 300 + 61)  # verified too long ago
        agent.heartbeat()
        assert platform.calls[-1][1]["devices"][0]["reachable"] is False

    def test_central_configuration_is_adopted_but_the_local_pin_wins(self, make_agent, lan, platform):
        agent = make_agent(mars(expected_mac=None), mars(name="LOBBY", ip="192.168.1.70", expected_serial=None))
        platform.config["intervals"] = {"heartbeat_seconds": 30, "sync_seconds": 120}
        platform.config["upload_batch_size"] = 500
        platform.config["devices"] = [
            {"uid": "u-mars", "name": "MARS-01", "expected_serial": SERIAL, "expected_mac": "00:17:61:12:9c:49", "ip_address": "192.168.1.209", "port": 4370},
            {"uid": "u-lobby", "name": "LOBBY", "expected_serial": "NCD7", "ip_address": "192.168.1.70", "port": 4370},
            {"uid": "u-other", "name": "MARS-OTHER", "expected_serial": "NCD-OTHER", "ip_address": "192.168.1.209", "port": 4370},
            {"uid": "u-sales", "name": "SALES-01", "expected_serial": "NCD8252101212", "ip_address": "192.168.1.60", "port": 4370, "protocol": "ZK_UDP"},
            {"uid": "u-label", "name": "PROJECT-01", "expected_serial": "NCD8252101398", "ip_address": None, "port": 4370},
        ]
        agent.heartbeat()
        devices = {dev.name: dev for dev in agent.cfg.devices}
        assert set(devices) == {"MARS-01", "LOBBY", "MARS-OTHER", "SALES-01"}  # the label-registered one waits for discover
        assert (devices["MARS-01"].expected_serial, devices["MARS-01"].expected_mac, devices["MARS-01"].uid) == (SERIAL, "00:17:61:12:9c:49", "u-mars")  # gaps filled
        assert devices["LOBBY"].expected_serial == "NCD7"  # an unpinned local terminal takes the central pin
        assert devices["MARS-OTHER"].expected_serial == "NCD-OTHER"  # two pins at one address are two devices: the identity gate decides
        assert devices["SALES-01"].force_udp and devices["SALES-01"].from_server and devices["SALES-01"].uid == "u-sales"
        assert (agent.cfg.heartbeat_interval, agent.cfg.sync_interval, agent.cfg.upload_batch_size) == (30, 120, 200)
        agent.heartbeat()
        assert len(agent.cfg.devices) == 4  # adopting again adds nothing

    def test_heartbeat_failures_are_logged_not_raised(self, make_agent, platform):
        agent = make_agent(mars())
        for failure in (AuthRejected("401"), ServerUnavailable("down"), Refused(400, "validation_error", "bad")):
            platform.failure = failure
            assert agent.heartbeat() is None


def test_run_loop_reads_delivers_and_stops(make_agent, lan, platform):
    terminal_at(lan)
    agent = make_agent(mars())
    assert agent.run(sleep=lambda seconds: None, cycles=2) == 0
    assert platform.names()[:4] == ["heartbeat", "announce", "users", "attendance"]
    assert len(platform.stored) == 3


def test_discover_reports_the_evidence(make_agent, lan, platform):
    terminal_at(lan, ip="192.168.1.60")
    agent = make_agent()
    outcome = agent.discover(hosts=["192.168.1.60", "192.168.1.61"])
    assert outcome["scan"]["hosts_open"] == 1 and outcome["scan"]["found"][0]["serial_number"] == SERIAL
    assert platform.names() == ["discovery"] and outcome["server"]["unmatched"][0]["ip_address"] == "192.168.1.60"
    platform.failure = ServerUnavailable("down")
    assert agent.discover(hosts=["192.168.1.60"])["server"] is None
    assert agent.discover(hosts=["192.168.1.60"], report=False)["server"] is None
