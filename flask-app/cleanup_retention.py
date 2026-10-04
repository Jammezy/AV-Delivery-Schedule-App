"""Preview by default; production apply needs explicit deployment controls."""
import argparse
import json
import os
import signal
import sys
import time
from urllib.parse import urlsplit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--local-test", action="store_true", help="Explicit disposable SQLite mode only")
    parser.add_argument("--max-rows", type=int, default=1000)
    parser.add_argument("--lock-timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    db = None
    started = time.monotonic()
    try:
        url = os.environ.get("DATABASE_URL", "").strip()
        if args.local_test:
            if url or not os.environ.get("DATABASE_PATH", "").strip():
                raise ValueError("Local test mode requires DATABASE_PATH and no DATABASE_URL")
            if not os.path.isfile(os.environ["DATABASE_PATH"]):
                raise ValueError("Local test storage must already exist and be initialized")
        else:
            parsed = urlsplit(url)
            if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
                raise ValueError("Production cleanup requires a PostgreSQL DATABASE_URL")
            # Exact expected host AND database guard against accidental targets.
            if (parsed.hostname != os.environ.get("RETENTION_DATABASE_HOST") or
                    parsed.path.lstrip("/") != os.environ.get("RETENTION_DATABASE_NAME")):
                raise ValueError("Database identity must match RETENTION_DATABASE_HOST and RETENTION_DATABASE_NAME")
        if args.apply and os.environ.get("RETENTION_ENABLED") != "true":
            raise ValueError("Apply requires RETENTION_ENABLED=true")
        from models import db
        from retention import run_retention
        result = run_retention(apply=args.apply, max_rows=args.max_rows, timeout_ms=args.lock_timeout_ms)
        result["durationSeconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Database exceptions can contain secrets/payloads: log only the class.
        print(json.dumps({"status": "failed", "errorType": type(error).__name__,
                          "message": str(error) if isinstance(error, ValueError) else "Cleanup failed; transaction rolled back. Check configuration, schema and database availability."}), file=sys.stderr)
        return 1
    finally:
        if db is not None and not db.is_closed():
            db.close()


if __name__ == "__main__":
    def cancelled(signum, frame):
        raise KeyboardInterrupt("Cleanup cancelled")
    signal.signal(signal.SIGTERM, cancelled)
    sys.exit(main())
