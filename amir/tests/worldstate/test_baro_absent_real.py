"""Real Baro-absent captures taken 2026-10-26 (Baro left 2026-10-25 13:00 UTC)."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from amir.worldstate.official import BaroVisit, parse_void_trader
from amir.worldstate.wfcd import parse_baro_location, parse_cycles, parse_inventory_names

FOLDER = Path(__file__).parents[1] / "fixtures" / "captured" / "2026-10-26"
OFFICIAL = FOLDER / "official_worldstate.json"
WFCD = FOLDER / "wfcd_pc.json"


def capture_time(name: str) -> datetime:
    """Capture time of `name`, read from the folder's README table (America/New_York)."""
    for line in (FOLDER / "README.md").read_text().splitlines():
        if line.startswith(f"| `{name}`"):
            stamp = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d) E[DS]T", line).group(1)
            naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
    raise AssertionError(f"{name} not in README")


def test_capture_time_is_read_from_readme():
    assert capture_time(OFFICIAL.name) == datetime(2026, 10, 26, 18, 43, tzinfo=UTC)


def test_baro_absent_real():
    visit = parse_void_trader(OFFICIAL.read_bytes())
    assert isinstance(visit, BaroVisit)
    captured_at = capture_time(OFFICIAL.name)
    assert visit.activation > captured_at
    assert visit.activation == datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
    assert visit.expiry == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    assert visit.node == "MercuryHUB"
    assert visit.manifest == ()


def test_next_activation_listed_at_least_72h_ahead():
    visit = parse_void_trader(OFFICIAL.read_bytes())
    lead = visit.activation - capture_time(OFFICIAL.name)
    assert lead >= timedelta(hours=72)
    assert lead == timedelta(hours=90, minutes=17)


def test_wfcd_cycles_work_while_baro_absent():
    cycles = parse_cycles(WFCD.read_bytes())
    assert set(cycles) == {"cetus", "vallis", "cambion"}
    assert cycles["cetus"].state == "night"
    assert cycles["cetus"].expiry == datetime(2026, 10, 26, 17, 41, tzinfo=UTC)
    assert cycles["vallis"].state == "cold"
    assert cycles["vallis"].expiry == datetime(2026, 10, 26, 17, 33, 28, tzinfo=UTC)
    assert cycles["cambion"].state == "vome"


def test_wfcd_agrees_with_official_and_has_no_inventory():
    official = parse_void_trader(OFFICIAL.read_bytes())
    location = parse_baro_location(WFCD.read_bytes())
    assert location == (official.activation, "Larunda Relay (Mercury)")
    assert json.loads(WFCD.read_bytes())["voidTrader"]["inventory"] == []
    assert parse_inventory_names(WFCD.read_bytes()) == {}
