"""Greeting clip library: real FFmpeg decode validation and a no-immediate-repeat pick."""

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".mp4", ".aac", ".flac", ".qt", ".mov"}
_SCAN_CONCURRENCY = 4


async def validate_clip(
    ffmpeg: str,
    path: Path,
    timeout: float = 20.0,  # noqa: ASYNC109 - callers bound the whole playback
) -> bool:
    """A clip is valid iff FFmpeg decodes its first audio stream, exit code 0 and no errors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.debug("could not start ffmpeg for %s: %s", path.name, type(exc).__name__)
        return False
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        log.debug("clip check timed out: %s", path.name)
        return False
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    # FFmpeg exits 0 on a truncated file while reporting decode errors at -v error, so any
    # error output also means the clip is not cleanly decodable.
    if proc.returncode == 0 and not lines:
        return True
    detail = _redact(lines[0], path) if lines else "no output"
    log.debug("clip check failed: %s (%s)", path.name, detail)
    return False


def _redact(text: str, path: Path) -> str:
    """Replace the clip's full path (as given and resolved) and its directory in FFmpeg output."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    for full in sorted({str(path), str(resolved)}, key=len, reverse=True):
        text = text.replace(full, path.name)
    for parent in sorted({str(path.parent), str(resolved.parent)}, key=len, reverse=True):
        text = text.replace(parent, "<dir>")
    return text


@dataclass
class ScanReport:
    valid: list[Path] = field(default_factory=list)
    invalid: list[Path] = field(default_factory=list)


class GreetingLibrary:
    def __init__(self, directory: Path, ffmpeg: str, rng: random.Random | None = None) -> None:
        self._directory = directory
        self._ffmpeg = ffmpeg
        self._rng = rng or random.Random()
        self._valid: list[Path] = []
        self._bad: set[Path] = set()
        self._last: Path | None = None

    @property
    def valid_count(self) -> int:
        return len(self._available())

    def _available(self) -> list[Path]:
        return [p for p in self._valid if p not in self._bad]

    def _candidates(self) -> list[Path]:
        try:
            if not self._directory.is_dir():
                return []
            root = self._directory.resolve()
            entries = sorted(self._directory.iterdir())
        except (OSError, RuntimeError) as exc:
            log.warning("cannot read the greetings directory: %s", type(exc).__name__)
            return []
        found: dict[Path, None] = {}
        for entry in entries:
            if entry.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                resolved = entry.resolve()
                is_file = resolved.is_file()
            except (OSError, RuntimeError) as exc:
                log.warning("skipping %s: cannot resolve (%s)", entry.name, type(exc).__name__)
                continue
            if not resolved.is_relative_to(root):
                log.warning("skipping %s: resolves outside the greetings directory", entry.name)
                continue
            if is_file:
                found[resolved] = None
        return list(found)

    async def scan(self) -> ScanReport:
        candidates = self._candidates()
        gate = asyncio.Semaphore(_SCAN_CONCURRENCY)

        async def check(path: Path) -> bool:
            async with gate:
                return await validate_clip(self._ffmpeg, path)

        results = await asyncio.gather(*(check(p) for p in candidates))
        report = ScanReport(
            valid=[p for p, ok in zip(candidates, results, strict=True) if ok],
            invalid=[p for p, ok in zip(candidates, results, strict=True) if not ok],
        )
        self._valid = list(report.valid)
        self._bad.clear()
        self._last = None
        for path in report.invalid:
            log.warning("greeting clip failed validation: %s", path.name)
        if not report.valid:
            log.warning("no valid greeting clips found; Amir will join silently")
        return report

    def pick(self) -> Path | None:
        options = self._available()
        if not options:
            return None
        if len(options) > 1:
            options = [p for p in options if p != self._last]
        choice = self._rng.choice(options)
        self._last = choice
        return choice

    def mark_bad(self, path: Path) -> None:
        self._bad.add(path)
