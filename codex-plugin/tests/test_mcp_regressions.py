import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge
from protocol import MODERN_VERSION, VERSION_KEY


class BridgeTests(unittest.TestCase):
    def test_stdio_handles_both_protocols_and_rejects_invalid_calls_without_desktop(self):
        modern = {"_meta": {VERSION_KEY: MODERN_VERSION}}
        methods = [
            ("initialize", {"protocolVersion": "2024-11-05"}),
            ("server/discover", modern),
            ("tools/list", modern),
            ("tools/call", {"name": ["list_windows"]}),
            ("tools/call", {"name": "missing_tool"}),
            ("tools/call", {"name": "start_live", "arguments": []}),
            ("tools/call", {"name": "start_live", **modern}),
            ("tools/call", {"name": "wait_for_practice_change", "arguments": {"practice_id": "id", "timeout_seconds": 0}}),
            (["ping"], {}),
            ("ping", {}),
        ]
        requests = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(methods)
        ]
        # Any dispatched call in this subprocess fails before it can touch the profile.
        process = subprocess.run(
            [sys.executable, "-B", "-c",
             "from unittest.mock import Mock; import bridge; "
             "bridge.connect = Mock(side_effect=AssertionError('desktop contact')); "
             "raise SystemExit(bridge.main())"],
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
        )
        replies = {item["id"]: item for item in map(json.loads, process.stdout.splitlines())}
        self.assertEqual(set(replies), set(range(len(requests))))
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn(MODERN_VERSION, replies[1]["result"]["supportedVersions"])
        self.assertEqual(replies[2]["result"]["resultType"], "complete")
        self.assertIn("list_windows", {tool["name"] for tool in replies[2]["result"]["tools"]})
        for index in (3, 4, 5):
            self.assertEqual(replies[index]["error"]["code"], -32602)
        for index in (6, 7):
            self.assertTrue(replies[index]["result"]["isError"])
        self.assertEqual(replies[6]["result"]["resultType"], "complete")
        self.assertEqual(replies[8]["error"]["code"], -32600)
        self.assertEqual(replies[9]["result"], {})

    def test_cancelled_call_skips_desktop_connect(self):
        cancelled = Event()
        cancelled.set()
        request = {"id": 1, "params": {"name": "list_windows"}}
        with patch.object(bridge, "ensure_desktop") as ensure_desktop:
            self.assertIsNone(bridge.handle_call(request, "owner", cancelled))
            ensure_desktop.assert_not_called()

    def test_cancellation_during_connect_skips_desktop_start(self):
        cancelled = Event()
        with patch.object(bridge, "connect", side_effect=cancelled.set), patch.object(bridge.subprocess, "Popen") as spawn:
            self.assertIsNone(bridge.ensure_desktop(cancelled=cancelled))
            spawn.assert_not_called()

    def test_cancelled_socket_request_sends_nothing_and_disconnects(self):
        cancelled = Event()
        cancelled.set()
        socket = Mock()
        self.assertIsNone(bridge.roundtrip(socket, {"request": {"id": 1}}, cancelled=cancelled))
        socket.write.assert_not_called()
        socket.abort.assert_called_once()


if __name__ == "__main__":
    unittest.main()
