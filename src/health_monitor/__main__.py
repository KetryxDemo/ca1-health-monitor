"""Allow ``python -m health_monitor`` to start the service."""

from health_monitor.service import main

if __name__ == "__main__":
    raise SystemExit(main())
