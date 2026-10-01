import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import sys
import subprocess
import shutil
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("call_all_under_test", ROOT / "call_all.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "launches.json"

    def coordinator(self, start, nudge=None):
        return launcher.SessionLauncher(self.state, start=start, nudge=nudge or Mock(return_value=(True, None)))

    def test_real_session_ids_are_required(self):
        response = Mock()
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(launcher.urllib.request, "urlopen", return_value=response):
            success, session_id, error = launcher.start_session("cog_test", "Devin-Builder")
        self.assertFalse(success)
        self.assertIsNone(session_id)
        self.assertIn("unconfirmed", error.lower())

    def test_success_persists_and_nudges_after_restart(self):
        start = Mock(return_value=(True, "session-123", None))
        nudge = Mock(return_value=(True, None))
        result = self.coordinator(start, nudge).launch(["Devin-Builder", "Devin-Builder"], "cog_test")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["sessions"], {"Devin-Builder": "session-123"})
        repeated = self.coordinator(start, nudge).launch(["Devin-Builder"], "cog_test")
        self.assertEqual(repeated["count"], 0)
        self.assertEqual(repeated["nudged"], ["Devin-Builder"])
        self.assertEqual(repeated["already_requested"], [])
        nudge.assert_called_once_with("cog_test", "Devin-Builder", "session-123")
        self.assertEqual(start.call_count, 1)
        self.assertNotIn("cog_test", self.state.read_text())

    def test_nudge_failure_is_reported_not_recreated(self):
        state = {"platform": launcher.PLATFORM,
                 "launches": {"Devin-Builder": {"status": "created", "session_id": "session-1"}}}
        self.state.write_text(json.dumps(state))
        start = Mock()
        nudge = Mock(return_value=(False, "HTTP 404: gone"))
        result = self.coordinator(start, nudge).launch(["Devin-Builder"], "cog_test")
        self.assertFalse(result["ok"])
        self.assertIn("Nudge failed", result["errors"]["Devin-Builder"])
        start.assert_not_called()

    def test_unconfirmed_launch_is_neither_nudged_nor_retried(self):
        state = {"platform": launcher.PLATFORM,
                 "launches": {"Devin-Builder": {"status": "unconfirmed"}}}
        self.state.write_text(json.dumps(state))
        start = Mock()
        nudge = Mock()
        result = self.coordinator(start, nudge).launch(["Devin-Builder"], "cog_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["already_requested"], ["Devin-Builder"])
        start.assert_not_called()
        nudge.assert_not_called()

    def test_nudge_uses_devin_prefixed_session_id(self):
        response = Mock()
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(launcher.urllib.request, "urlopen", return_value=response) as urlopen:
            ok, error = launcher.nudge_session("cog_test", "Devin-Builder", "abc123")
        self.assertTrue(ok)
        self.assertIsNone(error)
        request = urlopen.call_args.args[0]
        self.assertIn("/sessions/devin-abc123/messages", request.full_url)
        self.assertIn("Devin-Builder", request.data.decode())

    def test_missing_key_does_not_launch_or_write(self):
        start = Mock()
        with patch.object(launcher, "load_api_key", return_value=""):
            result = self.coordinator(start).launch(["Devin-Builder"])
        self.assertFalse(result["ok"])
        self.assertIn("API key", result["error"])
        start.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_failure_is_visible_and_stops_remaining_launches(self):
        start = Mock(return_value=(False, None, "HTTP 403: Forbidden"))
        result = self.coordinator(start).launch(["Devin-Builder", "Devin-Reviewer"], "cog_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertIn("403", result["error"])
        self.assertEqual(result["not_attempted"], ["Devin-Reviewer"])
        start.assert_called_once()

    def test_partial_success_not_reported_as_complete(self):
        start = Mock(side_effect=[(True, "session-1", None), (False, None, "HTTP 429: Limited")])
        result = self.coordinator(start).launch(["Devin-Builder", "Devin-Reviewer"], "cog_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["failed"], ["Devin-Reviewer"])

    def test_ambiguous_failure_is_not_retried(self):
        start = Mock(return_value=(False, None, "Unconfirmed: request timed out"))
        self.coordinator(start).launch(["Devin-Builder"], "cog_test")
        result = self.coordinator(start).launch(["Devin-Builder"], "cog_test")
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["ok"])
        self.assertIn("review", result["error"].lower())
        start.assert_called_once()

    def test_error_redacts_key(self):
        start = Mock(return_value=(False, None, "HTTP 403 cog_test"))
        result = self.coordinator(start).launch(["Devin-Builder"], "cog_test")
        self.assertNotIn("cog_test", json.dumps(result))
        self.assertNotIn("cog_test", self.state.read_text())

    def test_broken_registry_fails_closed(self):
        self.state.write_text("not-json")
        start = Mock()
        result = self.coordinator(start).launch(["Devin-Builder"], "cog_test")
        self.assertFalse(result["ok"])
        start.assert_not_called()

    def test_concurrent_click_is_rejected(self):
        entered = threading.Event()
        release = threading.Event()
        def start(*_):
            entered.set()
            release.wait(2)
            return True, "session-1", None
        coordinator = self.coordinator(start)
        worker = threading.Thread(target=coordinator.launch, args=(["Devin-Builder"], "cog_test"))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            result = coordinator.launch(["Devin-Builder"], "cog_test")
            self.assertTrue(result["busy"])
        finally:
            release.set()
            worker.join()

    def test_http_failure_does_not_expose_credential(self):
        error = urllib.error.HTTPError("https://api.devin.ai", 403, "Forbidden", {},
                                       io.BytesIO(b'{"detail":"bad cog_test"}'))
        with patch.object(launcher.urllib.request, "urlopen", side_effect=error):
            success, _, message = launcher.start_session("cog_test", "Devin-Builder")
        self.assertFalse(success)
        self.assertNotIn("cog_test", message)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("chat_server_under_test", ROOT / "server.py")
        self.server = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"call_all": launcher}), patch.object(threading.Thread, "start") as start:
            spec.loader.exec_module(self.server)
            start.assert_not_called()
        self.server.ROOMS["general"] = self.server.Room("general", members=["Devin-Builder"])
        self.handler = self.server.ChatHandler.__new__(self.server.ChatHandler)
        self.handler.path = "/call_all"
        self.handler.headers = {}
        self.handler.rfile = io.BytesIO()
        self.handler._send_json = Mock()

    def test_failed_session_launch_is_not_listener_success(self):
        result = {"ok": False, "started": [], "count": 0, "error": "HTTP 403"}
        with patch.object(self.server.LAUNCHER, "launch", return_value=result) as launch, \
                patch.object(self.server.subprocess, "Popen") as popen, \
                patch.object(self.server.subprocess, "run") as run:
            self.handler.do_POST()
        launch.assert_called_once_with(["Devin-Builder"])
        popen.assert_not_called()
        run.assert_not_called()
        code, body = self.handler._send_json.call_args.args
        self.assertEqual(code, 502)
        self.assertEqual(body["started_pollers"], [])
        self.assertEqual(body["nudged_count"], 0)
        self.assertFalse(body["ok"])

    def test_non_devin_and_moderator_names_are_filtered(self):
        self.server.ROOMS["general"].members.update(["anonymous", "Devin-Moderator", "human"])
        self.server.ROOMS["general"].messages.append(
            {"ts": 1, "sender": "anonymous", "text": "hi", "room": "general", "role": "ai"})
        result = {"ok": True, "started": [], "nudged": ["Devin-Builder"], "count": 0,
                  "sessions": {}, "errors": {}, "already_requested": [], "not_attempted": []}
        with patch.object(self.server.LAUNCHER, "launch", return_value=result) as launch:
            self.handler.do_POST()
        launch.assert_called_once_with(["Devin-Builder"])
        body = self.handler._send_json.call_args.args[1]
        self.assertIn("anonymous", body["skipped_names"])

    def test_actual_session_count_is_returned(self):
        result = {"ok": True, "started": ["Devin-Builder"], "count": 1,
                  "sessions": {"Devin-Builder": "session-1"}}
        with patch.object(self.server.LAUNCHER, "launch", return_value=result):
            self.handler.do_POST()
        code, body = self.handler._send_json.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"]["Devin-Builder"], "session-1")

    def test_browser_displays_failure_and_session_ids(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable for browser handler test")
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        function = html.split("async function callAllAIs()", 1)[1].split("async function createRoom()", 1)[0]
        script = "const assert = require('assert');\n" + "async function callAllAIs()" + function + r'''
let status = {textContent: ''};
let btn = {disabled: false, parentElement: {insertAdjacentElement(){}}};
global.document = {getElementById: id => id === 'callAllBtn' ? btn : status};
global.currentRoom = 'general';
global.confirm = () => true;
global.setTimeout = fn => fn();
(async () => {
  global.fetch = async () => ({ok:false, json:async () => ({ok:false,count:0,error:'HTTP 403'})});
  await callAllAIs();
  assert(status.textContent.includes('Created 0 AI session(s)'));
  assert(status.textContent.includes('HTTP 403'));
  global.fetch = async () => ({ok:true, json:async () => ({ok:true,count:1,sessions:{'Devin-Builder':'session-1'}})});
  await callAllAIs();
  assert(status.textContent.includes('Created 1 AI session(s)'));
  assert(status.textContent.includes('session-1'));
  global.confirm = () => false;
  global.fetch = () => { throw new Error('Must not launch after canceled confirmation'); };
  await callAllAIs();
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
