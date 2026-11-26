"""Every fixture says where its real data came from, and none says where production lives."""

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
RECORDS = sorted(FIXTURES.rglob("provenance.json"))
# Any web address, host:path, absolute server path or IPv4 address (named generically, so the
# test itself names no host).
WHERE_PRODUCTION_LIVES = re.compile(
    r"://|\b[\w-]+\.(?:com|net|org|io|dev|app)\b|\b[\w-]+:/(?:home|opt|etc|srv|var)/|"
    r"(?<![\w.])/(?:home|opt|etc|srv)/[\w.-]+|\b\d{1,3}(?:\.\d{1,3}){3}\b"
)


def test_every_fixture_folder_has_a_provenance_record():
    folders = {p.parent for p in FIXTURES.rglob("*") if p.is_file()}
    assert folders == {r.parent for r in RECORDS}


def test_no_provenance_record_names_the_server_or_its_addresses():
    named = {
        str(r.relative_to(FIXTURES)): WHERE_PRODUCTION_LIVES.findall(r.read_text()) for r in RECORDS
    }
    assert {r: hits for r, hits in named.items() if hits} == {}


def test_each_record_still_says_its_data_is_from_production():
    for record in RECORDS:
        assert "production" in json.dumps(json.loads(record.read_text())), record
