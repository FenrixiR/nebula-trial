import argparse
import json
import os
import sys

from . import host_stack
from .hijack import engine

HERE = os.path.dirname(os.path.abspath(__file__))
UNREACHABLE = 3


def run(state_dir, manifests, payload, runner_factory, stack, log=print):
    try:
        with open(os.path.join(state_dir, "settings.json")) as f:
            address = json.load(f).get("address", "")
    except (OSError, ValueError):
        address = ""
    try:
        with open(os.path.join(state_dir, "printer-password")) as f:
            password = f.read().strip()
    except OSError:
        password = ""
    if not address or not password:
        log("no printer was set up, so there is nothing to remove from one")
        if stack.any_running():
            stack.stop(log)
        return 0
    runner = runner_factory(address, password)
    try:
        return _clean_printer(runner, address, manifests, payload, stack, log)
    finally:
        getattr(runner, "close", lambda: None)()


def _clean_printer(runner, address, manifests, payload, stack, log):
    try:
        facts = engine.facts(runner)
    except (engine.NebulaError, OSError) as ex:
        log(f"the printer at {address} cannot be reached ({ex}); it will go back to Cosmos by itself, and the files left on it do nothing")
        if stack.any_running():
            stack.stop(log)
        return UNREACHABLE
    m = engine.pick(manifests, facts["issue"])
    engine.refresh_controller(runner, m, payload)
    if facts.get("mode") == "hijacked" or stack.any_running():
        stack.stop(log)
    try:
        left = engine.purge(runner, m)
    except engine.NebulaError:
        try:
            engine.release(runner, m)
        except engine.NebulaError:
            pass
        raise
    if left:
        raise engine.NebulaError("still on the printer: " + ", ".join(left))
    log("the printer runs Cosmos again and nothing of Nebula is left on it")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nebula.uninstall")
    ap.add_argument("--state", default=os.path.expanduser("~/.nebula"))
    a = ap.parse_args(argv)
    manifests = engine.load_manifests(os.path.join(HERE, "manifests"))
    try:
        return run(a.state, manifests, HERE, lambda addr, pw: engine.SshRunner(addr, password=pw), host_stack.HostStack())
    except engine.NebulaError as ex:
        print("nebula:", ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
