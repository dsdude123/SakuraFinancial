"""Root test configuration: make every service package importable.

Each service keeps its own uniquely named package (settings_service,
ledger_service, ...) inside its own directory; putting each service directory
on sys.path lets a single pytest invocation run the whole monorepo.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent

sys.path.insert(0, str(ROOT / "libs" / "common"))
services_dir = ROOT / "services"
if services_dir.is_dir():
    for service_dir in sorted(services_dir.iterdir()):
        if service_dir.is_dir():
            sys.path.insert(0, str(service_dir))
