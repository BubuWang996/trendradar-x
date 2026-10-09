"""Fail-closed Actions entry point using the official storage and crawler."""
import os
import sys
import time
from collections import Counter
from datetime import timedelta

from trendradar.__main__ import NewsAnalyzer


def main():
    started = time.monotonic()
    calls = Counter()
    analyzer = None
    try:
        required = ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                    "S3_ENDPOINT_URL", "WEWORK_WEBHOOK_URL")
        if any(not os.environ.get(name) for name in required):
            raise RuntimeError("Required cloud credentials missing")
        analyzer = NewsAnalyzer()
        cfg = analyzer.ctx.config
        if (analyzer.storage_manager.backend_name != "remote"
                or analyzer.report_mode != "incremental"
                or cfg["AI_ANALYSIS"]["ENABLED"]
                or cfg["AI_TRANSLATION"]["ENABLED"]
                or analyzer.ctx.filter_method != "keyword"):
            raise RuntimeError("Cloud configuration guard failed")
        backend = analyzer.storage_manager.get_backend()
        def count_call(model, **kwargs):
            calls[model.name] += 1
        backend.s3_client.meta.events.register("before-call.s3", count_call)
        today = backend._format_date_folder()
        key = backend._get_remote_db_key(today, "rss")
        if not backend._check_object_exists(key):
            # Official databases are per-day. Establish a quiet baseline on rollover.
            yesterday = backend._format_date_folder(
                (backend._get_configured_time() - timedelta(days=1)).strftime("%Y-%m-%d"))
            previous_key = backend._get_remote_db_key(yesterday, "rss")
            if not backend._check_object_exists(previous_key):
                raise RuntimeError("No verified R2 history; refusing empty-state push")
            previous = backend._get_connection(yesterday, "rss")
            if previous.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("R2 previous state is invalid")
            cfg["ENABLE_NOTIFICATION"] = False
            print("[Cloud guard] New day: quiet baseline, no historical bulk push")
        current = backend._get_connection(today, "rss")
        if current.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("R2 current state is invalid")
        if cfg["ENABLE_NOTIFICATION"] and current.execute(
                "SELECT COUNT(*) FROM rss_items").fetchone()[0] == 0:
            raise RuntimeError("R2 established state is empty; refusing historical push")
        print("[Cloud guard] R2 state verified before collection and notification")
        analyzer.run()
    except Exception as error:
        print(f"[Cloud guard] FAILED ({type(error).__name__}); no automatic retry")
        return 1
    finally:
        if analyzer:
            analyzer.ctx.cleanup()
        print(f"[Cloud metrics] elapsed_seconds={time.monotonic() - started:.2f}; "
              f"s3_api_calls={dict(calls)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
