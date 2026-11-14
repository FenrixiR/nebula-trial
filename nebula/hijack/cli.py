import argparse
import os
import sys

from . import engine as e


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nebula.hijack")
    ap.add_argument("command", choices=["plan", "preflight", "apply", "verify", "release", "purge", "pin"])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--payload", default=".", help="directory the manifest's file sources are relative to")
    ap.add_argument("--host", help="printer address: LAN address (path A) or 10.78.0.100 (path B)")
    ap.add_argument("--password-env", default="NEBULA_PRINTER_PW", help="environment variable holding the printer's root password")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.command == "pin":
            e.pin_manifest(a.manifest, a.payload)
            return 0
        m = e.load_manifest(a.manifest)
        if a.command == "plan" or (a.command == "apply" and a.dry_run):
            for i, s in enumerate(e.plan(m, a.payload), 1):
                print(f"{i}. {s}")
            problems = e.check_pins(m, a.payload)
            for p in problems:
                print("NOT READY:", p)
            return 1 if problems else 0
        pw = os.environ.get(a.password_env)
        if not (a.host and pw is not None):
            ap.error(f"--host and the {a.password_env} environment variable are required")
        r = e.SshRunner(a.host, password=pw)
        if a.command == "preflight":
            checks = e.evaluate(m, e.facts(r))
            for c in checks:
                print(("ok   " if c.ok else "FAIL ") + c.name, c.detail)
            return 0 if all(c.ok for c in checks) else 1
        if a.command == "apply":
            e.apply(r, m, a.payload)
        elif a.command == "verify":
            rc, out = e.ctl(r, m, "verify")
            print(out.strip())
            return rc
        elif a.command == "purge":
            left = e.purge(r, m)
            print("still on the printer: " + ", ".join(left) if left else "nothing of Nebula is left on the printer")
            return 1 if left else 0
        else:
            print(e.release(r, m).strip())
        return 0
    except e.NebulaError as ex:
        print("nebula:", ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
