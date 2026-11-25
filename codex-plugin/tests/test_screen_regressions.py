from pathlib import Path
from queue import Queue
import sys
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import native
from practice import Practice, changed
from window_reader import WindowReader


class ScreenCaptureTests(unittest.TestCase):
    def test_dead_capture_worker_is_retried_even_if_closing_its_pipe_fails(self):
        worker = Mock()
        worker.poll.return_value = 1
        worker.stdin.flush.side_effect = OSError("pipe closed")
        worker.stdin.close.side_effect = OSError("pipe closed")
        reader = WindowReader()
        reader.process = worker

        with self.assertRaisesRegex(native.WindowError, "自动重试"):
            reader.read(42, 2, 2)
        self.assertIsNone(reader.process)
        worker.wait.assert_called_once_with(timeout=.3)

        def restart():
            reader.process = Mock()
            reader.replies = Queue()
            reader.replies.put((12, b"\x01\x02\x03" * 4))

        with patch.object(reader, "start", side_effect=restart) as start:
            self.assertEqual(reader.read(42, 2, 2).getpixel((1, 1)), (1, 2, 3))
        start.assert_called_once_with()

    def test_practice_detects_a_color_change_with_equal_luminance(self):
        frames = [Image.new("RGB", (500, 320), color) for color in ((255, 0, 0), (0, 130, 0))]
        self.assertEqual(frames[0].convert("L").tobytes(), frames[1].convert("L").tobytes())
        frames.append(Image.new("RGB", (1920, 1080)))
        shutdown = Mock()
        shutdown.wait.side_effect = [False, False, False, True]
        shutdown.is_set.return_value = False
        running = Event()
        running.set()
        state = SimpleNamespace(shutdown=shutdown, running=running, ending=Event(),
                                capture_generation=1, last_poll=time.monotonic(),
                                scope=Mock(active=Mock(return_value=42)), sampled=Mock())

        with (patch("practice.WindowReader") as factory,
              patch("practice.native.window_state", side_effect=[(0, 0, *frame.size, 123) for frame in frames])):
            factory.return_value.__enter__.return_value.read.side_effect = frames
            Practice.watch(state)

        before, after, large = [call.args[1] for call in state.sampled.emit.call_args_list]
        self.assertIs(before[2], before[4])
        self.assertEqual(before[3].size, (144, 90))
        self.assertFalse(changed(before, before))
        self.assertTrue(changed(before, after))
        self.assertEqual(large[2].size, (1440, 810))
        self.assertEqual(large[4].size, (1920, 1080))


if __name__ == "__main__":
    unittest.main()
