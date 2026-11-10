"""Small offline safety checks; no AI or real-game test automation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sphere_agent.bridge import Bridge
from sphere_agent.engine import Engine
from sphere_agent.storage import Ledger, redact, writer_lock


class SafetyChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agent.db"
        self.ledger = Ledger(self.path)
        self.bridge = AsyncMock()
        self.config = {
            "allow_write": True,
            "max_writes": 2,
            "max_recipe_executions": 3,
            "pause_file": str(Path(self.temp.name) / "PAUSE"),
            "action_timeout_seconds": 0.1,
            "poll_seconds": 0.01,
        }
        self.engine = Engine(self.bridge, self.ledger, self.config, allow_write=True)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    async def test_readonly_never_calls_mcp_write(self):
        self.engine.allow_write = False
        with self.assertRaisesRegex(RuntimeError, "Read-only"):
            await self.engine.handcraft(1, 1)
        self.bridge.call.assert_not_called()

    async def test_pending_without_action_id_never_replays(self):
        self.ledger.set_pending({"key": "original-key", "stage": "commit_intent"})
        with self.assertRaisesRegex(RuntimeError, "manual upstream"):
            await self.engine.reconcile()
        with self.assertRaisesRegex(RuntimeError, "Unresolved"):
            await self.engine.handcraft(1, 1)
        self.bridge.call.assert_not_called()
        self.assertEqual(self.ledger.pending()["key"], "original-key")

    async def test_pause_blocks_prepare(self):
        Path(self.config["pause_file"]).touch()
        with self.assertRaisesRegex(RuntimeError, "PAUSE"):
            await self.engine.handcraft(1, 1)
        self.bridge.call.assert_not_called()

    async def test_terminal_mismatch_preserves_evidence(self):
        self.ledger.set_pending({"actionId": "original", "sessionId": "s", "planetId": 1, "key": "k"})
        self.bridge.call.return_value = {
            "actionId": "original",
            "terminal": True,
            "succeeded": True,
            "sessionId": "different",
            "planetId": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            await self.engine.reconcile()
        self.assertTrue(self.ledger.pending()["terminal"]["succeeded"])
        self.assertEqual(self.bridge.call.call_args.args[0], "spherewright_get_action_result")

    async def test_verified_original_action_resolves_without_commit(self):
        self.ledger.set_pending(
            {
                "actionId": "a",
                "sessionId": "s",
                "planetId": 1,
                "key": "k",
                "count": 1,
                "recipe": {"outputs": [{"itemId": 2, "count": 1}]},
            }
        )
        self.bridge.call.return_value = {
            "actionId": "a",
            "terminal": True,
            "succeeded": True,
            "sessionId": "s",
            "planetId": 1,
            "idempotencyKey": "k",
            "actionKind": "handcraft",
            "afterStateHash": "h",
            "itemDeltas": [{"itemId": 2, "delta": 1}],
        }
        with patch.object(
            self.engine,
            "observe",
            AsyncMock(return_value=({"player": {}}, {"sessionId": "s", "planetId": 1})),
        ):
            await self.engine.reconcile()
        self.assertIsNone(self.ledger.pending())
        self.assertEqual(self.bridge.call.await_count, 1)

    async def test_raw_mcp_mutations_not_allowed(self):
        bridge = Bridge(AsyncMock(), 1)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            await bridge.call("spherewright_commit_build", {})

    def test_tokens_redacted_and_single_writer_locked(self):
        self.assertEqual(redact({"nested": {"planToken": "secret"}}), {"nested": {"planToken": "[redacted]"}})
        with writer_lock(self.path):
            with self.assertRaises(OSError), writer_lock(self.path):
                self.fail("Second writer should not enter")


if __name__ == "__main__":
    unittest.main()
