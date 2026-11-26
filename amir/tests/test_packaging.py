"""Packaging: no secrets in compose or the image, publishing off by default, a guarded deploy.

The deploy tests run a copy of scripts/deploy.sh inside a throwaway repo under tmp_path. Fake
`ssh` and `rsync` come first on PATH: ssh only records its arguments, and rsync records them,
then runs the real rsync into a local folder that stands in for the server. Nothing can reach
a server, even if a guard broke.
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy.sh"
VOICE_CLIP_PATH = ROOT / "tests" / "fixtures" / "audio" / "armstrong-small-step.ogg"
EXAMPLE = ROOT / "config" / "amir.example.toml"
SERVER = "amir-test@deploy.invalid"
DEPLOY_DIR = "~/amir-test-dir"  # the server folder, as the setting AMIR_DEPLOY_DIR gives it
REMOTE_DEST = f"{SERVER}:{DEPLOY_DIR}/"
REAL_RSYNC = shutil.which("rsync")
REMOTE_COMMAND_TAIL = "docker compose up -d --force-recreate"
RUNTIME_IDS = {  # constructed Discord ids
    "guild_id": 2001,
    "voice_channel_id": 3001,
    "news_channel_id": 3002,
    "cycles_channel_id": 3003,
    "voice_log_channel_id": 3004,
}

FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\n' "--- ssh" "$@" >> "$FAKE_CALLS"
"""
FAKE_RSYNC = """#!/usr/bin/env bash
printf '%s\\n' "--- rsync" "$@" >> "$FAKE_CALLS"
args=("$@")
args[${#args[@]}-1]="$FAKE_SERVER_DIR/"
exec "$REAL_RSYNC" "${args[@]}"
"""
# The server's docker, for running the deploy's remote command locally: each tag is a file in
# $FAKE_DOCKER_STATE holding an image id, and a build gives :latest the id in next-build. As the
# server's containerd image store (Docker 29.9.1, 2026-10-27), an image is found by a tag, or by
# its id only while some tag still names it: once a build moves :latest, the old image's id is
# "No such image" although its container still runs.
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "--- docker" "$@" >> "$FAKE_CALLS"
tag_file() { printf '%s/%s' "$FAKE_DOCKER_STATE" "${1//:/_}"; }
image_of() {
    if [[ -f "$(tag_file "$1")" ]]; then cat "$(tag_file "$1")"; return 0; fi
    grep -lxF -- "$1" "$FAKE_DOCKER_STATE"/*_* >/dev/null 2>&1 && { printf '%s\\n' "$1"; return 0; }
    echo "Error response from daemon: No such image: $1" >&2
    return 1
}
case "$1 $2" in
    "image inspect")
        id="$(image_of "${!#}")" || exit 1
        [[ "$3" == "-f" ]] && printf '%s\\n' "$id"
        exit 0
        ;;
    "image tag")
        id="$(image_of "$3")" || exit 1
        printf '%s\\n' "$id" > "$(tag_file "$4")"
        ;;
    "image rm")
        rm "$(tag_file "${!#}")" || exit 1
        ;;
    "compose build" | "compose up")
        if [[ "$2" == build || " $* " == *" --build "* ]]; then
            cp "$FAKE_DOCKER_STATE/next-build" "$(tag_file amir-beckett:latest)"
        fi
        ;;
esac
"""


def lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def make_repo(tmp_path: Path, publishing: str | None = "off", *, ids: bool = True) -> Path:
    """The parts of a working copy that deploy.sh reads or must leave behind. `publishing` is
    "on" or "off" for a config/amir.toml derived from the example, or None for no config; `ids`
    sets the five Discord IDs in it. Synced files start private (0600), as an editor may save
    them."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(DEPLOY, repo / "scripts" / "deploy.sh")
    (repo / ".venv").symlink_to(Path(sys.prefix))  # the project's Python, for the config check
    shutil.copy2(ROOT / "compose.yaml", repo / "compose.yaml")
    shutil.copy2(ROOT / ".env.example", repo / ".env.example")
    clip = repo / "assets" / "greetings" / "amir-greeting-01.ogg"  # the test clip as a greeting
    clip.parent.mkdir(parents=True)
    shutil.copy2(VOICE_CLIP_PATH, clip)
    clip.chmod(0o600)
    for name in (".env", ".env.local"):
        (repo / name).write_text("DISCORD_TOKEN=local-token-must-never-leave\n", encoding="utf-8")
    for name in ("data/amir.sqlite3", ".git/HEAD", "docs/previews/cards.html", ".tool-cache/x"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("local only\n", encoding="utf-8")
    if publishing is not None:
        text = EXAMPLE.read_text(encoding="utf-8")
        if publishing == "on":
            assert text.count("enabled = false") == 1
            text = text.replace("enabled = false", "enabled = true")
        if ids:
            for key, value in RUNTIME_IDS.items():
                text, count = re.subn(rf"(?m)^{key} = 0$", f"{key} = {value}", text)
                assert count == 1, key
        (repo / "config").mkdir()
        (repo / "config" / "amir.toml").write_text(text, encoding="utf-8")
        (repo / "config" / "amir.toml").chmod(0o600)
    return repo


def run_deploy(
    tmp_path: Path,
    repo: Path,
    *args: str,
    server: str | None = SERVER,
    deploy_dir: str | None = DEPLOY_DIR,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run the repo's deploy.sh with the fakes; returns the result and the recorded calls.
    `server` is DEPLOY_SERVER and `deploy_dir` is AMIR_DEPLOY_DIR in its environment; None
    leaves either unset."""
    if REAL_RSYNC is None:
        pytest.skip("rsync is not installed")
    fakes = tmp_path / "bin"
    fakes.mkdir(exist_ok=True)
    for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
        (fakes / name).write_text(body, encoding="utf-8")
        (fakes / name).chmod(0o755)
    calls_file = tmp_path / "calls.log"
    server_dir = tmp_path / "server"
    server_dir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CALLS": str(calls_file),
        "FAKE_SERVER_DIR": str(server_dir),
        "REAL_RSYNC": REAL_RSYNC,
    }
    env.pop("DISCORD_TOKEN", None)
    env.pop("AMIR_CONFIG", None)
    env.pop("DEPLOY_SERVER", None)
    env.pop("AMIR_DEPLOY_DIR", None)
    if server is not None:
        env["DEPLOY_SERVER"] = server
    if deploy_dir is not None:
        env["AMIR_DEPLOY_DIR"] = deploy_dir
    done = subprocess.run(
        ["bash", str(repo / "scripts" / "deploy.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls: list[list[str]] = []
    if calls_file.exists():
        for line in calls_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("--- "):
                calls.append([line[4:]])
            else:
                calls[-1].append(line)
    return done, calls


def test_compose_has_no_secrets():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert not re.search(r"DISCORD_TOKEN\s*[=:]\s*\S", compose)
    assert re.search(r"(?m)^\s*env_file:\s*\.env\s*$", compose)
    for ignore_file in (".gitignore", ".dockerignore"):
        ignored = lines(ROOT / ignore_file)
        assert ".env*" in ignored, ignore_file  # .env and any .env.local, .envrc ...
        assert ignored.index("!.env.example") > ignored.index(".env*"), ignore_file


def _check_ignore(root: Path, names: list[str]) -> subprocess.CompletedProcess[str]:
    """`git check-ignore` over `names`; skips if git is missing or `root` is outside a work tree."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    done = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--no-index", "--verbose", "--non-matching"]
        + names,
        capture_output=True,
        text=True,
        timeout=30,
        # LC_ALL=C pins git's message language. amir/ lives inside the fleet repo, so git
        # must be free to find the work tree above it.
        env={**os.environ, "LC_ALL": "C"},
    )
    if done.returncode == 128 and "not a git repository" in done.stderr:
        pytest.skip(f"{root} is not inside a git work tree")
    return done


def test_check_ignore_skips_outside_a_git_work_tree(tmp_path):
    with pytest.raises(pytest.skip.Exception, match="not inside a git work tree"):
        _check_ignore(tmp_path, [".env"])


def test_git_ignores_every_env_file_but_the_example():
    names = [".env", ".env.local", ".env.production", ".envrc", ".env.deploy", ".env.example"]
    done = _check_ignore(ROOT, names)
    assert done.returncode in (0, 1), done.stderr
    # --verbose --non-matching prints "<source>:<line>:<pattern>\t<path>"; "::\t" = no match.
    rules = dict(reversed(line.split("\t", 1)) for line in done.stdout.splitlines())
    for name in names[:-1]:
        assert rules[name].endswith(":.env*"), name
    assert rules[".env.example"].endswith(":!.env.example")


def test_example_config_publishing_off():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert tomllib.loads(text)["publishing"]["enabled"] is False
    assert re.search(r"(?m)^enabled = false\b", text)


def test_real_config_and_state_stay_out_of_git_and_the_image():
    assert "config/amir.toml" in lines(ROOT / ".gitignore")
    ignored = set(lines(ROOT / ".dockerignore"))
    for entry in (".env*", "data/", ".venv/", ".git", "docs/", "config/amir.toml"):
        assert entry in ignored, entry


def test_container_hardening_is_locked():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for setting in (
        r"read_only: true",
        r"init: true",
        r"cap_drop: \[ALL\]",
        r"- no-new-privileges:true",
        r"tmpfs:\n\s+- /tmp",
        r"- \./config/amir\.toml:/app/config/amir\.toml:ro",
        r"- \./assets/greetings:/app/assets/greetings:ro",
        r"- amir-data:/data",
        r"restart: unless-stopped",
        r"stop_grace_period: 20s",
        r"driver: json-file\n\s+options:\n\s+max-size: \"10m\"\n\s+max-file: \"3\"",
        r'test: \["CMD", "python", "-m", "amir\.health"\]',
    ):
        assert re.search(rf"(?m)^\s*{setting}\s*$", compose), setting
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    for setting in (
        r"RUN useradd --uid 10001 .* amir .*",
        r"USER amir",
        r"VOLUME /data",
        r'HEALTHCHECK .* CMD \["python","-m","amir\.health"\]',
        r'CMD \["python","-m","amir"\]',
    ):
        assert re.search(rf"(?m)^{setting}$", dockerfile), setting


def test_deploy_script_guards_publishing(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path, publishing="on"), "--dry-run")
    assert done.returncode != 0
    assert "approv" in (done.stdout + done.stderr).lower()
    assert calls == []


def test_deploy_script_needs_local_config(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path, publishing=None), "--dry-run")
    assert done.returncode != 0
    assert "config/amir.toml" in done.stderr
    assert calls == []


def test_deploy_script_needs_every_discord_id(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path, ids=False), "--dry-run")
    assert done.returncode != 0
    assert "discord.guild_id" in done.stderr and "discord.voice_log_channel_id" in done.stderr
    assert calls == []


def test_deploy_script_refuses_an_invalid_config(tmp_path):
    repo = make_repo(tmp_path)
    config = repo / "config" / "amir.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace('level = "INFO"', 'level = "LOUD"'),
        encoding="utf-8",
    )
    done, calls = run_deploy(tmp_path, repo, "--dry-run")
    assert done.returncode != 0
    assert "logging.level" in done.stderr
    assert calls == []


def test_deploy_dry_run_changes_nothing_and_skips_ssh(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert [call[0] for call in calls] == ["rsync"]
    assert "--dry-run" in calls[0] and calls[0][-1] == REMOTE_DEST
    assert list((tmp_path / "server").iterdir()) == []
    assert REMOTE_COMMAND_TAIL in done.stdout


def test_deploy_publishing_needs_the_flag_and_says_so(tmp_path):
    repo = make_repo(tmp_path, publishing="on")
    done, calls = run_deploy(tmp_path, repo, "--dry-run", "--allow-publishing")
    assert done.returncode == 0, done.stderr
    assert "Publishing: ON" in done.stdout
    assert "publishing is ON" in done.stderr
    assert [call[0] for call in calls] == ["rsync"]


def test_deploy_syncs_the_repo_but_never_secrets_or_state(tmp_path):
    assert os.access(DEPLOY, os.X_OK)  # the owner runs scripts/deploy.sh directly
    repo = make_repo(tmp_path)
    server = tmp_path / "server"
    server.mkdir()
    (server / ".env").write_text("DISCORD_TOKEN=the-server-token\n", encoding="utf-8")
    (server / "data").mkdir()
    (server / "data" / "keep").write_text("server state\n", encoding="utf-8")
    (server / "removed-locally.txt").write_text("stale\n", encoding="utf-8")

    done, calls = run_deploy(tmp_path, repo)

    assert done.returncode == 0, done.stderr
    assert [call[0] for call in calls] == ["rsync", "ssh"]
    assert calls[0][-1] == REMOTE_DEST
    ssh = calls[1]
    assert ssh[1:-1] == [SERVER]
    # A config-only change keeps the image and the compose file identical, so only
    # --force-recreate makes the container see the newly synced config/amir.toml.
    assert ssh[-1].endswith(REMOTE_COMMAND_TAIL)
    assert (server / "config" / "amir.toml").read_bytes() == (
        repo / "config" / "amir.toml"
    ).read_bytes()
    assert (server / "compose.yaml").exists()
    assert (server / "scripts" / "deploy.sh").exists()
    assert (server / ".env").read_text(encoding="utf-8") == "DISCORD_TOKEN=the-server-token\n"
    assert (server / "data" / "keep").read_text(encoding="utf-8") == "server state\n"
    assert not (server / "data" / "amir.sqlite3").exists()
    assert not (server / "removed-locally.txt").exists()
    for name in (".git", ".venv", "docs/previews", ".tool-cache", ".env.local"):
        assert not (server / name).exists(), name
    assert (server / ".env.example").exists()
    # UID 10001 in the container must be able to read what was synced, whatever the local mode.
    for path in (server / "config" / "amir.toml", server / "assets/greetings/amir-greeting-01.ogg"):
        assert path.stat().st_mode & stat.S_IROTH, path
    for folder in (server / "config", server / "assets" / "greetings"):
        assert folder.stat().st_mode & (stat.S_IROTH | stat.S_IXOTH) == (
            stat.S_IROTH | stat.S_IXOTH
        ), folder
    assert "local-token-must-never-leave" not in done.stdout + done.stderr


def image_id(build: str) -> str:
    """A docker image id (constructed, like the Discord ids above)."""
    return "sha256:" + hashlib.sha256(build.encode()).hexdigest()


def run_remote_command(
    tmp_path: Path, command: str, tags: dict[str, str], built: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[list[str]]]:
    """Run the deploy's remote command under bash with a fake docker whose images carry `tags`
    and whose next build has the id `built`. Returns the result, the tags after, the calls."""
    home = tmp_path / "home"
    (home / "amir-test-dir").mkdir(parents=True)
    state = tmp_path / "docker"
    state.mkdir()
    for tag, image in tags.items():
        (state / tag.replace(":", "_")).write_text(image + "\n", encoding="utf-8")
    (state / "next-build").write_text(built + "\n", encoding="utf-8")
    fakes = tmp_path / "server-bin"
    fakes.mkdir()
    (fakes / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (fakes / "docker").chmod(0o755)
    calls_file = tmp_path / "docker-calls.log"
    env = {
        **os.environ,
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "FAKE_CALLS": str(calls_file),
        "FAKE_DOCKER_STATE": str(state),
    }
    done = subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, timeout=30
    )
    after = {
        f.name.replace("_", ":", 1): f.read_text(encoding="utf-8").strip()
        for f in state.iterdir()
        if f.name != "next-build"
    }
    calls: list[list[str]] = []
    for line in calls_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("--- "):
            calls.append([])
        else:
            calls[-1].append(line)
    return done, after, calls


@pytest.mark.parametrize(
    ("before", "built", "after"),
    [
        pytest.param(
            {"amir-beckett:latest": image_id("b"), "amir-beckett:previous": image_id("a")},
            image_id("c"),
            {"amir-beckett:latest": image_id("c"), "amir-beckett:previous": image_id("b")},
            id="new-image",
        ),
        pytest.param(
            {"amir-beckett:latest": image_id("b"), "amir-beckett:previous": image_id("a")},
            image_id("b"),
            {"amir-beckett:latest": image_id("b"), "amir-beckett:previous": image_id("a")},
            id="unchanged-image-keeps-the-rollback",
        ),
        pytest.param({}, image_id("a"), {"amir-beckett:latest": image_id("a")}, id="first-deploy"),
    ],
)
def test_deploy_tags_previous_only_when_the_image_changes(tmp_path, before, built, after):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path))
    assert done.returncode == 0, done.stderr
    assert [call[0] for call in calls] == ["rsync", "ssh"]
    command = calls[-1][-1]

    ran, tags, docker = run_remote_command(tmp_path, command, before, built)

    assert ran.returncode == 0, ran.stderr
    assert tags == after
    assert tags.get("amir-beckett:previous") != tags["amir-beckett:latest"]
    up = [call for call in docker if call[:2] == ["compose", "up"]]
    assert len(up) == 1 and {"-d", "--force-recreate"} <= set(up[0])
    assert docker[-1] == up[0]  # the container is recreated last, after any tagging


def test_deploy_never_glob_expands_the_env_exclude(tmp_path, monkeypatch):
    # A file in the caller's directory that the unquoted `--exclude=.env*` would expand to.
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "--exclude=.env.decoy").write_text("", encoding="utf-8")
    monkeypatch.chdir(caller)
    done, calls = run_deploy(tmp_path, make_repo(tmp_path), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "--exclude=.env*" in calls[0]
    assert "--exclude=.env.decoy" not in calls[0]


def test_no_server_no_deploy(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path), "--dry-run", server=None)
    assert done.returncode != 0
    assert "DEPLOY_SERVER" in done.stderr
    assert calls == []


def test_the_server_comes_from_the_ignored_deploy_file(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".env.deploy").write_text(f"# the server\nDEPLOY_SERVER={SERVER}\n", encoding="utf-8")
    done, calls = run_deploy(tmp_path, repo, "--dry-run", server=None)
    assert done.returncode == 0, done.stderr
    assert calls[0][-1] == REMOTE_DEST


def test_the_environment_server_wins_over_the_file(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".env.deploy").write_text("DEPLOY_SERVER=amir-file@deploy.invalid\n", encoding="utf-8")
    done, calls = run_deploy(tmp_path, repo, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert calls[0][-1] == REMOTE_DEST


def test_no_deploy_folder_no_deploy(tmp_path):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path), "--dry-run", deploy_dir=None)
    assert done.returncode != 0
    assert "AMIR_DEPLOY_DIR" in done.stderr
    assert ".deploy.env" in done.stderr
    assert calls == []


def test_the_deploy_folder_comes_from_the_ignored_file(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts" / ".deploy.env").write_text(
        f"# the folder\nAMIR_DEPLOY_DIR={DEPLOY_DIR}\n", encoding="utf-8"
    )
    done, calls = run_deploy(tmp_path, repo, "--dry-run", deploy_dir=None)
    assert done.returncode == 0, done.stderr
    assert calls[0][-1] == REMOTE_DEST
    assert f"cd {DEPLOY_DIR} &&" in done.stdout


def test_the_environment_deploy_folder_wins_over_the_file(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts" / ".deploy.env").write_text("AMIR_DEPLOY_DIR=~/from-file\n", encoding="utf-8")
    done, calls = run_deploy(tmp_path, repo, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert calls[0][-1] == REMOTE_DEST


@pytest.mark.parametrize("bad", ["~/a b", "~/a;reboot", "~/$(x)", "/"])
def test_an_unsafe_deploy_folder_is_refused(tmp_path, bad):
    done, calls = run_deploy(tmp_path, make_repo(tmp_path), "--dry-run", deploy_dir=bad)
    assert done.returncode != 0
    assert "AMIR_DEPLOY_DIR" in done.stderr
    assert calls == []


def test_git_ignores_the_local_deploy_file():
    done = _check_ignore(ROOT, ["scripts/.deploy.env"])
    assert done.returncode in (0, 1), done.stderr
    assert not done.stdout.startswith("::"), "scripts/.deploy.env is not ignored"


def test_no_server_folder_is_named_in_the_repo():
    name = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    folder = re.compile(rf"~/{name}|\b{name}/")
    for path in (DEPLOY, ROOT / "README.md", *(ROOT / "docs").glob("*.md")):
        assert folder.findall(path.read_text(encoding="utf-8")) == [], path.name


def test_the_deploy_script_and_readme_name_no_server_or_discord_id():
    address = re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b")
    discord_id = re.compile(r"\b\d{17,19}\b")
    for path in (DEPLOY, ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert address.findall(text) == [], path.name
        assert discord_id.findall(text) == [], path.name
