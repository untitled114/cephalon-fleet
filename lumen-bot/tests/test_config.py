"""config.yaml holds only what Lumen reads: an unread setting silently does nothing.

Every section and every key is checked against Lumen's code, so a setting the code never reads
(or stops reading) fails here by name instead of staying in the file looking like a switch.
"""

from pathlib import Path

import yaml

HERE = Path(__file__).parent.parent
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
CODE = "\n".join(p.read_text() for p in sorted(HERE.glob("*.py")))


def _reads(name: str) -> bool:
    return f'.get("{name}"' in CODE or f'["{name}"]' in CODE


def test_every_section_is_one_lumen_reads():
    assert [section for section in CONFIG if not _reads(section)] == []


def test_every_setting_is_one_lumen_reads():
    unread = [
        f"{section}.{key}"
        for section, settings in CONFIG.items()
        for key in settings or {}
        if not _reads(key)
    ]
    assert unread == []
