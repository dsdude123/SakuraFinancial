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


import pytest


# A brokerage activity export with the awkward shapes real ones have — a
# multi-line preamble padded with blanks, "--" where a column doesn't apply to
# a row, a negative quantity on the sale, and cash activity mixed in with
# trades. The figures are invented; never put real statement data in the repo.
BROKER_ACTIVITY_CSV = """All Transactions Activity Types

Account Activity for Individual Brokerage -0000 from Prior Year

Total:,0.00

Activity/Trade Date,Transaction Date,Settlement Date,Activity Type,Description,Symbol,Cusip,Quantity #,Price $,Amount $,Commission,Category,Note
04/09/2025,04/09/2025,04/09/2025,Online Transfer,ACH DEPOSIT  REFID:000000000000;,--,--,,,1000.0,0.0,--,--
04/07/2025,04/07/2025,04/07/2025,Service Fee,ADV FEE 04/01-04/30,--,--,,,-2.00,0.0,--,--
04/04/2025,04/04/2025,04/04/2025,Dividend,EXAMPLE BOND ETF INCOME,EXB,--,,,3.00,0.0,--,--
04/03/2025,04/03/2025,04/03/2025,Dividend,EXAMPLE TOTAL MARKET ETF,EXM,--,,,7.00,0.0,--,--
03/31/2025,03/31/2025,03/31/2025,Dividend,EXAMPLE LARGE CAP ETF,EXL,--,,,2.00,0.0,--,--
03/28/2025,03/28/2025,03/31/2025,Bought,EXAMPLE MID CAP ETF,EXD,--,0.100,50.00,-5.00,0.0,--,--
03/28/2025,03/28/2025,03/31/2025,Bought,EXAMPLE LARGE CAP ETF,EXL,--,2.000,60.00,-120.00,0.0,--,--
03/28/2025,03/28/2025,03/31/2025,Bought,EXAMPLE TOTAL MARKET ETF,EXM,--,0.500,40.00,-20.00,0.0,--,--
03/28/2025,03/28/2025,03/31/2025,Sold,EXAMPLE INTERNATIONAL ETF  LOT 20240618,EXI,--,-1.500,50.00,75.00,0.01,--,--
03/28/2025,03/28/2025,03/31/2025,Bought,EXAMPLE BOND ETF,EXB,--,1.000,100.00,-100.00,0.0,--,--
"""


@pytest.fixture()
def broker_activity_csv() -> str:
    return BROKER_ACTIVITY_CSV
