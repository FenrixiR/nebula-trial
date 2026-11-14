import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STUBS = {
    "ip": """#!/bin/sh
echo "ip $*" >> "$STUBDIR/calls"
case "$*" in
  "-4 addr show eth0"|"-4 addr show usb0") [ -f "$STUBDIR/addr" ] && echo "    inet 10.78.0.100/24 scope global eth0" ;;
  "link show eth0"|"link show usb0") if [ -f "$STUBDIR/up" ]; then echo "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500"; else echo "2: eth0: <BROADCAST,MULTICAST> mtu 1500"; fi ;;
  "link set eth0 up"|"link set usb0 up") touch "$STUBDIR/up" ;;
esac
exit 0
""",
    "ping": """#!/bin/sh
echo "ping $*" >> "$STUBDIR/calls"
[ -f "$STUBDIR/pingok" ]
""",
    "udhcpc": """#!/bin/sh
echo "udhcpc $*" >> "$STUBDIR/calls"
touch "$STUBDIR/addr"
exit 0
""",
    "wget": """#!/bin/sh
echo "wget $*" >> "$STUBDIR/calls"
[ -f "$STUBDIR/hostok" ]
""",
    "start-stop-daemon": "#!/bin/sh\nexit 0\n",
    "sleep": "#!/bin/sh\n/bin/sleep 0.02\n",
}


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.stubs = self.d + "/stubs"
        os.makedirs(self.stubs)
        for name, body in STUBS.items():
            with open(f"{self.stubs}/{name}", "w") as f:
                f.write(body)
            os.chmod(f"{self.stubs}/{name}", 0o755)
        self.root = self.d + "/root"
        os.makedirs(self.root)
        self.sysnet = self.d + "/sysnet"
        os.makedirs(self.sysnet + "/eth0")
        shutil.copy(os.path.join(REPO, "nebula/printer/nebula-ctl.sh"), self.root)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_ctl(self, command, env_file, flags=(), timeout=1.5, **env):
        with open(self.root + "/nebula.env", "w") as f:
            f.write(env_file)
        for name in flags:
            open(f"{self.d}/{name}", "w").close()
        full = dict(os.environ, PATH=self.stubs + ":" + os.environ["PATH"], STUBDIR=self.d, NEBULA_ROOT=self.root,
                    RUNDIR=self.d, INITDIR=self.d, SYSNET=self.sysnet, **env)
        try:
            subprocess.run(["sh", self.root + "/nebula-ctl.sh", command], env=full, timeout=timeout,
                           capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        return read(self.d + "/calls"), read(self.root + "/nebula.log")


class KeeperTest(Base):
    def keeper(self, flags):
        return self.run_ctl("keeper", "HOST_ADDR=10.78.0.1\n", flags, KEEPER_SILENT_LIMIT="3")[0]

    def test_healthy_cable_is_left_alone(self):
        calls = self.keeper(["addr", "up", "pingok"])
        self.assertNotIn("udhcpc", calls)
        self.assertNotIn("link set", calls)

    def test_link_down_with_a_stale_address_is_brought_up(self):
        calls = self.keeper(["addr", "pingok"])
        self.assertIn("ip link set eth0 up", calls)
        self.assertNotIn("udhcpc", calls)

    def test_silent_host_gets_a_fresh_lease(self):
        calls = self.keeper(["addr", "up"])
        self.assertIn("udhcpc -i eth0 -n -q", calls)
        self.assertIn("ip link set eth0 up", calls)

    def test_cosmos_26_10_names_the_cable_usb0(self):
        shutil.rmtree(self.sysnet + "/eth0")
        os.makedirs(self.sysnet + "/usb0")
        calls = self.keeper(["pingok"])
        self.assertIn("ip link set usb0 up", calls)
        self.assertIn("udhcpc -i usb0 -n -q", calls)
        self.assertNotIn("eth0", calls)

    def test_missing_address_is_leased(self):
        calls = self.keeper(["up", "pingok"])
        self.assertIn("udhcpc -i eth0 -n -q", calls)


class ReleaseTest(Base):

    def release(self):
        stubs = {"start-stop-daemon": '#!/bin/sh\necho "ssd $*" >> "$STUBDIR/calls"\nexit 0\n'}
        for name, body in stubs.items():
            with open(f"{self.stubs}/{name}", "w") as f:
                f.write(body)
        return self.run_ctl("release", "HOST_ADDR=10.78.0.1\nSTART_ORDER=\n", ["up"])[0]

    def test_release_stops_the_relay_and_watchdog_but_not_the_keeper(self):
        calls = self.release()
        self.assertIn("nebula-relay.pid", calls)
        self.assertIn("nebula-watchdog.pid", calls)
        self.assertNotIn("-K -q -p " + self.d + "/nebula-keeper.pid", calls)

    def test_release_makes_sure_the_keeper_is_running(self):
        calls = self.release()
        self.assertIn("-S -b -m -p " + self.d + "/nebula-keeper.pid", calls)


class PersistTest(Base):

    def setUp(self):
        super().setUp()
        self.etc = self.d + "/etc"
        os.makedirs(self.etc + "/init.d")
        shutil.copy(os.path.join(REPO, "nebula/printer/nebula-boot.sh"), self.root)
        with open(f"{self.stubs}/start-stop-daemon", "w") as f:
            f.write('#!/bin/sh\necho "ssd $*" >> "$STUBDIR/calls"\nexit 0\n')

    def ctl(self, *args):
        with open(self.root + "/nebula.env", "w") as f:
            f.write("HOST_ADDR=10.78.0.1\n")
        env = dict(os.environ, PATH=self.stubs + ":" + os.environ["PATH"], STUBDIR=self.d, NEBULA_ROOT=self.root,
                   RUNDIR=self.d, INITDIR=self.etc + "/init.d", ETCDIR=self.etc)
        return subprocess.run(["sh", self.root + "/nebula-ctl.sh", *args], env=env, capture_output=True, text=True).stdout

    def boot(self):
        env = dict(os.environ, PATH=self.stubs + ":" + os.environ["PATH"], STUBDIR=self.d, NEBULA_ROOT=self.root,
                   RUNDIR=self.d, INITDIR=self.etc + "/init.d", ETCDIR=self.etc)
        subprocess.run(["sh", self.root + "/nebula-boot.sh", "start"], env=env, capture_output=True)
        return read(self.d + "/calls")

    def test_off_by_default_and_the_boot_script_is_inert(self):
        self.assertIn("persist: off", self.ctl("persist", "status"))
        self.assertNotIn("keeper", self.boot())

    def test_on_links_the_init_script_and_starts_the_keeper_at_boot(self):
        self.ctl("persist", "on")
        self.assertIn("persist: on", self.ctl("persist", "status"))
        for r in ("rc3.d", "rc5.d"):
            self.assertTrue(os.path.islink(f"{self.etc}/{r}/S97nebula-cable"))
        self.assertTrue(os.path.islink(self.etc + "/init.d/nebula-cable"))
        self.assertIn("nebula-keeper.pid", self.boot())

    def test_off_removes_every_trace(self):
        self.ctl("persist", "on")
        self.ctl("persist", "off")
        self.assertIn("persist: off", self.ctl("persist", "status"))
        for p in ("init.d/nebula-cable", "rc3.d/S97nebula-cable", "rc5.d/S97nebula-cable"):
            self.assertFalse(os.path.lexists(f"{self.etc}/{p}"))
        self.assertFalse(os.path.exists(self.root + "/keep-cable"))
        self.assertNotIn("keeper", self.boot())

    def test_a_leftover_folder_without_the_marker_does_nothing(self):
        self.ctl("persist", "on")
        os.remove(self.root + "/keep-cable")
        self.assertNotIn("keeper", self.boot())


class PurgeTest(PersistTest):

    def ctl(self, *args):
        with open(self.root + "/nebula.env", "w") as f:
            f.write("HOST_ADDR=10.78.0.1\nSTART_ORDER=gui\n")
        with open(self.etc + "/init.d/gui", "w") as f:
            f.write('#!/bin/sh\necho "gui $1" >> "$STUBDIR/calls"\n')
        os.chmod(self.etc + "/init.d/gui", 0o755)
        env = dict(os.environ, PATH=self.stubs + ":" + os.environ["PATH"], STUBDIR=self.d, NEBULA_ROOT=self.root,
                   RUNDIR=self.d, INITDIR=self.etc + "/init.d", ETCDIR=self.etc)
        return subprocess.run(["sh", self.root + "/nebula-ctl.sh", *args], env=env, capture_output=True, text=True).stdout

    def test_purge_removes_the_folder_the_boot_hook_and_the_run_files(self):
        self.ctl("persist", "on")
        self.ctl("purge")
        self.assertFalse(os.path.exists(self.root))
        for p in ("init.d/nebula-cable", "rc3.d/S97nebula-cable", "rc5.d/S97nebula-cable"):
            self.assertFalse(os.path.lexists(f"{self.etc}/{p}"))
        self.assertFalse(os.path.exists(self.d + "/nebula-mode"))

    def test_purge_on_a_hijacked_printer_starts_the_stock_software_first(self):
        with open(self.d + "/nebula-mode", "w") as f:
            f.write("hijacked\n")
        self.ctl("purge")
        self.assertIn("gui start", read(self.d + "/calls"))
        self.assertFalse(os.path.exists(self.root))

    def test_purge_on_a_stock_printer_does_not_restart_the_stock_software(self):
        self.ctl("purge")
        self.assertNotIn("gui start", read(self.d + "/calls"))


class WatchdogTest(Base):

    def watchdog(self, flags):
        return self.run_ctl("watchdog", "HOST_ADDR=10.78.0.1\nWATCHDOG_MINUTES=1\n", flags, timeout=2)

    def test_a_host_that_answers_is_never_released(self):
        calls, log = self.watchdog(["hostok"])
        self.assertIn("http://10.78.0.1:7125/server/info", calls)
        self.assertNotIn("watchdog: host unreachable", log)

    def test_a_silent_host_is_released_after_the_interval(self):
        calls, log = self.watchdog([])
        self.assertIn("watchdog: host unreachable for 1 min, releasing", log)


if __name__ == "__main__":
    unittest.main()
