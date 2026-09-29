"""Office agent command line.

python -m essl_agent --config agent.ini run           continuous operation
python -m essl_agent --config agent.ini once          one read + deliver cycle
python -m essl_agent --config agent.ini discover      find this office's terminals on this LAN
python -m essl_agent --config agent.ini test-device   read-only terminal check (no platform needed)
python -m essl_agent --config agent.ini status        local queue state
"""

from __future__ import annotations

import argparse
import json
import sys

from essl_agent import VERSION
from essl_agent.config import AgentConfig, DeviceConfig
from essl_agent.runner import Agent
from essl_agent.store import Store
from essl_agent.zk_reader import DeviceError, read_attendance, read_info, read_users, tcp_reachable


def cmd_run(args) -> int:
    return Agent(AgentConfig.load(args.config)).run()


def cmd_once(args) -> int:
    agent = Agent(AgentConfig.load(args.config))
    agent.heartbeat()
    result = agent.run_once()
    print(json.dumps({**result, "queue_pending": agent.store.pending_count()}, indent=2))
    agent.store.close()
    return 0


def cmd_discover(args) -> int:
    agent = Agent(AgentConfig.load(args.config))
    try:
        outcome = agent.discover(subnet=args.subnet, prefix=args.prefix, port=args.port, password=args.password, report=not args.no_report)
    except RuntimeError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        agent.store.close()
        return 2
    scan, server = outcome["scan"], outcome.get("server")
    print(f"\nsubnet {scan.get('subnet')} (this machine {scan.get('agent_ip')}, gateway {scan.get('gateway')}): {scan.get('hosts_scanned')} scanned, {scan.get('hosts_open')} answering on {args.port}")
    for host in scan["found"]:
        if host.get("error"):
            print(f"  {host['ip_address']:16} unidentified: {host['error']}")
        else:
            print(f"  {host['ip_address']:16} serial={host.get('serial_number')} mac={host.get('mac_address')} model={host.get('device_name')} firmware={host.get('firmware_version')}")
    if server:
        for match in server.get("matches", []):
            print(f"  {'MATCHED' if match.get('matched') else 'PROBLEM'}: {match.get('reported_serial')} -> {match.get('ip_address')}: {match.get('message')}")
        for host in server.get("unmatched", []):
            print(f"  UNKNOWN: {host.get('ip_address')} ({host.get('serial_number') or host.get('error')}) is not registered to this agent")
        for serial in server.get("still_awaiting", []):
            print(f"  NOT FOUND: {serial} is registered but did not answer on this LAN")
    agent.store.close()
    return 0


def cmd_test_device(args) -> int:
    """Read-only terminal check; needs neither the platform nor a credential when --ip is given."""
    devices = [DeviceConfig(name="cli", ip=args.ip, port=args.port)] if args.ip else AgentConfig.load(args.config).devices
    if not devices:
        print("no devices configured", file=sys.stderr)
        return 2
    failures = 0
    for dev in devices:
        print(f"\n=== {dev.name} {dev.ip}:{dev.port} ===\n  tcp reachable : {tcp_reachable(dev.ip, dev.port)}")
        try:
            info = read_info(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
            for key in ("serial_number", "mac_address", "device_name", "firmware_version", "platform", "device_time"):
                print(f"  {key:16}: {info.get(key)}")
            users = read_users(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
            records = read_attendance(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
            print(f"  users           : {len(users)}\n  attendance      : {len(records)}")
        except DeviceError as exc:
            print(f"  FAILED: {exc}")
            failures += 1
    return 1 if failures else 0


def cmd_status(args) -> int:
    store = Store(AgentConfig.load(args.config).queue_path)
    print(json.dumps(store.stats(), indent=2))
    print("\nrecent events:")
    for event in store.recent_events(15):
        print(f"  {event['at'][:19]}  {event['level']:5} {event['message']}")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="essl_agent", description=f"Flarize office agent {VERSION}")
    parser.add_argument("--config", default="agent.ini")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run continuously").set_defaults(func=cmd_run)
    sub.add_parser("once", help="one read + deliver cycle").set_defaults(func=cmd_once)
    sub.add_parser("status", help="show the local queue").set_defaults(func=cmd_status)
    discover = sub.add_parser("discover", help="find this office's terminals on this LAN")
    discover.add_argument("--subnet", help="e.g. 192.168.5.0/24; defaults to this machine's own network")
    discover.add_argument("--prefix", type=int, default=24)
    discover.add_argument("--port", type=int, default=4370)
    discover.add_argument("--password", type=int, default=0)
    discover.add_argument("--no-report", action="store_true", help="scan and print without telling the platform")
    discover.set_defaults(func=cmd_discover)
    test = sub.add_parser("test-device", help="read-only terminal check")
    test.add_argument("--ip")
    test.add_argument("--port", type=int, default=4370)
    test.set_defaults(func=cmd_test_device)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
