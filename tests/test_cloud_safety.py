"""Offline failure-injection tests; no credentials, RSS or webhook requests."""
import ast
import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def method(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace[name]


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class CloudSafetyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_r2_404_is_absent_but_permission_and_connection_errors_are_fatal(self):
        check = method("trendradar/storage/remote.py", "_check_object_exists", {"os": os, "ClientError": ClientError})
        backend = SimpleNamespace(s3_client=Mock(), bucket_name="test")
        backend.s3_client.head_object.side_effect = ClientError("404")
        self.assertFalse(check(backend, "rss/today.db"))
        for error in (ClientError("403"), TimeoutError("credential-must-not-appear")):
            backend.s3_client.head_object.side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                check(backend, "rss/today.db")
            self.assertNotIn("credential", str(caught.exception))

    def test_failed_news_and_rss_writes_are_fatal(self):
        for name in ("save_news_data", "save_rss_data"):
            save = method("trendradar/storage/manager.py", name, {})
            backend = Mock()
            getattr(backend, name).return_value = False
            manager = SimpleNamespace(get_backend=lambda: backend, is_github_actions=lambda: True)
            with self.assertRaises(RuntimeError):
                save(manager, object())

    def test_collection_failure_cannot_reach_notification(self):
        run = method("trendradar/__main__.py", "run", {})
        analyzer = Mock()
        analyzer.is_github_actions = True
        analyzer._initialize_and_check_config.return_value = True
        analyzer._crawl_data.return_value = ({}, {}, [])
        analyzer._crawl_rss_data.side_effect = RuntimeError("R2 failed")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            run(analyzer)
        analyzer._execute_mode_strategy.assert_not_called()
        analyzer.ctx.cleanup.assert_called_once()

    def test_failed_wework_delivery_is_fatal_without_resend(self):
        send = method("trendradar/__main__.py", "_send_notification_if_needed", {})
        dispatcher = Mock()
        dispatcher.dispatch_all.return_value = {"wework": False}
        analyzer = SimpleNamespace(
            is_github_actions=True, _has_notification_configured=lambda: True,
            _has_valid_content=lambda *args: False, frequency_file=None,
            _hotlist_total_count=0, _rss_matched_count=1, _rss_total_count=1,
            _rss_source_total=2, _rss_source_failed=0, update_info=None, proxy_url=None,
            ctx=SimpleNamespace(config={"ENABLE_NOTIFICATION": True, "SHOW_VERSION_UPDATE": False},
                prepare_report=lambda *args, **kwargs: {}, platform_ids=[],
                create_notification_dispatcher=lambda: dispatcher))
        schedule = SimpleNamespace(push=True, once_push=False)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            send(analyzer, [], "test", "incremental", rss_items=[{"count": 1}], schedule=schedule)
        dispatcher.dispatch_all.assert_called_once()

    def test_workflow_and_syntax_boundaries(self):
        workflow = (ROOT / ".github/workflows/crawler.yml").read_text(encoding="utf-8")
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("group: crawler-r2", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("AI_API_KEY", workflow)
        self.assertNotIn("wrangler", workflow)
        self.assertIn("LIMIT_SECONDS=604800", workflow)
        for path in (ROOT / "trendradar").rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main()
