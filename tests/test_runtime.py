import unittest
from threading import enumerate as enumerate_threads

from voice_super_agent.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime(desktop_check=True)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def test_snapshot_then_ordered_updates_and_reconnect(self):
        initial = self.runtime.connect("main")
        self.runtime.update_windows(main_window="hidden")
        self.runtime.update_windows(main_window="visible")
        first = self.runtime.events.get(timeout=2)
        second = self.runtime.events.get(timeout=2)
        self.assertLess(initial["sequence"], first["snapshot"]["sequence"])
        self.assertLess(first["snapshot"]["sequence"], second["snapshot"]["sequence"])
        self.assertEqual(first["snapshot"]["state"]["main_window"], "hidden")
        self.assertEqual(second["snapshot"]["state"]["main_window"], "visible")
        self.runtime.disconnect("main")
        restored = self.runtime.connect("main")
        self.assertEqual(restored, second["snapshot"])
        self.runtime.update_windows(floating_visible=True)
        self.assertEqual(self.runtime.events.get(timeout=2)["views"], ("main",))

    def test_activity_allows_commands_and_stops_cleanly(self):
        self.runtime.connect("main")
        self.runtime.command("start_activity")
        while True:
            event = self.runtime.events.get(timeout=2)
            if event["snapshot"]["state"]["activity_tick"] > 0:
                break
        self.runtime.command("show_main")
        while self.runtime.events.get(timeout=2)["kind"] != "command":
            pass
        self.runtime.command("stop_activity")
        stopped = self.runtime.connect("main")
        self.assertFalse(stopped["state"]["activity_running"])
        self.assertGreater(stopped["state"]["activity_tick"], 0)
        with self.assertRaisesRegex(ValueError, "未知桌面命令"):
            self.runtime.command("unknown")

    def test_disconnect_keeps_background_state(self):
        self.runtime.connect("main")
        self.runtime.disconnect("main")
        self.runtime.update_windows(main_window="hidden")
        event = self.runtime.events.get(timeout=2)
        self.assertEqual(event["views"], ())
        self.assertEqual(self.runtime.connect("main")["state"]["main_window"], "hidden")


class ShutdownTests(unittest.TestCase):
    def test_shutdown_cancels_running_activity_and_joins_thread(self):
        runtime = Runtime(desktop_check=True)
        runtime.start()
        runtime.command("start_activity")
        runtime.stop()
        self.assertFalse(any(thread.name == "agent-runtime" for thread in enumerate_threads()))


if __name__ == "__main__":
    unittest.main()
