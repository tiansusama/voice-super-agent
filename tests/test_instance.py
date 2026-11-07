import os
import subprocess
import sys
from threading import Event
import unittest

from voice_super_agent.desktop.instance import SingleInstance


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_notifies_before_watcher_starts(self):
        name = rf"Local\VoiceSuperAgent.Test.{os.getpid()}.Notify"
        primary = SingleInstance(name)
        self.assertTrue(primary.primary)
        notified = Event()
        try:
            child = subprocess.run(
                [sys.executable, "-c",
                 "import sys; from voice_super_agent.desktop.instance import SingleInstance; "
                 "instance = SingleInstance(sys.argv[1]); print(instance.primary); "
                 "instance.notify(); instance.close()", name],
                capture_output=True, text=True, check=True, timeout=5,
            )
            self.assertEqual(child.stdout.strip(), "False")
            primary.watch(notified.set)
            self.assertTrue(notified.wait(2))
        finally:
            primary.close()
        replacement = SingleInstance(name)
        try:
            self.assertTrue(replacement.primary)
        finally:
            replacement.close()

    def test_abrupt_process_exit_releases_instance(self):
        name = rf"Local\VoiceSuperAgent.Test.{os.getpid()}.Exit"
        subprocess.run(
            [sys.executable, "-c",
             "import os, sys; from voice_super_agent.desktop.instance import SingleInstance; "
             "instance = SingleInstance(sys.argv[1]); os._exit(0)", name],
            check=True, timeout=5,
        )
        instance = SingleInstance(name)
        try:
            self.assertTrue(instance.primary)
        finally:
            instance.close()


if __name__ == "__main__":
    unittest.main()
