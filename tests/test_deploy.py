"""Tests for the deployment files: deploy/*.sh, docker-compose.yml, .env.example, Caddyfile,
Dockerfile, GitHub workflows and the Arabic operator docs.

Shell scripts run for real under bash with stub `docker`, `ss`, `ip`, `curl`, `rclone` and
`systemctl` commands on PATH, inside a temporary copy of deploy/, so nothing touches the host.
The docker stub keeps a tiny image store (tags and ids) so release/rollback tags can be checked.
Tests that need the real thing (rclone's local backend, uvicorn, ssh-keygen) skip when it is absent;
deploy/selftest.sh covers real containers (restore, forwarded headers) in a throwaway compose project.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
SCRIPTS = ("bootstrap.sh", "deploy.sh", "backup.sh", "selftest.sh")
WORKFLOWS = REPO / ".github" / "workflows"
DOCS = REPO / "docs" / "ar"
DOMAIN = "rotator.magic-lap.com"
# Operator guides must point to the actual deployed application, independently of stub hosts.
DOC_DOMAIN = "repeater.magic-lap.com"

DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
printf 'YTDLP_REFRESH=%s REPEATER_IMAGE_TAG=%s WORKER_STOP_GRACE_S=%s\n' "${YTDLP_REFRESH:-}" "${REPEATER_IMAGE_TAG:-}" "${WORKER_STOP_GRACE_S:-}" >> "$STUB_ENV_LOG"
images="$STUB_STATE/images"
mkdir -p "$images"
tag_file() { printf '%s/%s' "$images" "${1//[:\/]/_}"; }
resolve_id() { if [[ -f "$(tag_file "$1")" ]]; then cat "$(tag_file "$1")"; else printf '%s' "$1"; fi; }
case "$1" in
  volume) [[ -n "${STUB_PG_VOLUME:-}" ]] && exit 0; exit 1 ;;
  ps)
    if [[ " $* " == *" -aq "* ]]; then
      [[ -s "${STUB_INSPECT:-/dev/null}" ]] && seq 1 "$(wc -l < "$STUB_INSPECT")" | sed 's/^/c/'
      exit 0
    fi
    exit 0 ;;
  image)
    case "$2" in
      inspect)
        ref="${*: -1}"
        [[ -f "$(tag_file "$ref")" ]] || exit 1
        if [[ " $* " == *" --format "* ]]; then
          id="$(cat "$(tag_file "$ref")")"
          # `sha256:v2#7`: content "v2", a build that got its own image id (containerd image store)
          if [[ " $* " == *RootFS* ]]; then echo "layers-of-${id%%#*}"; else echo "$id"; fi
        fi
        exit 0 ;;
      tag)
        if grep -qxF "$3" "$STUB_STATE/gone" 2>/dev/null; then
          echo "Error response from daemon: No such image: $3" >&2; exit 1
        fi
        resolve_id "$3" > "$(tag_file "$4")"; exit 0 ;;
      ls) ls "$images" | sed -n 's/^repeater-app_//p'; exit 0 ;;
    esac
    exit 0 ;;
  rmi) rm -f "$(tag_file "$2")"; exit 0 ;;
  inspect)
    if [[ " $* " == *PortBindings* ]]; then cat "${STUB_INSPECT:-/dev/null}";
    elif [[ " $* " == *NetworkSettings.Networks* ]]; then
      if [[ -n "${STUB_PROXY_JSON:-}" ]]; then
        if [[ " $* " == *'{{json '* ]]; then printf '%s\n' "$STUB_PROXY_JSON";
        else printf '%s\n' '172.19.0.1' 'invalid IP'; fi
      else echo "${STUB_PROXY_IP:-172.18.0.1}"; fi;
    else echo "${STUB_HEALTH:-healthy}"; fi
    exit 0 ;;
  exec) cat "${STUB_CADDYFILE:-/dev/null}"; exit 0 ;;
  version) echo "29.0.0"; exit 0 ;;
  compose) shift ;;
  *) exit 0 ;;
esac
while [[ $# -gt 0 ]]; do
  case "$1" in --project-directory|-f|--env-file) shift 2 ;; *) break ;; esac
done
sub="${1:-}"; shift || true
case "$sub" in
  version) echo "Docker Compose version v2.30.0" ;;
  config) exit "${STUB_CONFIG_RC:-0}" ;;
  build)
    [[ "${STUB_BUILD_RC:-0}" == 0 ]] || exit "$STUB_BUILD_RC"
    old="$(cat "$(tag_file repeater-app:latest)" 2>/dev/null)"
    printf '%s' "${STUB_BUILD_ID:-sha256:build-$$}" > "$(tag_file repeater-app:latest)"
    # containerd image store: an image that loses its last tag is deleted at once
    if [[ -n "$old" && "$old" != "$(cat "$(tag_file repeater-app:latest)")" ]] && ! grep -lxF "$old" "$images"/* >/dev/null 2>&1; then
      echo "$old" >> "$STUB_STATE/gone"
    fi
    exit 0 ;;
  pull) exit "${STUB_PULL_RC:-0}" ;;
  up) [[ " $* " == *" --wait db"* ]] && : > "$STUB_STATE/db_started"; exit 0 ;;
  run) exit "${STUB_MIGRATE_RC:-0}" ;;
  ps)
    svc="${*: -1}"
    if [[ " $* " == *" --status running "* ]]; then
      case "$svc" in
        db) if [[ -e "$STUB_STATE/db_started" ]]; then printf dbid; else printf '%s' "${STUB_RUNNING_DB-dbid}"; fi ;;
        web) printf '%s' "${STUB_RUNNING_WEB-}" ;;
        worker) printf '%s' "${STUB_RUNNING_WORKER-}" ;;
      esac
      exit 0
    fi
    if [[ " $* " == *" -q "* ]]; then echo "cid-${svc}"; exit 0; fi
    echo "SERVICE STATUS" ;;
  exec)
    [[ "${1:-}" == "-T" ]] && shift
    shift
    printf '%s\n' "$*" >> "$STUB_EXEC_LOG"
    case "${1:-}" in
      pg_dump)
        if [[ -n "${STUB_PGDUMP_FAIL:-}" ]]; then echo "pg_dump: connection refused" >&2; exit 1; fi
        printf -- '--\n-- PostgreSQL database dump\n--\nSELECT 1;\n' ;;
      psql)
        if [[ " $* " == *" -c "* ]]; then exit "${STUB_PSQL_C_RC:-0}"; fi
        cat > "$STUB_STDIN_FILE"; exit "${STUB_PSQL_RC:-0}" ;;
      python)
        if [[ " $* " == *" check-proxy "* ]]; then
          if [[ -n "${STUB_PROXY_PYTHON:-}" ]]; then
            while [[ "${1:-}" != check-proxy ]]; do shift; done
            shift
            exec "$STUB_PROXY_PYTHON" -m repeater.cli check-proxy "$@"
          fi
          exit "${STUB_PROXY_CHECK_RC:-0}"
        fi
        cat > "$STUB_STDIN_FILE"
        echo "Warning: Password input may be echoed." >&2
        printf 'Password: admin created\n'
        exit "${STUB_ADMIN_RC:-0}" ;;
    esac ;;
esac
exit 0
"""

SS_STUB = r"""#!/usr/bin/env bash
args="$*"
if [[ "$args" == *"sport = :80 or sport = :443"* ]]; then
  cat "${STUB_SS_WEB:-/dev/null}"
elif [[ "$args" == *"sport = :"* ]]; then
  port="$(grep -oE 'sport = :[0-9]+' <<<"$args" | grep -oE '[0-9]+')"
  if grep -qx "$port" "${STUB_SS_BUSY:-/dev/null}" 2>/dev/null; then
    echo "LISTEN 0 4096 127.0.0.1:$port 0.0.0.0:*"
  fi
fi
exit 0
"""

IP_STUB = r"""#!/usr/bin/env bash
case "$*" in
  *docker0*) echo "4: docker0    inet 172.18.0.1/16 brd 172.18.255.255 scope global docker0" ;;
  *"route get"*) echo "1.1.1.1 via 203.0.113.1 dev eth0 src 203.0.113.10 uid 0" ;;
  *"addr show"*)
    echo "1: lo    inet 127.0.0.1/8 scope host lo"
    echo "2: eth0    inet 203.0.113.10/24 brd 203.0.113.255 scope global eth0"
    echo "4: docker0    inet 172.18.0.1/16 brd 172.18.255.255 scope global docker0" ;;
esac
"""

SYSTEMCTL_STUB = r"""#!/usr/bin/env bash
# `systemctl is-enabled|is-active UNIT`; STUB_UNITS lists "UNIT enabled" / "UNIT active" lines.
state="${1#is-}"
if [[ "$1" == is-enabled || "$1" == is-active ]]; then
  if grep -qx "${2:-} $state" "${STUB_UNITS:-/dev/null}" 2>/dev/null; then echo "$state"; exit 0; fi
  if [[ "$state" == enabled ]]; then echo disabled; else echo inactive; fi
  exit 1
fi
exit 0
"""

RCLONE_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_RCLONE_LOG"
printf '%s\\n' "${RCLONE_CONFIG:-}" >> "$STUB_RCLONE_LOG.config"
if [[ -n "${STUB_RCLONE_FAIL_ON:-}" && " $* " == *" ${STUB_RCLONE_FAIL_ON} "* ]]; then exit 1; fi
if [[ "${STUB_RCLONE_RC:-0}" != 0 ]]; then exit "$STUB_RCLONE_RC"; fi
if [[ "$1" == cat ]]; then
    if [[ -n "${STUB_RCLONE_CORRUPT:-}" ]]; then printf corrupted
    else cat "$REPEATER_BACKUP_DIR/${2##*/}"; fi
fi
if [[ "$1" == lsf ]]; then printf '%s' "${STUB_RCLONE_LSF:-}"; fi
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_CURL_ARGS"
if [[ " $* " == *" --config - "* ]]; then cat >> "$STUB_CURL_STDIN"; fi
exit "${STUB_CURL_RC:-0}"
"""

OK_STUB = "#!/usr/bin/env bash\nexit 0\n"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A copy of deploy/ under tmp_path/app/deploy plus stub commands; returns paths and env."""
    deploy = tmp_path / "app" / "deploy"
    deploy.mkdir(parents=True)
    for name in (*SCRIPTS, "monitor.py", ".env.example", "docker-compose.yml", "Caddyfile"):
        shutil.copy2(DEPLOY / name, deploy / name)
    (tmp_path / "state").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "docker": DOCKER_STUB,
        "ss": SS_STUB,
        "ip": IP_STUB,
        "rclone": RCLONE_STUB,
        "systemctl": SYSTEMCTL_STUB,
        "curl": CURL_STUB,
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    logs = tmp_path / "logs"
    logs.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "STUB_LOG": str(logs / "docker.log"),
        "STUB_ENV_LOG": str(logs / "docker-env.log"),
        "STUB_EXEC_LOG": str(logs / "exec.log"),
        "STUB_STDIN_FILE": str(logs / "stdin"),
        "STUB_RCLONE_LOG": str(logs / "rclone.log"),
        "STUB_STATE": str(tmp_path / "state"),
        "STUB_CURL_ARGS": str(logs / "curl-args.log"),
        "STUB_CURL_STDIN": str(logs / "curl-stdin.log"),
        "REPEATER_COOLIFY_DIR": str(tmp_path / "no-coolify"),
        "REPEATER_ROOT": str(tmp_path / "srv"),
        "REPEATER_BACKUP_DIR": str(tmp_path / "backups"),
        "REPEATER_CRON_FILE": str(tmp_path / "cron-repeater-backup"),
        "HEALTH_TIMEOUT": "5",
    }
    return {"root": tmp_path, "deploy": deploy, "logs": logs, "env": env}


def run(
    sandbox: dict, argv: list[str], *, extra_env: dict | None = None, stdin: str = ""
) -> subprocess.CompletedProcess:
    env = {**sandbox["env"], **(extra_env or {})}
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=60, check=False)


def bootstrap_fn(sandbox: dict, snippet: str, **kw) -> subprocess.CompletedProcess:
    """Source the sandbox bootstrap.sh (main does not run) and execute a bash snippet."""
    script = f'set -Eeuo pipefail; source "{sandbox["deploy"]}/bootstrap.sh"; {snippet}'
    return run(sandbox, ["bash", "-c", script], **kw)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def docker_log(sandbox: dict) -> list[str]:
    path = sandbox["logs"] / "docker.log"
    return path.read_text().splitlines() if path.exists() else []


def make_env(sandbox: dict, **overrides: str) -> Path:
    """A complete deploy/.env in the sandbox (as bootstrap.sh would leave it)."""
    values = {
        "POSTGRES_PASSWORD": "a" * 64,
        "REPEATER_SECRET_KEY": "s" * 64,
        "REPEATER_TOKEN_ENCRYPTION_KEYS": "k1:" + "A" * 43,
        "REPEATER_BASE_URL": f"https://{DOMAIN}",
        "REPEATER_MEDIA_DIR": str(sandbox["root"] / "media"),
        **overrides,
    }
    path = sandbox["deploy"] / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)
    return path


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False)
    return probe.returncode == 0


def compose_config(profiles: str = "", **extra_env: str) -> dict:
    env = {**os.environ, "COMPOSE_PROFILES": profiles, **extra_env}
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(DEPLOY / "docker-compose.yml"),
            "--env-file",
            str(DEPLOY / ".env.example"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout)


def size_bytes(value: str | int) -> int:
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"(\d+)([kmg]?)b?", str(value).strip().lower())
    assert match, value
    return int(match.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2)]


# --------------------------------------------------------------------------- static checks


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_syntax(name: str) -> None:
    result = subprocess.run(["bash", "-n", str(DEPLOY / name)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _shellcheck() -> str | None:
    return os.environ.get("SHELLCHECK") or shutil.which("shellcheck")


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_shellcheck(name: str) -> None:
    shellcheck = _shellcheck()
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "-x", str(DEPLOY / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


def test_env_example_lists_every_setting() -> None:
    from repeater.config import Settings

    text = (DEPLOY / ".env.example").read_text()
    missing = [
        f"REPEATER_{name.upper()}"
        for name in Settings.model_fields
        if not re.search(rf"^#? ?REPEATER_{name.upper()}=", text, re.MULTILINE)
    ]
    assert not missing, f"add to deploy/.env.example: {missing}"
    for key in (
        "POSTGRES_PASSWORD",
        "REPEATER_DOMAIN",
        "REPEATER_WEB_PORT",
        "REPEATER_MEDIA_DIR",
        "COMPOSE_PROFILES",
    ):
        assert re.search(rf"^{key}=", text, re.MULTILINE), key


def test_env_example_parses_as_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from repeater.config import Settings

    for key in list(os.environ):
        if key.startswith("REPEATER_"):
            monkeypatch.delenv(key)
    settings = Settings(_env_file=DEPLOY / ".env.example")
    assert settings.is_production
    assert settings.base_url == f"https://{DOMAIN}"
    assert settings.oauth_redirect_uri("tiktok") == f"https://{DOMAIN}/oauth/tiktok/callback"
    assert settings.max_upload_mb == 500
    assert settings.meta_graph_version == "v26.0"
    assert settings.ytdlp_cookies_dir is None
    assert settings.trusted_hosts == ["*"]
    # Generated secrets are blank in the template so nobody runs with a shared value.
    assert settings.secret_key == ""
    assert settings.token_encryption_keys == ""


def test_dockerfile_contract() -> None:
    text = (REPO / "Dockerfile").read_text()
    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12-slim$", text, re.MULTILINE)
    assert "ffmpeg ca-certificates tini" in text
    assert "--uid 10001" in text and re.search(r"^USER 10001:10001$", text, re.MULTILINE)
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in text
    assert "http://127.0.0.1:8000/healthz" in text and "HEALTHCHECK" in text
    # The shipped image is the LAST stage, so a plain `docker build` (and compose) builds it.
    stages = re.findall(r"^FROM .* AS (\w+)$", text, re.MULTILINE)
    assert stages[-1] == "app" and "test" in stages
    cmd = re.findall(r"^CMD \[(.*?)\]$", text, re.MULTILINE | re.DOTALL)[-1]
    for part in ('"repeater.web.app:create_app"', '"--factory"', '"--proxy-headers"'):
        assert part in cmd
    assert "COPY alembic.ini" in text or "alembic.ini ./" in text
    assert "COPY migrations" in text
    # Dependencies are installed before the code is copied (cached layer).
    assert text.index("pip install -r") < text.index("COPY repeater ./repeater")
    # Dev tools exist only in the CI test stage, never in the shipped one.
    app_stage = text[text.index("FROM base AS app") :]
    assert "[dev]" not in app_stage and "pytest" not in app_stage.replace("# ", "")


def test_uvicorn_never_trusts_every_forwarder() -> None:
    """security-2 / deploy-ops-11: `--forwarded-allow-ips "*"` makes uvicorn take the LEFTMOST
    X-Forwarded-For entry, which the client controls behind a proxy that appends (nginx's
    $proxy_add_x_forwarded_for)."""
    text = (REPO / "Dockerfile").read_text()
    cmd = re.findall(r"^CMD \[(.*?)\]$", text, re.MULTILINE | re.DOTALL)[-1]
    assert "--forwarded-allow-ips" not in cmd and '"*"' not in cmd
    assert "FORWARDED_ALLOW_IPS" in text  # the comment explaining where the value comes from


def test_dockerignore_keeps_secrets_out() -> None:
    lines = {line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()}
    for pattern in (".env", "**/.env", "deploy", "media", "**/.git", ".venv", "tests"):
        assert pattern in lines, pattern
    for needed in ("README.md", "pyproject.toml", "alembic.ini", "migrations", "repeater"):
        assert needed not in lines


def test_caddyfile_contract() -> None:
    text = (DEPLOY / "Caddyfile").read_text()
    assert "{$REPEATER_DOMAIN} {" in text
    assert re.search(r"request_body \{\s*max_size 600MB\s*\}", text)
    assert "reverse_proxy web:8000" in text
    assert "encode zstd gzip" in text
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "-Server"):
        assert header in text
    assert re.search(r"read_body 1h", text) and re.search(r"write 1h", text)
    assert "log {" not in text, "access logs would record OAuth codes and signed media links"


# --------------------------------------------------------------------------- docker compose


requires_compose = pytest.mark.skipif(not compose_available(), reason="docker compose is not installed")


@requires_compose
def test_compose_services_and_profiles() -> None:
    assert set(compose_config()["services"]) == {"db", "migrate", "web", "worker"}
    assert set(compose_config("caddy")["services"]) == {"db", "migrate", "web", "worker", "caddy"}


@requires_compose
def test_compose_web_is_loopback_only() -> None:
    web = compose_config()["services"]["web"]
    assert len(web["ports"]) == 1
    port = web["ports"][0]
    assert port["host_ip"] == "127.0.0.1"
    assert str(port["published"]) == "8090"
    assert port["target"] == 8000
    for name, service in compose_config("caddy")["services"].items():
        if name not in ("web", "caddy"):
            assert not service.get("ports"), f"{name} must not publish ports"


@requires_compose
def test_compose_dependencies_and_restart() -> None:
    services = compose_config("caddy")["services"]
    assert services["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("web", "worker"):
        assert services[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        assert services[name]["depends_on"]["db"]["condition"] == "service_healthy"
    assert services["migrate"]["command"] == ["python", "-m", "repeater.cli", "migrate"]
    assert services["worker"]["command"] == ["python", "-m", "repeater.jobs.worker"]
    assert services["migrate"]["restart"] == "no"
    for name in ("db", "web", "worker", "caddy"):
        assert services[name]["restart"] == "unless-stopped"
    assert "pg_isready" in " ".join(services["db"]["healthcheck"]["test"])
    worker_check = " ".join(services["worker"]["healthcheck"]["test"])
    assert "REPEATER_WORKER_HEARTBEAT_PATH" in worker_check and "getmtime" in worker_check


@requires_compose
def test_compose_logging_and_limits_fit_the_share() -> None:
    services = compose_config("caddy")["services"]
    long_running = ("db", "web", "worker", "caddy")
    for name, service in services.items():
        assert service["logging"]["driver"] == "json-file"
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "5"}
        assert service["deploy"]["resources"]["limits"]["memory"], name
    cpus = sum(float(services[n]["deploy"]["resources"]["limits"]["cpus"]) for n in long_running)
    memory = sum(size_bytes(services[n]["deploy"]["resources"]["limits"]["memory"]) for n in long_running)
    # The caps are ceilings on a 4 vCPU / 16 GB VPS shared with other projects: at most 3 vCPU and
    # 6 GB for Repeater, so the others always keep a vCPU.
    assert cpus <= 3.0 + 1e-9, cpus
    assert memory <= 6 * 1024**3, memory


@requires_compose
def test_worker_is_sized_for_the_medium_preset() -> None:
    """deploy-ops-missed-1: -preset medium on 1 CPU encodes a worst-case clip at ~5.4x its length,
    so the 1800 s cap would fail clips over ~5.5 minutes alone and ~2.7 with two encodes at once;
    2 CPUs halve that."""
    worker = compose_config()["services"]["worker"]["deploy"]["resources"]["limits"]
    assert float(worker["cpus"]) >= 2.0
    assert size_bytes(worker["memory"]) >= 3 * 1024**3  # two ffmpeg processes of ~0.6 GB + python
    assert "REPEATER_WORKER_CPUS=2.0" in (DEPLOY / ".env.example").read_text()


@requires_compose
def test_worker_stop_grace_is_the_job_grace_plus_a_minute() -> None:
    """deploy-ops-6: a deploy recreates the worker; docker must not SIGKILL running encodes and
    uploads before the worker's own REPEATER_WORKER_SHUTDOWN_GRACE_S (600 s) has passed."""
    from repeater.config import Settings

    default = Settings.model_fields["worker_shutdown_grace_s"].default
    assert compose_config()["services"]["worker"]["stop_grace_period"] == "11m0s"
    assert default + 60 == 660
    # deploy.sh / backup.sh hand compose the sum of the configured value and 60 s.
    assert compose_config(WORKER_STOP_GRACE_S="960")["services"]["worker"]["stop_grace_period"] == "16m0s"


@requires_compose
def test_compose_media_and_secrets_wiring() -> None:
    services = compose_config("caddy")["services"]
    for name in ("web", "worker"):
        mounts = [v for v in services[name]["volumes"] if v["type"] == "bind"]
        assert [(m["source"], m["target"], bool(m.get("read_only"))) for m in mounts] == [
            ("/srv/repeater/media", "/srv/repeater/media", False),
            ("/srv/repeater/backups/status", "/srv/repeater/backup-status", True),
        ]
        env = services[name]["environment"]
        assert env["REPEATER_MEDIA_ROOT"] == "/srv/repeater/media"
        assert env["REPEATER_BACKUP_STATUS_FILE"] == "/srv/repeater/backup-status/last_success"
        assert env["REPEATER_DATABASE_URL"].startswith("postgresql+psycopg://repeater:")
        assert env["REPEATER_DATABASE_URL"].endswith("@db:5432/repeater")
        assert "no-new-privileges:true" in services[name]["security_opt"]
    # The database never receives the application's secrets.
    assert "env_file" not in services["db"]
    assert "REPEATER_SECRET_KEY" not in services["db"].get("environment", {})
    assert any(v.get("source") == "pg_data" for v in services["db"]["volumes"])
    caddy = services["caddy"]
    assert {str(p["published"]) for p in caddy["ports"]} == {"80", "443"}
    assert any(v["target"] == "/etc/caddy/Caddyfile" and v.get("read_only") for v in caddy["volumes"])


@requires_compose
def test_compose_builds_the_app_stage() -> None:
    assert compose_config()["services"]["web"]["build"]["target"] == "app"


def _default_forwarded_allow_ips() -> str:
    match = re.search(
        r'FORWARDED_ALLOW_IPS: "\$\{REPEATER_FORWARDED_ALLOW_IPS:-([^}]*)\}"',
        (DEPLOY / "docker-compose.yml").read_text(),
    )
    assert match, "docker-compose.yml must set FORWARDED_ALLOW_IPS from REPEATER_FORWARDED_ALLOW_IPS"
    return match.group(1)


def _client_seen(trusted: str, peer: str, forwarded_for: str) -> str:
    """The client address uvicorn's ProxyHeadersMiddleware hands the app."""
    import asyncio

    middleware_mod = pytest.importorskip("uvicorn.middleware.proxy_headers")
    seen: dict = {}

    async def app(scope, receive, send):  # noqa: ANN001 - minimal ASGI app
        seen["client"] = scope["client"][0]

    async def go() -> None:
        scope = {
            "type": "http",
            "client": (peer, 5000),
            "scheme": "http",
            "headers": [(b"x-forwarded-for", forwarded_for.encode())],
        }
        await middleware_mod.ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None)

    asyncio.run(go())
    return seen["client"]


def test_forwarded_allow_ips_default_is_loopback_and_docker_ranges_only() -> None:
    value = _default_forwarded_allow_ips()
    assert "*" not in value
    assert set(value.split(",")) == {"127.0.0.1", "::1", "172.16.0.0/12", "192.168.0.0/16"}


@pytest.mark.parametrize(
    "peer",
    ["127.0.0.1", "::1", "172.17.0.1", "172.19.0.5", "172.31.255.254", "192.168.16.2"],
)
def test_client_typed_x_forwarded_for_is_ignored_behind_a_trusted_proxy(peer: str) -> None:
    """A proxy that APPENDS (nginx's $proxy_add_x_forwarded_for) gives "typed-by-client, real":
    the app must see the rightmost untrusted entry, never the leftmost."""
    assert _client_seen(_default_forwarded_allow_ips(), peer, "6.6.6.6, 203.0.113.9") == "203.0.113.9"
    # The old setting is what made the leftmost entry win.
    assert _client_seen("*", peer, "6.6.6.6, 203.0.113.9") == "6.6.6.6"


@pytest.mark.parametrize("peer", ["203.0.113.50", "8.8.8.8", "172.32.0.1", "10.0.0.5"])
def test_x_forwarded_for_from_an_untrusted_peer_is_ignored(peer: str) -> None:
    assert _client_seen(_default_forwarded_allow_ips(), peer, "6.6.6.6") == peer


# --------------------------------------------------------------------------- bootstrap.sh


def test_bootstrap_generates_env_with_valid_secrets(sandbox: dict) -> None:
    from repeater.crypto import KeyRing

    result = bootstrap_fn(
        sandbox, "create_env_file; set_domain; fill_secrets", extra_env={"REPEATER_DOMAIN": DOMAIN}
    )
    assert result.returncode == 0, result.stderr
    env_path = sandbox["deploy"] / ".env"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    values = read_env(env_path)
    assert re.fullmatch(r"[0-9a-f]{64}", values["POSTGRES_PASSWORD"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{64}", values["REPEATER_SECRET_KEY"])
    ring = KeyRing.parse(values["REPEATER_TOKEN_ENCRYPTION_KEYS"])
    assert ring.active_kid == "k1" and len(ring.keys["k1"]) == 32
    assert values["REPEATER_DOMAIN"] == DOMAIN
    assert values["REPEATER_BASE_URL"] == f"https://{DOMAIN}"
    assert values["REPEATER_ENV"] == "production"
    assert values["REPEATER_MEDIA_DIR"] == sandbox["env"]["REPEATER_ROOT"] + "/media"
    # Secret values never reach the output.
    for key in ("POSTGRES_PASSWORD", "REPEATER_SECRET_KEY"):
        assert values[key] not in result.stdout + result.stderr


def test_bootstrap_rerun_keeps_existing_secrets(sandbox: dict) -> None:
    first = bootstrap_fn(
        sandbox, "create_env_file; set_domain; fill_secrets", extra_env={"REPEATER_DOMAIN": DOMAIN}
    )
    assert first.returncode == 0, first.stderr
    before = read_env(sandbox["deploy"] / ".env")
    second = bootstrap_fn(
        sandbox,
        "create_env_file; set_domain; fill_secrets",
        extra_env={"REPEATER_DOMAIN": "other.magic-lap.com"},
    )
    assert second.returncode == 0, second.stderr
    after = read_env(sandbox["deploy"] / ".env")
    assert after == before
    assert "keeping REPEATER_DOMAIN" in second.stderr


def test_bootstrap_refuses_new_db_password_for_existing_volume(sandbox: dict) -> None:
    make_env(sandbox, POSTGRES_PASSWORD="")
    result = bootstrap_fn(sandbox, "fill_secrets", extra_env={"STUB_PG_VOLUME": "1"})
    assert result.returncode != 0
    assert "repeater_pg_data already exists" in result.stderr
    assert read_env(sandbox["deploy"] / ".env")["POSTGRES_PASSWORD"] == ""


def test_bootstrap_rejects_invalid_domain(sandbox: dict) -> None:
    result = bootstrap_fn(
        sandbox,
        "create_env_file; set_domain",
        extra_env={"REPEATER_DOMAIN": "rotator.magic-lap.com; rm -rf /"},
    )
    assert result.returncode != 0
    assert "invalid domain" in result.stderr


def test_env_set_quotes_and_round_trips(sandbox: dict) -> None:
    env_path = make_env(sandbox)
    script = f"""
        env_set "{env_path}" REPEATER_YTDLP_PROXY 'http://user:pa$$word@proxy:8080'
        env_set "{env_path}" REPEATER_TELEGRAM_CHAT_ID '-100123'
        env_set "{env_path}" REPEATER_META_APP_SECRET 'abc def'
        echo "$(env_get REPEATER_YTDLP_PROXY "{env_path}")|$(env_get REPEATER_META_APP_SECRET "{env_path}")"
    """
    result = bootstrap_fn(sandbox, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://user:pa$$word@proxy:8080|abc def"
    values = read_env(env_path)
    assert values["REPEATER_YTDLP_PROXY"] == "'http://user:pa$$word@proxy:8080'"
    assert values["REPEATER_TELEGRAM_CHAT_ID"] == "-100123"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_env_set_replaces_duplicates_and_rejects_quotes(sandbox: dict) -> None:
    env_path = sandbox["deploy"] / ".env"
    env_path.write_text(
        "# REPEATER_META_APP_ID=commented\nREPEATER_META_APP_ID=1\nOTHER=x\nREPEATER_META_APP_ID=2\n"
    )
    ok = bootstrap_fn(sandbox, f'env_set "{env_path}" REPEATER_META_APP_ID 42')
    assert ok.returncode == 0, ok.stderr
    assert env_path.read_text() == "# REPEATER_META_APP_ID=commented\nREPEATER_META_APP_ID=42\nOTHER=x\n"
    bad = bootstrap_fn(sandbox, f'env_set "{env_path}" REPEATER_META_APP_SECRET "it\'s"')
    assert bad.returncode != 0
    assert "single quote" in bad.stderr and "it's" not in bad.stderr


def test_update_env_applies_only_allowed_keys(sandbox: dict) -> None:
    env_path = make_env(sandbox)
    stdin = (
        "REPEATER_TIKTOK_CLIENT_KEY=tt-key-123\r\n"
        "REPEATER_TIKTOK_CLIENT_SECRET=tt-secret-value\n"
        "REPEATER_GOOGLE_CLIENT_ID=\n"
        "ADMIN_EMAIL=ops@magic-lap.com\n"
        "ADMIN_PASSWORD=not-written-anywhere\n"
    )
    result = bootstrap_fn(
        sandbox,
        f'read_stdin_env; apply_updates "{env_path}"; echo "admin=$ADMIN_EMAIL"',
        stdin=stdin,
    )
    assert result.returncode == 0, result.stderr
    values = read_env(env_path)
    assert values["REPEATER_TIKTOK_CLIENT_KEY"] == "tt-key-123"
    assert values["REPEATER_TIKTOK_CLIENT_SECRET"] == "tt-secret-value"
    assert "REPEATER_GOOGLE_CLIENT_ID" not in values
    assert "not-written-anywhere" not in env_path.read_text()
    assert "admin=ops@magic-lap.com" in result.stdout
    assert "updated REPEATER_TIKTOK_CLIENT_SECRET" in result.stdout
    assert "tt-secret-value" not in result.stdout + result.stderr
    again = bootstrap_fn(sandbox, f'read_stdin_env; apply_updates "{env_path}"', stdin=stdin)
    assert "already up to date" in again.stdout


@pytest.mark.parametrize("line", ["REPEATER_SECRET_KEY=x", "POSTGRES_PASSWORD=x", "lower=x", "NOEQUALS"])
def test_update_env_rejects_other_keys(sandbox: dict, line: str) -> None:
    env_path = make_env(sandbox)
    before = env_path.read_text()
    result = bootstrap_fn(sandbox, f'read_stdin_env; apply_updates "{env_path}"', stdin=line + "\n")
    assert result.returncode != 0
    assert env_path.read_text() == before


NGINX_LISTENING = 'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6),("nginx",pid=811,fd=6))'
TRAEFIK = "coolify-proxy|traefik:v3.1|coolify|running|bridge|80 443 8080"
OTHER_APP = "blog|ghost:5|blog|running|bridge|2368"


def write_stub_file(sandbox: dict, name: str, lines: list[str] | str) -> str:
    """A stub input file (one entry per line); returns its path for the environment."""
    text = lines if isinstance(lines, str) else "\n".join(lines)
    path = sandbox["root"] / f"{name}.txt"
    path.write_text(text + ("\n" if text else ""))
    return str(path)


def proxy_env(
    sandbox: dict,
    *,
    ss: str = "",
    containers: list[str] | None = None,
    units: list[str] | None = None,
    busy_ports: str = "",
    **extra: str,
) -> dict[str, str]:
    return {
        "STUB_SS_WEB": write_stub_file(sandbox, "ss", ss),
        "STUB_INSPECT": write_stub_file(sandbox, "inspect", containers or []),
        "STUB_UNITS": write_stub_file(sandbox, "units", units or []),
        "STUB_SS_BUSY": write_stub_file(sandbox, "busy", busy_ports),
        **extra,
    }


# "name|image|project|state|network mode|published host ports" as `docker inspect` is asked for them.
@pytest.mark.parametrize(
    ("ss_output", "containers", "units", "kind"),
    [
        ("", [], [], "none"),
        (NGINX_LISTENING, [], [], "host-nginx"),
        ('LISTEN 0 4096 *:443 *:* users:(("caddy",pid=90,fd=7))', [], [], "host-caddy"),
        ('LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("apache2",pid=5,fd=3))', [], [], "host-apache"),
        (
            'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=7,fd=4))',
            [TRAEFIK],
            [],
            "docker-traefik",
        ),
        ("", ["npm-app-1|jc21/nginx-proxy-manager:latest|npm|running|bridge|80 81 443"], [], "docker-npm"),
        ("", ["web|nginx:1.27|site|running|bridge|443"], [], "docker-nginx"),
        ("", ["repeater-caddy-1|caddy:2.11-alpine|repeater|running|bridge|80 443"], [], "repeater-caddy"),
        ("", ["db|postgres:16|x|running|bridge|"], [], "none"),
        ('LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=7,fd=4))', [], [], "docker-other"),
        ('LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("haproxy",pid=7,fd=4))', [], [], "host-other"),
        # deploy-ops-4: a proxy that is down right now still owns its ports for later.
        ("", ["npm-app-1|jc21/nginx-proxy-manager:latest|npm|exited|bridge|80 81 443"], [], "docker-npm"),
        ("", ["coolify-proxy|traefik:v3.1|coolify|restarting|bridge|80 443"], [], "docker-traefik"),
        ("", ["traefik|traefik:v3|edge|exited|host|"], [], "docker-traefik"),
        ("", ["web|caddy:2|site|created|bridge|443"], [], "docker-caddy"),
        ("", ["repeater-caddy-1|caddy:2.11-alpine|repeater|exited|bridge|80 443"], [], "repeater-caddy"),
        ("", ["tool|busybox|x|exited|host|"], [], "none"),
        ("", [OTHER_APP, "old|ghost:4|blog|exited|bridge|8080"], [], "none"),
        ("", [], ["nginx enabled"], "host-nginx"),
        ("", [], ["apache2 enabled"], "host-apache"),
        ("", [], ["caddy active"], "host-caddy"),
        ("", [], ["traefik enabled"], "host-traefik"),
        ("", [], ["lsws enabled"], "host-other"),
        ("", [], ["nginx disabled"], "none"),
    ],
)
def test_detect_proxy(
    sandbox: dict, ss_output: str, containers: list[str], units: list[str], kind: str
) -> None:
    env = proxy_env(sandbox, ss=ss_output, containers=containers, units=units)
    result = bootstrap_fn(sandbox, "detect_proxy", extra_env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split(" ", 1)[0] == kind


def test_detect_proxy_names_the_state_of_a_stopped_proxy(sandbox: dict) -> None:
    env = proxy_env(
        sandbox, containers=["npm-app-1|jc21/nginx-proxy-manager:latest|npm|exited|bridge|80 443"]
    )
    result = bootstrap_fn(sandbox, "detect_proxy", extra_env=env)
    assert "npm-app-1" in result.stdout and "exited" in result.stdout


def test_detect_proxy_sees_a_coolify_installation_without_its_proxy(sandbox: dict) -> None:
    (sandbox["root"] / "no-coolify").mkdir()
    result = bootstrap_fn(sandbox, "detect_proxy", extra_env=proxy_env(sandbox))
    assert result.stdout.startswith("docker-traefik Coolify installation")


def test_detect_proxy_ignores_a_listening_docker_proxy_of_this_project(sandbox: dict) -> None:
    """The compose project name decides what is Repeater's own Caddy (selftest uses another)."""
    env = proxy_env(
        sandbox, containers=["x-caddy-1|caddy:2.11-alpine|repeater-selftest|running|bridge|80 443"]
    )
    result = bootstrap_fn(
        sandbox, "detect_proxy", extra_env={**env, "COMPOSE_PROJECT_NAME": "repeater-selftest"}
    )
    assert result.stdout.split(" ", 1)[0] == "repeater-caddy"


def _configure(
    sandbox: dict,
    *,
    prelude: str = "",
    create: bool = True,
    **kw,
) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    extra = kw.pop("extra_env", {})
    env = {**proxy_env(sandbox, **kw), **extra}
    steps = "REPEATER_DOMAIN=rotator.magic-lap.com; " + ("create_env_file; set_domain; " if create else "")
    result = bootstrap_fn(
        sandbox,
        f"{steps}{prelude} configure_proxy; "
        'echo "mode=$PROXY_MODE"; if [[ "$PROXY_MODE" == external ]]; then print_proxy_snippet; fi',
        extra_env=env,
    )
    values = read_env(sandbox["deploy"] / ".env") if (sandbox["deploy"] / ".env").exists() else {}
    return result, values


def test_configure_proxy_free_ports_enable_caddy(sandbox: dict) -> None:
    result, values = _configure(sandbox)
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == "caddy"
    assert values["REPEATER_WEB_BIND"] == "127.0.0.1"
    assert "mode=caddy" in result.stdout


def test_configure_proxy_host_nginx_prints_snippet_and_leaves_it_alone(sandbox: dict) -> None:
    result, values = _configure(sandbox, ss=NGINX_LISTENING)
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == ""
    assert values["REPEATER_WEB_BIND"] == "127.0.0.1"
    assert "mode=external" in result.stdout
    assert f"server_name {DOMAIN};" in result.stdout
    assert "proxy_pass http://127.0.0.1:8090;" in result.stdout
    assert "client_max_body_size 600m;" in result.stdout
    log = docker_log(sandbox)
    assert not any(line.startswith(("stop", "rm", "kill", "restart")) for line in log)


def test_proxy_snippet_overwrites_x_forwarded_for(sandbox: dict) -> None:
    """security-2: `$proxy_add_x_forwarded_for` appends to what the client sent, so the leftmost
    entry (the one uvicorn used to trust) was client-chosen. The snippet sets the header instead."""
    result, _ = _configure(sandbox, ss=NGINX_LISTENING)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in result.stdout
    assert "proxy_add_x_forwarded_for" not in result.stdout
    assert "REPEATER_FORWARDED_ALLOW_IPS" in result.stdout
    assert "proxy_add_x_forwarded_for" not in (DEPLOY / "bootstrap.sh").read_text().replace(
        "(not appended", ""
    ), "bootstrap.sh must not print a snippet that appends to X-Forwarded-For"


def test_configure_proxy_container_proxy_binds_docker0(sandbox: dict) -> None:
    result, values = _configure(sandbox, containers=["coolify-proxy|traefik:v3.1|coolify|running|bridge|80"])
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == ""
    assert values["REPEATER_WEB_BIND"] == "172.18.0.1"
    assert 'url: "http://172.18.0.1:8090"' in result.stdout
    assert f"Host(`{DOMAIN}`)" in result.stdout


def test_configure_proxy_moves_off_a_busy_web_port(sandbox: dict) -> None:
    result, values = _configure(sandbox, busy_ports="8090\n8091")
    assert result.returncode == 0, result.stderr
    assert values["REPEATER_WEB_PORT"] == "8092"
    assert "port 8090 is taken by another service; the web container uses 8092" in result.stdout


@pytest.mark.parametrize(
    ("kw", "label"),
    [
        (
            {"containers": ["npm-app-1|jc21/nginx-proxy-manager:latest|npm|exited|bridge|80 81 443"]},
            "stopped NPM",
        ),
        (
            {"containers": ["coolify-proxy|traefik:v3.1|coolify|restarting|bridge|80 443"]},
            "restarting Traefik",
        ),
        ({"units": ["nginx enabled"]}, "enabled nginx that is not listening"),
        ({"units": ["apache2 active"]}, "active apache that is not listening"),
    ],
)
def test_first_run_never_takes_ports_from_a_proxy_that_is_down(sandbox: dict, kw: dict, label: str) -> None:
    """deploy-ops-4: the old detection only saw live listeners and running containers, so a proxy
    that was down during the first bootstrap lost 80/443 to Repeater's Caddy for good."""
    result, values = _configure(sandbox, **kw)
    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert values["COMPOSE_PROFILES"] == "", label
    assert "mode=external" in result.stdout, label


def test_first_run_on_a_shared_host_needs_an_explicit_opt_in_for_caddy(sandbox: dict) -> None:
    """Ports look free, but the host runs other projects' containers: Caddy only on request."""
    result, values = _configure(sandbox, containers=[OTHER_APP])
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == ""
    assert "mode=external" in result.stdout
    assert "REPEATER_PROXY=caddy" in result.stdout + result.stderr
    assert "1 other Docker containers" in result.stdout + result.stderr
    result, values = _configure(
        sandbox_again(sandbox), containers=[OTHER_APP], extra_env={"REPEATER_PROXY": "caddy"}
    )
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == "caddy" and "mode=caddy" in result.stdout


def sandbox_again(sandbox: dict) -> dict:
    """The same sandbox with a fresh deploy/.env (as on a first run)."""
    (sandbox["deploy"] / ".env").unlink(missing_ok=True)
    return sandbox


def test_repeater_proxy_caddy_is_refused_when_another_proxy_owns_the_ports(sandbox: dict) -> None:
    result, _ = _configure(sandbox, ss=NGINX_LISTENING, extra_env={"REPEATER_PROXY": "caddy"})
    assert result.returncode != 0
    assert "REPEATER_PROXY=caddy, but ports 80/443 belong to" in result.stderr
    stopped = ["npm-app-1|jc21/nginx-proxy-manager:latest|npm|exited|bridge|80 443"]
    result, _ = _configure(sandbox_again(sandbox), containers=stopped, extra_env={"REPEATER_PROXY": "caddy"})
    assert result.returncode != 0 and "npm-app-1" in result.stderr


def test_repeater_proxy_external_and_invalid_values(sandbox: dict) -> None:
    result, values = _configure(sandbox, extra_env={"REPEATER_PROXY": "external"})
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == "" and "mode=external" in result.stdout
    result, _ = _configure(sandbox_again(sandbox), extra_env={"REPEATER_PROXY": "traefik"})
    assert result.returncode != 0
    assert "REPEATER_PROXY must be auto, caddy or external" in result.stderr


def test_repeater_proxy_can_come_from_stdin(sandbox: dict) -> None:
    make_env(sandbox)
    result = bootstrap_fn(
        sandbox, 'read_stdin_env; echo "proxy=$REPEATER_PROXY"', stdin="REPEATER_PROXY=caddy\n"
    )
    assert result.returncode == 0, result.stderr
    assert "proxy=caddy" in result.stdout


def test_configure_proxy_rerun_keeps_decision(sandbox: dict) -> None:
    make_env(sandbox, COMPOSE_PROFILES="", REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(sandbox, 'configure_proxy; echo "mode=$PROXY_MODE"', extra_env=proxy_env(sandbox))
    assert result.returncode == 0, result.stderr
    assert "mode=external" in result.stdout
    assert "ports 80/443 are free" in result.stderr
    assert read_env(sandbox["deploy"] / ".env")["COMPOSE_PROFILES"] == ""


def test_configured_host_with_its_own_caddy_passes(sandbox: dict) -> None:
    make_env(sandbox, COMPOSE_PROFILES="caddy", REPEATER_DOMAIN=DOMAIN)
    containers = ["repeater-caddy-1|caddy:2.11-alpine|repeater|running|bridge|80 443"]
    result = bootstrap_fn(
        sandbox,
        'configure_proxy; echo "mode=$PROXY_MODE"',
        extra_env=proxy_env(sandbox, containers=containers),
    )
    assert result.returncode == 0, result.stderr
    assert "mode=caddy" in result.stdout


def test_moved_env_with_caddy_stops_before_deploy_when_another_proxy_owns_80(sandbox: dict) -> None:
    """deploy-ops-missed-2: a .env copied from a server that ran Repeater's Caddy, onto a host whose
    nginx owns 80/443: bootstrap used to warn and then fail halfway inside `compose up`."""
    make_env(sandbox, COMPOSE_PROFILES="caddy", REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(
        sandbox,
        "DOMAIN=x; configure_proxy; echo REACHED-DEPLOY",
        extra_env=proxy_env(sandbox, ss=NGINX_LISTENING),
    )
    assert result.returncode != 0
    assert "REACHED-DEPLOY" not in result.stdout
    assert "COMPOSE_PROFILES=caddy, but ports 80/443 belong to" in result.stderr
    assert "--redetect-proxy" in result.stderr
    assert "server_name" in result.stdout, "the snippet for the existing proxy is printed"
    assert not any(line.split()[0:1] == ["compose"] and " up " in line for line in docker_log(sandbox))


def test_redetect_proxy_fixes_a_moved_env(sandbox: dict) -> None:
    make_env(sandbox, COMPOSE_PROFILES="caddy", REPEATER_WEB_BIND="172.17.0.1", REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(
        sandbox,
        'DOMAIN=x; REDETECT_PROXY=1; configure_proxy; echo "mode=$PROXY_MODE"',
        extra_env=proxy_env(sandbox, ss=NGINX_LISTENING),
    )
    assert result.returncode == 0, result.stderr
    values = read_env(sandbox["deploy"] / ".env")
    assert values["COMPOSE_PROFILES"] == "" and values["REPEATER_WEB_BIND"] == "127.0.0.1"
    assert "mode=external" in result.stdout


def test_web_bind_must_be_an_address_of_this_host(sandbox: dict) -> None:
    """The old server's docker0 was 172.17.0.1; here it is 172.18.0.1: the web port would not bind."""
    make_env(sandbox, COMPOSE_PROFILES="", REPEATER_WEB_BIND="172.17.0.1", REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(sandbox, "DOMAIN=x; configure_proxy", extra_env=proxy_env(sandbox))
    assert result.returncode != 0
    assert "REPEATER_WEB_BIND=172.17.0.1 is not an address of this host" in result.stderr
    assert "172.18.0.1" in result.stderr
    fixed = bootstrap_fn(
        sandbox,
        "DOMAIN=x; REDETECT_PROXY=1; configure_proxy",
        extra_env=proxy_env(sandbox, containers=["coolify-proxy|traefik:v3.1|coolify|running|bridge|80 443"]),
    )
    assert fixed.returncode == 0, fixed.stderr
    assert read_env(sandbox["deploy"] / ".env")["REPEATER_WEB_BIND"] == "172.18.0.1"


@pytest.mark.parametrize("bind", ["0.0.0.0", "198.51.100.7"])  # noqa: S104 - the values under test
def test_web_bind_never_publishes_on_every_interface(sandbox: dict, bind: str) -> None:
    make_env(sandbox, COMPOSE_PROFILES="", REPEATER_WEB_BIND=bind, REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(sandbox, "DOMAIN=x; configure_proxy", extra_env=proxy_env(sandbox))
    assert result.returncode != 0
    assert "REPEATER_WEB_BIND" in result.stderr


def test_busy_web_port_is_fatal_unless_it_is_our_own_container(sandbox: dict) -> None:
    make_env(sandbox, COMPOSE_PROFILES="", REPEATER_DOMAIN=DOMAIN)
    busy = proxy_env(sandbox, busy_ports="8090")
    result = bootstrap_fn(sandbox, "DOMAIN=x; configure_proxy", extra_env=busy)
    assert result.returncode != 0
    assert "REPEATER_WEB_PORT=8090 is used by another service" in result.stderr
    own = bootstrap_fn(
        sandbox, "DOMAIN=x; configure_proxy", extra_env={**busy, "STUB_RUNNING_WEB": "cid-web"}
    )
    assert own.returncode == 0, own.stderr


def test_create_admin_sends_password_on_stdin_only(sandbox: dict) -> None:
    make_env(sandbox)
    password = "correct horse battery staple"
    result = bootstrap_fn(
        sandbox,
        "create_admin",
        extra_env={"ADMIN_EMAIL": "ops@magic-lap.com", "ADMIN_PASSWORD": password},
    )
    assert result.returncode == 0, result.stderr
    assert (sandbox["logs"] / "stdin").read_text() == password + "\n"
    log = "\n".join(docker_log(sandbox))
    assert "create-admin --email ops@magic-lap.com" in log
    assert password not in log
    assert "admin created" in result.stdout
    assert "Password input may be echoed" not in result.stdout


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("ops@magic-lap.com", "short", "at least 10 characters"),
        ("not-an-email", "long enough password", "not an email address"),
    ],
)
def test_create_admin_validates_input(sandbox: dict, email: str, password: str, message: str) -> None:
    make_env(sandbox)
    result = bootstrap_fn(
        sandbox, "create_admin", extra_env={"ADMIN_EMAIL": email, "ADMIN_PASSWORD": password}
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not (sandbox["logs"] / "stdin").exists()


def test_create_admin_reports_cli_failure(sandbox: dict) -> None:
    make_env(sandbox)
    result = bootstrap_fn(
        sandbox,
        "create_admin",
        extra_env={
            "ADMIN_EMAIL": "ops@magic-lap.com",
            "ADMIN_PASSWORD": "long enough password",
            "STUB_ADMIN_RC": "2",
        },
    )
    assert result.returncode != 0
    assert "creating the admin failed" in result.stderr


def test_create_admin_skipped_without_credentials(sandbox: dict) -> None:
    make_env(sandbox)
    result = bootstrap_fn(sandbox, "create_admin")
    assert result.returncode == 0
    assert "--create-admin" in result.stdout
    assert docker_log(sandbox) == []


def test_backup_cron_is_installed_once(sandbox: dict) -> None:
    result = bootstrap_fn(sandbox, "install_backup_cron; install_backup_cron")
    assert result.returncode == 0, result.stderr
    cron = Path(sandbox["env"]["REPEATER_CRON_FILE"]).read_text()
    assert re.search(
        r"^17 3 \* \* \* root REPEATER_BACKUP_DIR=\S+/backups bash \S+/app/deploy/backup\.sh 2>&1 \| logger -t repeater-backup$",
        cron,
        re.M,
    )
    assert result.stdout.count("installed the daily backup cron job") == 1


def test_checklist_lists_platform_urls(sandbox: dict) -> None:
    result = bootstrap_fn(sandbox, f"DOMAIN={DOMAIN}; PROXY_MODE=caddy; print_checklist")
    assert result.returncode == 0, result.stderr
    for path in (
        "/oauth/tiktok/callback",
        "/oauth/google/callback",
        "/oauth/meta/callback",
        "/meta/deauthorize",
        "/meta/data-deletion",
        "/privacy",
        "/terms",
    ):
        assert f"https://{DOMAIN}{path}" in result.stdout
    assert "203.0.113.10" in result.stdout
    assert "DNS only" in result.stdout


def test_bootstrap_modes_require_env_file(sandbox: dict) -> None:
    if os.geteuid() != 0:
        pytest.skip("bootstrap.sh main() requires root")
    result = run(sandbox, ["bash", str(sandbox["deploy"] / "bootstrap.sh"), "--update-env", "--stdin-env"])
    assert result.returncode != 0
    assert "does not exist yet" in result.stderr
    result = run(sandbox, ["bash", str(sandbox["deploy"] / "bootstrap.sh"), "--update-env"])
    assert "add --stdin-env" in result.stderr
    result = run(sandbox, ["bash", str(sandbox["deploy"] / "bootstrap.sh"), "--bogus"])
    assert "unknown option" in result.stderr


# --------------------------------------------------------------------------- deploy.sh


def deploy(sandbox: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
    return run(sandbox, ["bash", str(sandbox["deploy"] / "deploy.sh"), *args], extra_env=env)


def compose_calls(sandbox: dict) -> list[str]:
    calls = []
    for line in docker_log(sandbox):
        parts = line.split()
        # "compose --project-directory X -f Y --env-file Z <subcommand ...>"
        if len(parts) > 7 and parts[:2] == ["compose", "--project-directory"]:
            calls.append(" ".join(parts[7:]))
    return calls


def test_deploy_requires_env_file(sandbox: dict) -> None:
    result = deploy(sandbox)
    assert result.returncode != 0
    assert "run deploy/bootstrap.sh first" in result.stderr


def test_no_build_refuses_a_missing_release_before_any_migration(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, "--no-build", REPEATER_IMAGE_TAG="build-missing")
    assert result.returncode != 0 and "does not exist" in result.stderr
    assert not any(c.startswith(("build", "run", "up")) for c in compose_calls(sandbox))


def test_first_deploy_does_not_offer_a_nonexistent_rollback(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox)
    assert result.returncode == 0, result.stderr
    assert "Roll back with:" not in result.stdout


def test_proxy_trust_mismatch_stops_the_deploy(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_PROXY_CHECK_RC="1")
    assert result.returncode != 0 and "trusted proxy" in result.stderr
    assert any("check-proxy 172.18.0.1" in call for call in compose_calls(sandbox))
    assert "deploy complete." not in result.stdout


def test_deploy_rejects_missing_secrets(sandbox: dict) -> None:
    make_env(sandbox, REPEATER_SECRET_KEY="", REPEATER_TOKEN_ENCRYPTION_KEYS="")
    result = deploy(sandbox)
    assert result.returncode != 0
    assert "REPEATER_SECRET_KEY REPEATER_TOKEN_ENCRYPTION_KEYS" in result.stderr


def test_deploy_rejects_url_unsafe_db_password(sandbox: dict) -> None:
    make_env(sandbox, POSTGRES_PASSWORD="'p@ss/word'")
    result = deploy(sandbox)
    assert result.returncode != 0
    assert "URL-safe" in result.stderr


def test_deploy_happy_path_order(sandbox: dict) -> None:
    env_path = make_env(sandbox)
    before = env_path.read_text()
    result = deploy(sandbox)
    assert result.returncode == 0, result.stderr
    calls = compose_calls(sandbox)
    steps = [c.split()[0] for c in calls]
    assert steps.index("config") < steps.index("build") < steps.index("run") < steps.index("up")
    assert "pull --ignore-buildable" not in calls, "images are only pulled with --pull"
    assert "build migrate web worker" in calls
    assert "run --rm --no-TTY migrate" in calls
    assert "up -d --remove-orphans" in calls
    assert "web and worker are healthy" in result.stdout
    assert env_path.read_text() == before, "deploy.sh must not modify deploy/.env"
    assert (sandbox["root"] / "media").is_dir()
    env_log = (sandbox["logs"] / "docker-env.log").read_text()
    assert re.search(r"YTDLP_REFRESH=\d{8} REPEATER_IMAGE_TAG=latest", env_log)
    assert any(
        line.startswith("image prune -f --filter label=org.opencontainers.image.title=repeater")
        for line in docker_log(sandbox)
    )


def test_deploy_options(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, "--pull", "--refresh-ytdlp")
    assert result.returncode == 0, result.stderr
    assert "build --pull migrate web worker" in compose_calls(sandbox)
    assert re.search(r"YTDLP_REFRESH=\d{14} ", (sandbox["logs"] / "docker-env.log").read_text())


def test_deploy_pull_refreshes_the_images_that_are_not_built(sandbox: dict) -> None:
    """deploy-ops-5: `build --pull` only refreshes the Python base image; postgres and caddy have no
    build section and compose never pulls an image it already has, so --pull must pull them."""
    make_env(sandbox)
    result = deploy(sandbox, "--pull")
    assert result.returncode == 0, result.stderr
    calls = compose_calls(sandbox)
    steps = [c.split()[0] for c in calls]
    assert "pull --ignore-buildable" in calls
    assert calls.index("pull --ignore-buildable") < steps.index("run") < steps.index("up")
    # Also without a build: the monthly security update of postgres/caddy must not need one.
    again = deploy(sandbox, "--pull", "--no-build")
    assert again.returncode == 0, again.stderr
    assert compose_calls(sandbox).count("pull --ignore-buildable") == 2


def test_deploy_pull_failure_changes_nothing(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, "--pull", STUB_PULL_RC="1")
    assert result.returncode != 0
    assert "could not pull the newer images" in result.stderr
    assert not any(c.split()[0] in ("build", "run", "up") for c in compose_calls(sandbox))


def stored_images(sandbox: dict) -> dict[str, str]:
    """Tag -> image id in the docker stub's image store."""
    store = sandbox["root"] / "state" / "images"
    return {
        f.name.replace("repeater-app_", "", 1): f.read_text().strip() for f in store.glob("repeater-app_*")
    }


def seed_image(sandbox: dict, tag: str, image_id: str) -> None:
    store = sandbox["root"] / "state" / "images"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"repeater-app_{tag}").write_text(image_id)


@pytest.mark.parametrize(
    "builds",
    [
        # overlay2 image store: an identical rebuild keeps the image id
        ["sha256:v2", "sha256:v2", "sha256:v2"],
        # containerd image store: every build gets a new id (the manifest carries build provenance),
        # the layers are the same ("#n" marks such a build in the docker stub)
        ["sha256:v2#1", "sha256:v2#2", "sha256:v2#3"],
    ],
)
def test_previous_image_survives_a_rerun_that_builds_the_same_image(sandbox: dict, builds: list[str]) -> None:
    """deploy-ops-9: re-running Deploy (a retry, a credentials update, a docs-only push) rebuilds the
    identical image from cache; tagging :latest as :previous first made the rollback target the
    broken release itself. Comparing image ids is not enough: Docker's containerd image store gives
    every build a new id even when all layers are cached (seen on Docker 29), so content is compared."""
    make_env(sandbox)
    seed_image(sandbox, "latest", "sha256:v1")
    first = deploy(sandbox, STUB_BUILD_ID=builds[0])
    assert first.returncode == 0, first.stderr
    images = stored_images(sandbox)
    assert images["latest"] == builds[0] and images["previous"] == "sha256:v1"
    second = deploy(sandbox, STUB_BUILD_ID=builds[1])
    assert second.returncode == 0, second.stderr
    third = deploy(sandbox, STUB_BUILD_ID=builds[2])
    assert third.returncode == 0, third.stderr
    images = stored_images(sandbox)
    assert images["latest"] == builds[2]
    assert images["previous"] == "sha256:v1", "a same-content rebuild must not move :previous"
    assert "left as it is" in second.stdout
    assert "REPEATER_IMAGE_TAG=previous bash" in third.stdout, "the rollback command is printed"
    builds_tagged = [t for t in images if t.startswith("build-")]
    assert len(builds_tagged) == 1, "only the first (new) release got a dated tag"
    assert "rollback-candidate" not in images, "the temporary pin is removed again"


def test_the_old_release_is_pinned_before_the_build_so_it_cannot_be_collected(sandbox: dict) -> None:
    """Found on real Docker 29: an image that loses its last tag is deleted at once by the containerd
    image store, so tagging the old id AFTER the build failed ("No such image") and aborted the
    deploy with the new image built but not started."""
    make_env(sandbox)
    seed_image(sandbox, "latest", "sha256:v1")
    result = deploy(sandbox, STUB_BUILD_ID="sha256:v2")
    assert result.returncode == 0, result.stderr
    assert stored_images(sandbox)["previous"] == "sha256:v1"
    calls = docker_log(sandbox)
    pin = calls.index("image tag sha256:v1 repeater-app:rollback-candidate")
    build = next(i for i, c in enumerate(calls) if " build " in c)
    keep = calls.index("image tag repeater-app:rollback-candidate repeater-app:previous")
    drop = calls.index("rmi repeater-app:rollback-candidate")
    assert pin < build < keep < drop
    assert "up -d --remove-orphans" in compose_calls(sandbox), "the deploy went on to start the release"


def test_previous_image_moves_when_a_new_release_is_built(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "latest", "sha256:v1")
    assert deploy(sandbox, STUB_BUILD_ID="sha256:v2").returncode == 0
    assert deploy(sandbox, STUB_BUILD_ID="sha256:v3").returncode == 0
    images = stored_images(sandbox)
    assert (images["latest"], images["previous"]) == ("sha256:v3", "sha256:v2")


def test_a_failed_build_leaves_previous_alone(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "latest", "sha256:v1")
    seed_image(sandbox, "previous", "sha256:v0")
    assert deploy(sandbox, STUB_BUILD_RC="1").returncode != 0
    images = stored_images(sandbox)
    assert (images["latest"], images["previous"]) == ("sha256:v1", "sha256:v0")


def test_dated_release_tags_keep_the_newest_four(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "latest", "sha256:v1")
    for stamp in range(1, 7):
        seed_image(sandbox, f"build-2026010{stamp}000000", f"sha256:old{stamp}")
    assert deploy(sandbox, STUB_BUILD_ID="sha256:v2").returncode == 0
    builds = sorted(tag for tag in stored_images(sandbox) if tag.startswith("build-"))
    assert len(builds) == 4
    assert builds[:3] == ["build-20260104000000", "build-20260105000000", "build-20260106000000"]
    assert stored_images(sandbox)[builds[-1]] == "sha256:v2", "the newest tag is the release just built"


def test_deploy_takes_a_predeploy_backup_before_migrating(sandbox: dict) -> None:
    """deploy-ops-2: the documented rollback needs a dump taken right before the migration (the
    daily one can be 24 h old)."""
    make_env(sandbox)
    result = deploy(sandbox)
    assert result.returncode == 0, result.stderr
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    dumps = list(backups.glob("repeater-predeploy-*.sql.gz"))
    assert len(dumps) == 1
    exec_log = (sandbox["logs"] / "exec.log").read_text()
    assert "pg_dump -U repeater -d repeater" in exec_log
    steps = [c.split()[0] for c in compose_calls(sandbox)]
    # `exec` (the dump) happens after the build and before `run` (the migration).
    assert steps.index("build") < steps.index("exec") < steps.index("run")
    assert "pre-deploy backup finished" in result.stdout


def test_predeploy_backup_failure_stops_before_the_migration(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_PGDUMP_FAIL="1")
    assert result.returncode != 0
    assert "the pre-deploy backup failed, so nothing was migrated" in result.stderr
    assert not any(c.split()[0] in ("run", "up") for c in compose_calls(sandbox))
    skipped = deploy(sandbox, STUB_PGDUMP_FAIL="1", SKIP_PREDEPLOY_BACKUP="1")
    assert skipped.returncode == 0, skipped.stderr
    assert "migrating without a pre-deploy database dump" in skipped.stderr


def test_first_install_has_no_database_to_back_up(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_RUNNING_DB="")
    assert result.returncode == 0, result.stderr
    assert "first install" in result.stdout
    assert (
        not (sandbox["logs"] / "exec.log").exists()
        or "pg_dump" not in (sandbox["logs"] / "exec.log").read_text()
    )


def test_stopped_database_with_data_is_started_for_the_backup(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_RUNNING_DB="", STUB_PG_VOLUME="1")
    assert result.returncode == 0, result.stderr
    assert "up -d --wait db" in compose_calls(sandbox)
    assert "pg_dump" in (sandbox["logs"] / "exec.log").read_text()


def test_predeploy_dumps_survive_retries_until_the_age_limit(sandbox: dict) -> None:
    make_env(sandbox)
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    for day in range(1, 9):
        _old_dump(backups, f"repeater-predeploy-2026010{day}T000000Z.sql.gz", 30 - day)
    daily = _old_dump(backups, "repeater-db-20260101T000000Z.sql.gz", 10)
    assert deploy(sandbox).returncode == 0
    kept = sorted(p.name for p in backups.glob("repeater-predeploy-*.sql.gz"))
    assert len(kept) == 9 and kept[0] == "repeater-predeploy-20260101T000000Z.sql.gz"
    assert daily.exists(), "the daily dumps follow their own retention"


def test_failed_rollbacks_do_not_evict_or_overwrite_pre_migration_dumps(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "previous", "sha256:old")
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    original = _old_dump(backups, "repeater-predeploy-20261001T000000Z.sql.gz", 2)
    for _ in range(6):
        result = deploy(sandbox, "--no-build", REPEATER_IMAGE_TAG="previous", STUB_MIGRATE_RC="1")
        assert result.returncode != 0
    assert original.exists()
    assert len(list(backups.glob("repeater-predeploy-*.sql.gz"))) == 7


def test_migration_failure_on_a_rollback_points_at_the_restore(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "previous", "sha256:old")
    result = deploy(sandbox, "--no-build", STUB_MIGRATE_RC="1", REPEATER_IMAGE_TAG="previous")
    assert result.returncode != 0
    assert "REPEATER_IMAGE_TAG=previous bash" in result.stderr and "backup.sh restore" in result.stderr


def test_deploy_hands_compose_the_worker_stop_grace(sandbox: dict) -> None:
    """deploy-ops-6: stop_grace_period = REPEATER_WORKER_SHUTDOWN_GRACE_S + 60 s."""
    make_env(sandbox)
    assert deploy(sandbox).returncode == 0
    assert "WORKER_STOP_GRACE_S=660" in (sandbox["logs"] / "docker-env.log").read_text()
    make_env(sandbox, REPEATER_WORKER_SHUTDOWN_GRACE_S="900")
    (sandbox["logs"] / "docker-env.log").unlink()
    assert deploy(sandbox).returncode == 0
    assert "WORKER_STOP_GRACE_S=960" in (sandbox["logs"] / "docker-env.log").read_text()
    make_env(sandbox, REPEATER_WORKER_SHUTDOWN_GRACE_S="soon")
    (sandbox["logs"] / "docker-env.log").unlink()
    assert deploy(sandbox).returncode == 0
    assert "WORKER_STOP_GRACE_S=660" in (sandbox["logs"] / "docker-env.log").read_text()


def test_deploy_no_build_uses_requested_tag(sandbox: dict) -> None:
    make_env(sandbox)
    seed_image(sandbox, "previous", "sha256:old")
    result = deploy(sandbox, "--no-build", REPEATER_IMAGE_TAG="previous")
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("build") for c in compose_calls(sandbox))
    assert "REPEATER_IMAGE_TAG=previous" in (sandbox["logs"] / "docker-env.log").read_text()


def test_deploy_stops_when_migrations_fail(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_MIGRATE_RC="1")
    assert result.returncode != 0
    assert "migrations failed" in result.stderr
    assert not any(c.startswith("up") for c in compose_calls(sandbox))


def test_deploy_fails_when_unhealthy(sandbox: dict) -> None:
    make_env(sandbox)
    result = deploy(sandbox, STUB_HEALTH="unhealthy")
    assert result.returncode != 0
    assert "not healthy" in result.stderr
    assert any(c.startswith("logs --no-color --tail 100") for c in compose_calls(sandbox))


def test_deploy_recreates_caddy_when_caddyfile_changed(sandbox: dict) -> None:
    make_env(sandbox)
    old = sandbox["root"] / "old-Caddyfile"
    old.write_text("old config\n")
    result = deploy(sandbox, STUB_CADDYFILE=str(old))
    assert result.returncode == 0, result.stderr
    assert "up -d --no-deps --force-recreate caddy" in compose_calls(sandbox)


def test_deploy_rejects_unknown_option(sandbox: dict) -> None:
    result = deploy(sandbox, "--yolo")
    assert result.returncode != 0
    assert "unknown option" in result.stderr


# --------------------------------------------------------------------------- backup.sh


def backup(sandbox: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
    return run(sandbox, ["bash", str(sandbox["deploy"] / "backup.sh"), *args], extra_env=env)


def _old_dump(directory: Path, name: str, days: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(gzip.compress(b"-- PostgreSQL database dump\n"))
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_backup_writes_dump_and_prunes(sandbox: dict) -> None:
    make_env(sandbox)
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    stale = _old_dump(backups, "repeater-db-20260901T031700Z.sql.gz", 20)
    recent = _old_dump(backups, "repeater-db-20261010T031700Z.sql.gz", 5)
    unrelated = _old_dump(backups, "notes.sql.gz", 40)
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    dumps = sorted(backups.glob("repeater-db-*.sql.gz"))
    assert stale not in dumps and recent in dumps and unrelated.exists()
    new = [p for p in dumps if p != recent]
    assert len(new) == 1
    assert gzip.decompress(new[0].read_bytes()).startswith(b"--\n-- PostgreSQL database dump")
    assert stat.S_IMODE(new[0].stat().st_mode) == 0o600
    assert not list(backups.glob(".repeater-db-*")), "temporary file left behind"
    assert (
        "pg_dump -U repeater -d repeater --no-owner --no-privileges --clean --if-exists"
        in (sandbox["logs"] / "exec.log").read_text()
    )
    assert not (sandbox["logs"] / "rclone.log").exists()


def test_safety_dumps_expire_by_age_so_deleted_data_leaves_the_backups(sandbox: dict) -> None:
    """The privacy policy promises deleted data is gone from backups within 30 days: the pre-deploy
    and pre-restore dumps (kept by count) expire by age too, in the daily run and in a deploy."""
    make_env(sandbox)
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    old_deploy = _old_dump(backups, "repeater-predeploy-20260801T000000Z.sql.gz", 45)
    old_restore = _old_dump(backups, "repeater-prerestore-20260801T000000Z.sql.gz", 45)
    fresh_deploy = _old_dump(backups, "repeater-predeploy-20261001T000000Z.sql.gz", 12)
    fresh_restore = _old_dump(backups, "repeater-prerestore-20261001T000000Z.sql.gz", 12)
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    assert not old_deploy.exists() and not old_restore.exists()
    assert fresh_deploy.exists() and fresh_restore.exists()
    again = _old_dump(backups, "repeater-predeploy-20260802T000000Z.sql.gz", 50)
    assert deploy(sandbox).returncode == 0
    assert not again.exists() and fresh_deploy.exists()


def test_backup_failure_keeps_old_dumps(sandbox: dict) -> None:
    make_env(sandbox)
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    stale = _old_dump(backups, "repeater-db-20260901T031700Z.sql.gz", 20)
    result = backup(sandbox, STUB_PGDUMP_FAIL="1")
    assert result.returncode != 0
    assert "pg_dump failed" in result.stderr
    assert stale.exists()
    assert sorted(p.name for p in backups.iterdir() if not p.name.startswith(".backup")) == [stale.name]


def test_backup_requires_running_db(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, STUB_RUNNING_DB="")
    assert result.returncode != 0
    assert "db container is not running" in result.stderr


def test_backup_rejects_bad_retention(sandbox: dict) -> None:
    make_env(sandbox, BACKUP_RETENTION_DAYS="two weeks")
    result = backup(sandbox)
    assert result.returncode != 0
    assert "BACKUP_RETENTION_DAYS" in result.stderr


def rclone_calls(sandbox: dict) -> list[str]:
    path = sandbox["logs"] / "rclone.log"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize(("media_mode", "media_synced"), [("daily", True), ("off", False)])
def test_backup_offsite_copy(sandbox: dict, media_mode: str, media_synced: bool) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:magiclab-repeater", RCLONE_MEDIA_SYNC=media_mode)
    (sandbox["root"] / "media").mkdir()
    (sandbox["root"] / "media" / "a.mp4").write_text("v")
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    calls = rclone_calls(sandbox)
    # deploy-ops-3: the dumps are copied, never mirrored: what the local disk lacks stays off-site.
    dump_copy = next(c for c in calls if c.startswith("copy ") and "r2:magiclab-repeater/db" in c)
    assert "--include repeater-db-*.sql.gz" in dump_copy
    assert not any(c.startswith("sync ") and "/db" in c for c in calls)
    assert (
        any(c.startswith("delete r2:magiclab-repeater/db") and "--min-age 30d" in c for c in calls) is False
    ), "expiry only runs when a recent remote copy was listed (the stub lists nothing)"
    media_calls = [c for c in calls if c.startswith("sync ")]
    assert bool(media_calls) is media_synced
    if media_synced:
        media_call = media_calls[0]
        assert "r2:magiclab-repeater/media " in media_call + " "
        assert "--exclude uploads/**" in media_call and "--exclude .cookies/**" in media_call
        assert "--timeout 5m" in media_call
        assert re.search(r"--backup-dir r2:magiclab-repeater/media-trash/\d{8} ", media_call)
        assert "--max-delete 500" in media_call


def test_backup_offsite_media_limits_are_configurable(sandbox: dict) -> None:
    make_env(
        sandbox,
        RCLONE_REMOTE="r2:x",
        RCLONE_MEDIA_SYNC="daily",
        RCLONE_MAX_DELETE="25",
        BACKUP_REMOTE_RETENTION_DAYS="45",
    )
    (sandbox["root"] / "media").mkdir()
    (sandbox["root"] / "media" / "a.mp4").write_text("v")
    result = backup(sandbox, STUB_RCLONE_LSF="repeater-db-20261001T000000Z.sql.gz")
    assert result.returncode == 0, result.stderr
    calls = rclone_calls(sandbox)
    assert any(c.startswith("sync ") and "--max-delete 25" in c for c in calls)
    assert any(c.startswith("delete r2:x/db") and "--min-age 45d" in c for c in calls)


@pytest.mark.parametrize("key", ["RCLONE_MAX_DELETE", "RCLONE_TRASH_DAYS", "BACKUP_REMOTE_RETENTION_DAYS"])
def test_backup_offsite_numbers_are_validated(sandbox: dict, key: str) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:x", RCLONE_MEDIA_SYNC="daily", **{key: "lots"})
    (sandbox["root"] / "media").mkdir()
    (sandbox["root"] / "media" / "a.mp4").write_text("v")
    result = backup(sandbox)
    assert result.returncode != 0 and key in result.stderr


def test_backup_offsite_failure_is_reported(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:magiclab-repeater")
    result = backup(sandbox, STUB_RCLONE_RC="1")
    assert result.returncode != 0
    assert "rclone copy of the database dumps failed" in result.stderr


def test_backup_never_syncs_an_empty_media_dir_over_a_full_remote(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:x", RCLONE_MEDIA_SYNC="daily")
    (sandbox["root"] / "media").mkdir()
    result = backup(sandbox, STUB_RCLONE_LSF="media/a.mp4")
    assert result.returncode != 0
    assert "local media directory is empty" in result.stderr
    assert not any(c.startswith("sync ") for c in rclone_calls(sandbox))


def test_empty_library_requires_a_successful_remote_inventory(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="crypt:x", RCLONE_MEDIA_SYNC="daily")
    (sandbox["root"] / "media").mkdir()
    result = backup(sandbox, STUB_RCLONE_FAIL_ON="lsf")
    assert result.returncode != 0 and "inventory could not be verified" in result.stderr
    assert not any(c.startswith("sync ") for c in rclone_calls(sandbox))
    folder = Path(sandbox["env"]["REPEATER_BACKUP_DIR"]) / "status"
    assert (folder / "last_offsite_db_success").exists()
    assert not (folder / "last_offsite_media_success").exists()


# ----- the same off-site rules against rclone itself (its local backend; skipped without rclone)


def _real_rclone() -> str | None:
    return os.environ.get("REPEATER_TEST_RCLONE") or shutil.which("rclone")


@pytest.fixture
def real_rclone(sandbox: dict) -> dict:
    binary = _real_rclone()
    if binary is None:
        pytest.skip("rclone is not installed (set REPEATER_TEST_RCLONE or apt-get install rclone)")
    stub = sandbox["root"] / "bin" / "rclone"
    stub.unlink()
    stub.symlink_to(binary)
    sandbox["env"]["RCLONE_CONFIG"] = os.devnull
    remote = sandbox["root"] / "remote"
    (remote / "db").mkdir(parents=True)
    (remote / "media").mkdir()
    sandbox["remote"] = remote
    (sandbox["root"] / "media").mkdir()
    make_env(sandbox, RCLONE_REMOTE=str(remote), RCLONE_MEDIA_SYNC="daily")
    return sandbox


def _put(path: Path, text: str = "x", days_old: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if days_old:
        stamp = time.time() - days_old * 86400
        os.utime(path, (stamp, stamp))
    return path


def _files(root: Path) -> set[str]:
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()}


def test_offsite_dumps_missing_locally_are_never_deleted(real_rclone: dict) -> None:
    """deploy-ops-3: `rclone sync` made a freshly restored server (no local history) delete the
    whole off-site dump history at its first backup."""
    remote = real_rclone["remote"]
    _put(remote / "db" / "repeater-db-20261001T031700Z.sql.gz", "old", days_old=5)
    result = backup(real_rclone)
    assert result.returncode == 0, result.stderr
    names = _files(remote / "db")
    assert "repeater-db-20261001T031700Z.sql.gz" in names
    assert len(names) == 2, names


def test_offsite_dumps_expire_by_age(real_rclone: dict) -> None:
    remote = real_rclone["remote"]
    _put(remote / "db" / "repeater-db-20260801T031700Z.sql.gz", "ancient", days_old=40)
    _put(remote / "db" / "repeater-db-20261001T031700Z.sql.gz", "recent", days_old=5)
    _put(remote / "db" / "keep-me.txt", "not a dump", days_old=400)
    assert backup(real_rclone).returncode == 0
    assert _files(remote / "db") - {"keep-me.txt"} != set()
    assert "repeater-db-20260801T031700Z.sql.gz" not in _files(remote / "db")
    assert "repeater-db-20261001T031700Z.sql.gz" in _files(remote / "db")
    assert "keep-me.txt" in _files(remote / "db"), "only repeater-db-*.sql.gz files are ever expired"


def test_offsite_expiry_waits_for_a_recent_copy(real_rclone: dict) -> None:
    """If the copy cannot produce anything newer (here: the local dump dir is unreadable for rclone
    because the dump failed) nothing may age out, or one outage would empty the history."""
    remote = real_rclone["remote"]
    _put(remote / "db" / "repeater-db-20260801T031700Z.sql.gz", "ancient", days_old=40)
    result = backup(real_rclone, STUB_PGDUMP_FAIL="1")
    assert result.returncode != 0
    assert "repeater-db-20260801T031700Z.sql.gz" in _files(remote / "db")


def test_offsite_never_wipes_the_library_from_an_empty_media_dir(real_rclone: dict) -> None:
    remote = real_rclone["remote"]
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _put(remote / "media" / name)
    result = backup(real_rclone)
    assert result.returncode != 0
    assert "local media directory is empty" in result.stderr
    assert _files(remote / "media") == {"a.mp4", "b.mp4", "c.mp4"}
    assert not (Path(real_rclone["env"]["REPEATER_BACKUP_DIR"]) / "status" / "last_success").exists()


def test_offsite_media_deletions_are_capped_and_land_in_the_trash(real_rclone: dict) -> None:
    """A half-restored server (1 of 6 files copied back) deleted the other 5 off-site; now at most
    RCLONE_MAX_DELETE go, and they go to media-trash/<date>, not nowhere."""
    remote = real_rclone["remote"]
    for i in range(1, 7):
        _put(remote / "media" / f"f{i}.mp4", f"video {i}")
    _put(real_rclone["root"] / "media" / "f1.mp4", "video 1")
    make_env(real_rclone, RCLONE_REMOTE=str(remote), RCLONE_MEDIA_SYNC="daily", RCLONE_MAX_DELETE="2")
    result = backup(real_rclone)
    assert result.returncode != 0
    assert "RCLONE_MAX_DELETE" in result.stderr
    live = _files(remote / "media")
    assert len(live) >= 4, f"at most two deletions: {live}"
    trashed = {name for name in _files(remote / "media-trash")}
    assert len(trashed) <= 2 and trashed, trashed
    assert {Path(t).name for t in trashed} <= {f"f{i}.mp4" for i in range(2, 7)}


def test_offsite_media_mirror_uses_trash_and_expires_it_by_date(real_rclone: dict) -> None:
    remote = real_rclone["remote"]
    _put(remote / "media" / "keep.mp4", "same")
    _put(remote / "media" / "gone.mp4", "deleted locally")
    _put(remote / "media-trash" / "20200101" / "ancient.mp4", "very old trash")
    recent = time.strftime("%Y%m%d", time.gmtime(time.time() - 2 * 86400))
    _put(remote / "media-trash" / recent / "recent.mp4", "recent trash")
    _put(real_rclone["root"] / "media" / "keep.mp4", "same")
    _put(real_rclone["root"] / "media" / "new.mp4", "new")
    _put(real_rclone["root"] / "media" / "uploads" / "chunk.tmp", "staging")
    _put(real_rclone["root"] / "media" / ".cookies" / "source-1.txt", "cookies")
    result = backup(real_rclone)
    assert result.returncode == 0, result.stderr
    assert _files(remote / "media") == {"keep.mp4", "new.mp4"}, "mirrored; uploads/ and .cookies/ stay local"
    trash = _files(remote / "media-trash")
    assert any(t.endswith("/gone.mp4") for t in trash), "the file deleted locally moved to the trash"
    assert not any("ancient.mp4" in t for t in trash), "trash older than RCLONE_TRASH_DAYS (7) is purged"
    assert f"{recent}/recent.mp4" in trash, "newer trash is kept"
    assert (Path(real_rclone["env"]["REPEATER_BACKUP_DIR"]) / "status" / "last_success").is_file()


# ----- alerts and the last-success marker (deploy-ops-missed-3)

TELEGRAM = {"REPEATER_TELEGRAM_BOT_TOKEN": "123456:ABC-def_ghi", "REPEATER_TELEGRAM_CHAT_ID": "-100123"}


def status_file(sandbox: dict) -> Path:
    return Path(sandbox["env"]["REPEATER_BACKUP_DIR"]) / "status" / "last_success"


def test_backup_success_records_the_marker_and_sends_nothing(sandbox: dict) -> None:
    make_env(sandbox, **TELEGRAM)
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    epoch, stamp = status_file(sandbox).read_text().split()
    assert abs(int(epoch) - time.time()) < 120 and stamp.endswith("Z")
    assert not (sandbox["logs"] / "curl-args.log").exists(), "no alert on success"
    assert stat.S_IMODE(status_file(sandbox).stat().st_mode) == 0o644


def test_backup_failure_alerts_telegram_without_leaking_the_token(sandbox: dict) -> None:
    """The cron job's only trace used to be one journald line."""
    make_env(sandbox, **TELEGRAM)
    result = backup(sandbox, STUB_RUNNING_DB="")
    assert result.returncode != 0
    config = (sandbox["logs"] / "curl-stdin.log").read_text()
    assert 'url = "https://api.telegram.org/bot123456:ABC-def_ghi/sendMessage"' in config
    assert 'data-urlencode = "chat_id=-100123"' in config
    assert "Repeater backup FAILED" in config and "db container is not running" in config
    # The token travels on curl's stdin (--config -), never on a command line (`ps`) or in output.
    assert "123456:ABC" not in (sandbox["logs"] / "curl-args.log").read_text()
    assert "123456:ABC" not in result.stdout + result.stderr
    assert not status_file(sandbox).exists(), "a failed run must not look like a success"


def test_backup_failure_without_telegram_settings_just_fails(sandbox: dict) -> None:
    make_env(sandbox)
    assert backup(sandbox, STUB_RUNNING_DB="").returncode != 0
    assert not (sandbox["logs"] / "curl-args.log").exists()


def test_failed_alert_does_not_hide_the_backup_failure(sandbox: dict) -> None:
    make_env(sandbox, **TELEGRAM)
    result = backup(sandbox, STUB_RUNNING_DB="", STUB_CURL_RC="22")
    assert result.returncode != 0
    assert "db container is not running" in result.stderr
    assert "alert could not be delivered" in result.stderr


def test_backup_alerts_when_the_offsite_copy_fails_and_keeps_the_marker_unchanged(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:x", **TELEGRAM)
    result = backup(sandbox, STUB_RCLONE_RC="1")
    assert result.returncode != 0
    assert "rclone copy of the database dumps failed" in (sandbox["logs"] / "curl-stdin.log").read_text()
    assert not status_file(sandbox).exists()


@pytest.mark.parametrize("failure", [{"STUB_RCLONE_CORRUPT": "1"}, {"STUB_RCLONE_FAIL_ON": "cat"}])
def test_offsite_readback_failure_preserves_previous_success_and_remote_history(
    sandbox: dict, failure
) -> None:
    make_env(sandbox, RCLONE_REMOTE="crypt:repeater", RCLONE_MEDIA_SYNC="off")
    _write_status(sandbox, 40)
    previous = status_file(sandbox).read_text()
    result = backup(sandbox, **failure)
    assert result.returncode != 0
    assert status_file(sandbox).read_text() == previous
    assert (status_file(sandbox).parent / "last_local_success").exists()
    assert not (status_file(sandbox).parent / "last_offsite_db_success").exists()
    assert "delete " not in (sandbox["logs"] / "rclone.log").read_text()


def test_offsite_phase_receipts_and_dedicated_config(sandbox: dict) -> None:
    config = sandbox["root"] / "rclone.conf"
    config.write_text("[test]\ntype=local\n")
    make_env(sandbox, RCLONE_REMOTE="crypt:repeater", RCLONE_MEDIA_SYNC="off", RCLONE_CONFIG=str(config))
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    folder = status_file(sandbox).parent
    assert (folder / "last_local_success").exists()
    assert (folder / "last_offsite_db_success").exists()
    assert not (folder / "last_offsite_media_success").exists()
    assert "read-back SHA-256 verified" in result.stdout
    assert set((sandbox["logs"] / "rclone.log.config").read_text().splitlines()) == {str(config)}


def test_media_readback_failure_does_not_record_a_successful_media_backup(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="crypt:repeater", RCLONE_MEDIA_SYNC="daily")
    _write_status(sandbox, 40)
    previous = status_file(sandbox).read_text()
    result = backup(sandbox, STUB_RCLONE_FAIL_ON="check")
    assert result.returncode != 0 and "media library failed read-back" in result.stderr
    folder = status_file(sandbox).parent
    assert status_file(sandbox).read_text() == previous
    assert (folder / "last_offsite_db_success").exists()
    assert not (folder / "last_offsite_media_success").exists()


def test_missing_dedicated_rclone_config_fails_before_upload(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="crypt:x", RCLONE_CONFIG="/no/such/config")
    result = backup(sandbox)
    assert result.returncode != 0 and "not readable" in result.stderr
    assert not (sandbox["logs"] / "rclone.log").exists()


def _write_status(sandbox: dict, hours_ago: float) -> None:
    path = status_file(sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(time.time() - hours_ago * 3600)} x\n")


def test_status_reports_fresh_and_stale_backups(sandbox: dict) -> None:
    make_env(sandbox, **TELEGRAM)
    _write_status(sandbox, 3)
    fresh = backup(sandbox, "status", "--alert")
    assert fresh.returncode == 0, fresh.stderr
    assert not (sandbox["logs"] / "curl-args.log").exists()
    _write_status(sandbox, 40)
    stale = backup(sandbox, "status")
    assert stale.returncode == 1
    assert "no successful backup for 40 hours" in stale.stderr
    assert not (sandbox["logs"] / "curl-args.log").exists(), "plain status does not alert"
    alerted = backup(sandbox, "status", "--alert")
    assert alerted.returncode == 1
    assert "Repeater backup is STALE" in (sandbox["logs"] / "curl-stdin.log").read_text()


@pytest.mark.parametrize("epoch", ["nan", "inf", "garbage", "0", "-1", "99999999999999999"])
def test_status_rejects_invalid_receipt_instead_of_reporting_healthy(sandbox: dict, epoch) -> None:
    make_env(sandbox)
    _write_status(sandbox, 0)
    status_file(sandbox).write_text(epoch)
    result = backup(sandbox, "status")
    assert result.returncode == 1 and "receipt is invalid" in result.stderr


def test_status_without_any_record_waits_a_day_and_a_half(sandbox: dict) -> None:
    make_env(sandbox, **TELEGRAM)
    first = backup(sandbox, "status", "--alert")
    assert first.returncode == 0, "a fresh install is not an emergency"
    assert "no successful backup recorded yet" in first.stdout
    first_seen = status_file(sandbox).parent / ".first_seen"
    first_seen.write_text(str(int(time.time() - 40 * 3600)))
    later = backup(sandbox, "status", "--alert")
    assert later.returncode == 1 and "no successful backup recorded yet" in later.stderr


def test_status_rejects_unknown_options(sandbox: dict) -> None:
    make_env(sandbox)
    assert backup(sandbox, "status", "--loud").returncode != 0


def test_cron_runs_the_staleness_check(sandbox: dict) -> None:
    result = bootstrap_fn(sandbox, "install_backup_cron")
    assert result.returncode == 0, result.stderr
    cron = Path(sandbox["env"]["REPEATER_CRON_FILE"]).read_text()
    assert re.search(
        r"^7 9 \* \* \* root REPEATER_BACKUP_DIR=\S+ bash \S+/backup\.sh status --alert 2>&1", cron, re.M
    )
    assert re.search(r"^\*/5 \* \* \* \* root REPEATER_BACKUP_DIR=\S+ python3 \S+/monitor\.py", cron, re.M)


# ----- restore (deploy-ops-1, deploy-ops-2)


def restore_dump(sandbox: dict, name: str = "repeater-db-20261014T031700Z.sql.gz") -> Path:
    seed_image(sandbox, "latest", "sha256:release")
    seed_image(sandbox, "previous", "sha256:older-release")
    return _old_dump(sandbox["root"] / "b", name, 1)


def exec_log(sandbox: dict) -> str:
    path = sandbox["logs"] / "exec.log"
    return path.read_text() if path.exists() else ""


def test_restore_requires_confirmation(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)))
    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert not any(c.startswith("stop") for c in compose_calls(sandbox))


def test_restore_rejects_unknown_options_and_extra_files(sandbox: dict) -> None:
    make_env(sandbox)
    dump = str(restore_dump(sandbox))
    assert "unknown restore option" in backup(sandbox, "restore", dump, "--yes", "--now").stderr
    assert "exactly one file" in backup(sandbox, "restore", dump, dump, "--yes").stderr
    assert not any(c.startswith("stop") for c in compose_calls(sandbox))


def test_restore_loads_the_dump_in_one_transaction_that_resets_the_schema(sandbox: dict) -> None:
    """deploy-ops-2: loading over the live database without a transaction died halfway when the
    live schema was newer than the dump (dropped constraints, rerun failed at the same statement)."""
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes")
    assert result.returncode == 0, result.stderr
    stream = (sandbox["logs"] / "stdin").read_text().splitlines()
    assert stream[:4] == [
        "SET client_min_messages = warning;",
        "BEGIN;",
        "DROP SCHEMA public CASCADE;",
        "CREATE SCHEMA public;",
    ]
    assert "-- PostgreSQL database dump" in stream
    assert stream[-1] == "COMMIT;"
    assert "UPDATE public.workspaces SET paused = true" in "\n".join(stream), (
        "paused inside the same transaction"
    )
    assert "psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q" in exec_log(sandbox)


def test_restore_pauses_every_workspace_and_leaves_the_worker_stopped(sandbox: dict) -> None:
    """deploy-ops-1: the restored queue still holds publish jobs for posts that went out after the
    dump; a started worker published them again."""
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes")
    assert result.returncode == 0, result.stderr
    calls = compose_calls(sandbox)
    assert (
        calls.index("stop web worker") < calls.index("run --rm --no-TTY migrate") < calls.index("up -d web")
    )
    assert "up -d web worker" not in calls and not any(c.startswith("up") and "worker" in c for c in calls)
    log = exec_log(sandbox)
    assert "psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q -c UPDATE workspaces SET paused = true" in log
    # the pause after the migrations comes after them (a dump older than the column gets it too)
    assert log.rindex("UPDATE workspaces SET paused = true") > log.index("psql")
    out = result.stdout
    assert "ALL WORKSPACES ARE PAUSED" in out
    assert "Dump taken at 20261014T031700Z" in out
    assert "The worker is stopped" in out and "up -d worker" in out


def test_restore_resume_starts_the_worker_but_workspaces_stay_paused(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", "--resume")
    assert result.returncode == 0, result.stderr
    calls = compose_calls(sandbox)
    assert "up -d web worker" in calls
    assert calls.index("run --rm --no-TTY migrate") < calls.index("up -d web worker")
    assert "SET paused = true" in exec_log(sandbox)
    assert "ALL WORKSPACES ARE PAUSED" in result.stdout and "--resume" in result.stdout


def test_restore_failure_rolls_back_and_leaves_services_stopped(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", STUB_PSQL_RC="3")
    assert result.returncode != 0
    assert "rolled back" in result.stderr
    calls = compose_calls(sandbox)
    assert not any(c.startswith("up") or "migrate" in c for c in calls)
    assert "stop web worker" in calls


def test_restore_aborts_when_the_migrations_fail_and_warns_not_to_start_the_worker(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", STUB_MIGRATE_RC="1")
    assert result.returncode != 0
    assert "Do NOT start the worker" in result.stderr
    assert not any(c.startswith("up") for c in compose_calls(sandbox))


def test_restore_aborts_when_it_cannot_pause_the_workspaces(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", "--resume", STUB_PSQL_C_RC="1")
    assert result.returncode != 0
    assert "could not pause the workspaces" in result.stderr
    assert not any(c.startswith("up") for c in compose_calls(sandbox))


def test_restore_applies_the_migrations_of_the_chosen_release(sandbox: dict) -> None:
    """deploy-ops-2: a rollback restore ran `migrate` from :latest (the broken release) because the
    tag was not honoured or documented."""
    make_env(sandbox)
    dump = str(restore_dump(sandbox))
    result = backup(sandbox, "restore", dump, "--yes", REPEATER_IMAGE_TAG="previous")
    assert result.returncode == 0, result.stderr
    assert "applying the migrations of repeater-app:previous" in result.stdout
    env_lines = (sandbox["logs"] / "docker-env.log").read_text().splitlines()
    calls = docker_log(sandbox)
    run_index = next(i for i, c in enumerate(calls) if " run --rm --no-TTY migrate" in c)
    assert "REPEATER_IMAGE_TAG=previous" in env_lines[run_index]
    missing = backup(sandbox, "restore", dump, "--yes", REPEATER_IMAGE_TAG="build-gone")
    assert missing.returncode != 0
    assert "image repeater-app:build-gone does not exist" in missing.stderr


@pytest.mark.parametrize("tag", ["previous", "build-20261014120000"])
def test_restore_printed_worker_command_uses_the_restored_release(sandbox: dict, tag: str) -> None:
    make_env(sandbox)
    if tag != "previous":
        images = sandbox["root"] / "state" / "images"
        images.mkdir(exist_ok=True)
        (images / f"repeater-app_{tag}").write_text("sha256:older")
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", REPEATER_IMAGE_TAG=tag)
    assert result.returncode == 0, result.stderr
    command = re.search(r"Start it with: (.+)", result.stdout).group(1)
    assert f"REPEATER_IMAGE_TAG={tag}" in command
    started = run(sandbox, ["bash", "-c", command])
    assert started.returncode == 0, started.stderr
    assert f"REPEATER_IMAGE_TAG={tag}" in (sandbox["logs"] / "docker-env.log").read_text().splitlines()[-1]


def test_restore_keeps_a_safety_dump_of_what_it_replaces(sandbox: dict) -> None:
    make_env(sandbox)
    backups = Path(sandbox["env"]["REPEATER_BACKUP_DIR"])
    for day in range(1, 5):
        _old_dump(backups, f"repeater-prerestore-2026010{day}T000000Z.sql.gz", 30 - day)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes")
    assert result.returncode == 0, result.stderr
    kept = sorted(p.name for p in backups.glob("repeater-prerestore-*.sql.gz"))
    assert len(kept) == 3, kept
    assert not list(backups.glob("repeater-db-*")), "the daily retention never touches the safety dumps"


def test_restore_continues_without_a_safety_dump_when_the_live_database_is_broken(sandbox: dict) -> None:
    make_env(sandbox)
    result = backup(sandbox, "restore", str(restore_dump(sandbox)), "--yes", STUB_PGDUMP_FAIL="1")
    assert result.returncode == 0, result.stderr
    assert "no safety dump of the current database could be taken" in result.stderr
    assert "BEGIN;" in (sandbox["logs"] / "stdin").read_text()
    assert not list(Path(sandbox["env"]["REPEATER_BACKUP_DIR"]).glob(".repeater-*")), "no temp file left"


def test_restore_rejects_corrupt_file(sandbox: dict) -> None:
    make_env(sandbox)
    bad = sandbox["root"] / "bad.sql.gz"
    bad.write_text("not gzip")
    result = backup(sandbox, "restore", str(bad), "--yes")
    assert result.returncode != 0
    assert "not a valid gzip file" in result.stderr


def test_restore_rejects_a_gzip_that_is_not_a_dump(sandbox: dict) -> None:
    make_env(sandbox)
    other = sandbox["root"] / "other.sql.gz"
    other.write_bytes(gzip.compress(b"hello\n"))
    result = backup(sandbox, "restore", str(other), "--yes")
    assert result.returncode != 0 and "not a pg_dump file" in result.stderr
    assert not any(c.startswith("stop") for c in compose_calls(sandbox))


def test_help_documents_the_restore_flags(sandbox: dict) -> None:
    result = backup(sandbox, "--help")
    assert "--resume" in result.stdout and "PAUSES EVERY WORKSPACE" in result.stdout
    assert "pre-deploy" in result.stdout and "status" in result.stdout


# --------------------------------------------------------------------------- GitHub workflows


def _load_workflow(name: str) -> dict:
    yaml_mod = pytest.importorskip("yaml")
    data = yaml_mod.safe_load((WORKFLOWS / name).read_text())
    # PyYAML (YAML 1.1) reads the `on:` key as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _steps(workflow: dict):
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


@pytest.mark.parametrize("name", ["ci.yml", "deploy.yml", "server-inspect.yml"])
def test_workflow_hygiene(name: str, tmp_path: Path) -> None:
    workflow = _load_workflow(name)
    assert workflow["permissions"] == {"contents": "read"}
    for job_name, job in workflow["jobs"].items():
        if "steps" in job:
            assert job.get("timeout-minutes"), f"{name}:{job_name} needs timeout-minutes"
    shellcheck = _shellcheck()
    for job_name, step in _steps(workflow):
        if "uses" in step:
            assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
        script = step.get("run")
        if not script:
            continue
        # Expressions inside run scripts are a script-injection risk: values go through env.
        assert "${{" not in script, f"{name}:{job_name}:{step.get('name')}"
        path = tmp_path / "step.sh"
        path.write_text("#!/usr/bin/env bash\nset -eo pipefail\n" + script)
        check = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
        assert check.returncode == 0, f"{name}:{step.get('name')}: {check.stderr}"
        if shellcheck:
            lint = subprocess.run(
                [shellcheck, "-S", "warning", str(path)], capture_output=True, text=True, check=False
            )
            assert lint.returncode == 0, f"{name}:{step.get('name')}:\n{lint.stdout}"


def test_ci_workflow() -> None:
    workflow = _load_workflow("ci.yml")
    assert {"push", "pull_request", "workflow_call"} <= set(workflow["on"])
    test_job = workflow["jobs"]["test"]
    assert test_job["services"]["postgres"]["image"] == "postgres:16"
    assert "REPEATER_TEST_PG" in test_job["env"]
    runs = "\n".join(step.get("run", "") for step in test_job["steps"])
    for command in ("ffmpeg", 'pip install -e ".[dev]"', "ruff check", "ruff format --check", "pytest"):
        assert command in runs
    python = next(s for s in test_job["steps"] if "setup-python" in s.get("uses", ""))
    assert python["with"]["python-version"] == "3.12"
    docker_job = workflow["jobs"]["docker"]
    docker_runs = "\n".join(step.get("run", "") for step in docker_job["steps"])
    assert "docker build" in docker_runs and "config --quiet" in docker_runs and "shellcheck" in docker_runs
    assert "bash -n deploy/*.sh" in docker_runs, "every deploy script is syntax-checked, selftest.sh included"


def test_ci_runs_the_media_tests_on_the_shipped_ffmpeg() -> None:
    """deploy-ops-8: CI tested media code against Ubuntu's ffmpeg 6.1 while the image ships Debian's
    7.1, where the thumbnail fallback fails; the image smoke test only imported modules."""
    workflow = _load_workflow("ci.yml")
    docker_job = workflow["jobs"]["docker"]
    assert docker_job["services"]["postgres"]["image"] == "postgres:16"
    runs = "\n".join(step.get("run", "") for step in docker_job["steps"])
    assert "docker build --target test" in runs
    run_line = next(line for line in runs.splitlines() if line.startswith("docker run") and "ci-test" in line)
    assert "REPEATER_TEST_PG" in run_line and "$PWD/tests:/app/tests:ro" in run_line
    assert "--target app" in runs, "the shipped stage is built explicitly too"
    text = (REPO / "Dockerfile").read_text()
    test_stage = text[text.index("FROM base AS test") : text.index("FROM base AS app")]
    assert "tests/test_media.py" in test_stage and "optional-dependencies" in test_stage
    assert "ffmpeg" in text[: text.index("FROM base AS test")], "the test stage inherits the shipped ffmpeg"


def test_ci_runs_the_container_selftest() -> None:
    workflow = _load_workflow("ci.yml")
    runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["docker"]["steps"])
    assert "docker tag repeater:ci repeater-app:latest" in runs
    assert "bash deploy/selftest.sh repeater-app:latest" in runs


def test_deploy_workflow() -> None:
    workflow = _load_workflow("deploy.yml")
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "ref" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert workflow["concurrency"] == {"group": "deploy-production", "cancel-in-progress": False}
    job = workflow["jobs"]["deploy"]
    assert set(job["needs"]) == {"config", "tests"}
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--exclude=/deploy/.env" in runs and "--delete" in runs
    assert "bootstrap.sh --stdin-env" in runs and "deploy.sh" in runs
    assert "StrictHostKeyChecking yes" in runs
    assert "ssh-keyscan" not in runs.replace("run ssh-keyscan", ""), "no trust on first use"
    assert "/healthz" in runs
    # Secrets are piped over SSH stdin, never interpolated into a remote command line.
    for line in runs.splitlines():
        if "ssh vps" in line:
            assert "ADMIN_PASSWORD" not in line and "CLIENT_SECRET" not in line
    assert workflow["jobs"]["tests"]["uses"] == "./.github/workflows/ci.yml"


def _step(workflow: dict, job: str, name: str) -> dict:
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)


def test_deploy_requires_a_pinned_ssh_host_key() -> None:
    """deploy-ops-12: without VPS_KNOWN_HOSTS every run trusted whatever key answered and then sent
    the admin password and the platform secrets over that connection."""
    workflow = _load_workflow("deploy.yml")
    check = _step(workflow, "config", "Check required secrets")
    assert "VPS_KNOWN_HOSTS" in check["env"]
    assert "REPEATER_DOMAIN VPS_KNOWN_HOSTS" in check["run"]
    text = (WORKFLOWS / "deploy.yml").read_text()
    assert "Required secrets: VPS_HOST, VPS_USER, VPS_SSH_KEY, REPEATER_DOMAIN, VPS_KNOWN_HOSTS" in text
    assert "VPS_KNOWN_HOSTS is required" in _step(workflow, "deploy", "Set up SSH")["run"]


def _run_step(script: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    path = tmp_path / "step.sh"
    path.write_text("#!/usr/bin/env bash\nset -eo pipefail\n" + script)
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    return subprocess.run(
        ["bash", str(path)], capture_output=True, text=True, env={**base, **env}, timeout=60, check=False
    )


def test_deploy_stops_without_known_hosts(tmp_path: Path) -> None:
    workflow = _load_workflow("deploy.yml")
    output = tmp_path / "out"
    base = {
        "VPS_HOST": "203.0.113.5",
        "VPS_USER": "root",
        "VPS_SSH_KEY": "key",
        "REPEATER_DOMAIN": DOMAIN,
        "EVENT_NAME": "workflow_dispatch",
        "GITHUB_OUTPUT": str(output),
    }
    script = _step(workflow, "config", "Check required secrets")["run"]
    missing = _run_step(script, tmp_path, {**base, "VPS_KNOWN_HOSTS": ""})
    assert missing.returncode == 1
    assert "Missing repository secrets: VPS_KNOWN_HOSTS" in missing.stdout
    assert "ready=false" in output.read_text()
    output.write_text("")
    ready = _run_step(script, tmp_path, {**base, "VPS_KNOWN_HOSTS": "203.0.113.5 ssh-ed25519 AAAA"})
    assert ready.returncode == 0 and "ready=true" in output.read_text()
    # A push (not a manual run) only skips the deploy, with a notice.
    push = _run_step(script, tmp_path, {**base, "VPS_KNOWN_HOSTS": "", "EVENT_NAME": "push"})
    assert push.returncode == 0 and "Deploy skipped" in push.stdout


def test_ssh_setup_never_scans_for_a_host_key(tmp_path: Path) -> None:
    workflow = _load_workflow("deploy.yml")
    script = _step(workflow, "deploy", "Set up SSH")["run"]
    env = {"VPS_SSH_KEY": "stub private key", "VPS_HOST": "203.0.113.5", "VPS_USER": "root", "VPS_PORT": "22"}
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    # ssh-keygen only validates the private key in this step; ssh-keyscan must never be called.
    (stub_bin / "ssh-keygen").write_text("#!/usr/bin/env bash\nexit 0\n")
    (stub_bin / "ssh-keyscan").write_text(f"#!/usr/bin/env bash\necho scanned >> {tmp_path}/scanned\n")
    for stub in stub_bin.iterdir():
        stub.chmod(0o755)
    path = f"{stub_bin}:{os.environ['PATH']}"
    refused = _run_step(script, tmp_path, {**env, "PATH": path, "VPS_KNOWN_HOSTS": ""})
    assert refused.returncode == 1
    assert "VPS_KNOWN_HOSTS is required" in refused.stdout
    assert not (tmp_path / "scanned").exists(), "the server's current key must not be trusted on the spot"
    pinned = "203.0.113.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPinned"
    accepted = _run_step(script, tmp_path, {**env, "PATH": path, "VPS_KNOWN_HOSTS": pinned})
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert (tmp_path / ".ssh" / "known_hosts").read_text().strip() == pinned
    assert "StrictHostKeyChecking yes" in (tmp_path / ".ssh" / "config").read_text()
    assert not (tmp_path / "scanned").exists()


def test_deploy_passes_the_proxy_choice_only_on_the_first_deploy() -> None:
    workflow = _load_workflow("deploy.yml")
    step = _step(workflow, "deploy", "Install or update Repeater")
    assert "REPEATER_PROXY" in step["env"]
    first = step["run"].split('if [ "$status" -eq 1 ]', 1)[1].split("elif", 1)[0]
    assert "REPEATER_PROXY=%s" in first
    assert "REPEATER_PROXY" not in step["run"].split("elif", 1)[1]


def test_server_inspect_is_read_only() -> None:
    workflow = _load_workflow("server-inspect.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    runs = "\n".join(step.get("run", "") for _, step in _steps(workflow))
    remote = runs.split("<< 'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]
    forbidden = [
        r"\bcompose\b.*\b(up|down|restart|stop|start|rm|run|exec|config|pull|build)\b",
        r"\bdocker (rm|stop|kill|restart|run|exec|system prune|image prune|volume rm)\b",
        r"\b(apt-get|apt|rsync|chmod|chown|mkdir|install|rm|tee|systemctl (start|stop|restart|enable|disable))\b",
        r"cat [^|]*\.env",
        r">\s*/(?!dev/null)",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, remote), pattern
    # Only non-secret keys are read from deploy/.env.
    keys = set(re.findall(r"env_value ([A-Z_]+)", remote))
    assert keys == {"COMPOSE_PROFILES", "REPEATER_WEB_BIND", "REPEATER_WEB_PORT", "REPEATER_DOMAIN"}
    for needle in ("ss -ltnp", "docker ps", "docker version", "free -h", "df -h", "--tail 100", "/healthz"):
        assert needle in remote
    # The same evidence bootstrap.sh uses: stopped containers and enabled-but-not-running services.
    for needle in ("docker ps -aq", "PortBindings", "systemctl is-enabled"):
        assert needle in remote


# --------------------------------------------------------------------------- docs


def test_arabic_docs_exist_and_carry_exact_urls() -> None:
    from repeater.config import Settings

    settings = Settings(base_url=f"https://{DOC_DOMAIN}")
    expected = {
        "00-deploy.md": [
            "DNS only",
            "VPS_SSH_KEY",
            "VPS_KNOWN_HOSTS",
            "bootstrap.sh",
            "backup.sh restore",
            "--redetect-proxy",
            "REPEATER_PROXY",
            "--resume",
            "REPEATER_FORWARDED_ALLOW_IPS",
            "RCLONE_MAX_DELETE",
        ],
        "01-tiktok.md": [
            settings.oauth_redirect_uri("tiktok"),
            "video.publish",
            "SELF_ONLY",
            "5",
            f"https://{DOC_DOMAIN}/m/",
            "PULL_FROM_URL",
            "FILE_UPLOAD",
            "url_ownership_unverified",
            "Intended Use",
        ],
        "02-google.md": [
            settings.oauth_redirect_uri("google"),
            "YouTube Data API v3",
            "Google Drive API",
            "7",
        ],
        "03-meta.md": [
            settings.oauth_redirect_uri("meta"),
            f"https://{DOC_DOMAIN}/meta/deauthorize",
            f"https://{DOC_DOMAIN}/meta/data-deletion",
            f"https://{DOC_DOMAIN}/privacy",
            "instagram_content_publish",
        ],
        "04-telegram.md": ["REPEATER_TELEGRAM_BOT_TOKEN", "REPEATER_TELEGRAM_CHAT_ID", "getUpdates"],
        "05-operations.md": [
            "--refresh-ytdlp",
            "docker compose",
            "logs",
            "rotate-keys",
            "gen-secrets",
            "repeater-predeploy",
            "REPEATER_IMAGE_TAG=previous",
            "REPEATER_WORKER_SHUTDOWN_GRACE_S",
            "deploy.sh --pull",
        ],
    }
    for name, needles in expected.items():
        text = (DOCS / name).read_text()
        assert re.search(r"[؀-ۿ]", text), f"{name} should be in Arabic"
        for needle in needles:
            assert needle in text, f"{name}: missing {needle!r}"


def test_google_home_page_is_not_a_login_redirect() -> None:
    """deploy-ops-7: Google's brand verification rejects a home page that redirects to a login."""
    text = (DOCS / "02-google.md").read_text()
    line = next(line for line in text.splitlines() if "Application home page:" in line)
    assert f"`https://{DOC_DOMAIN}`" not in line.split("Application home page:")[1].split("**")[0]
    assert "magic-lap.com/rotator" in line and "/privacy" in line
    assert "303" in line, "the doc says why the app's own URL does not qualify"


def test_tiktok_doc_states_the_intended_use_limit_and_the_url_property() -> None:
    text = (DOCS / "01-tiktok.md").read_text()
    assert "الحسابات التي تديرها أنت أو فريقك" in text and "غير مقبولة" in text
    assert "كل صانع محتوى" in text and "SELF_ONLY" in text
    assert "URL prefix" in text and f"https://{DOC_DOMAIN}/m/" in text
    assert "FILE_UPLOAD" in text and "url_ownership_unverified" in text


def test_meta_doc_lists_every_requested_scope() -> None:
    """The permissions table must match what repeater/platforms/meta.py actually asks for."""
    meta = pytest.importorskip("repeater.platforms.meta")
    text = (DOCS / "03-meta.md").read_text()
    table = text.split("## 2.", 1)[1].split("## 3.", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
    assert documented == set(meta.SCOPES), documented ^ set(meta.SCOPES)


def test_meta_callback_paths_match_the_app() -> None:
    source = REPO / "repeater" / "web" / "routes" / "meta.py"
    if not source.exists():
        pytest.skip("web routes not present")
    text = source.read_text()
    for path in ("/meta/deauthorize", "/meta/data-deletion"):
        assert f'"{path}"' in text


@pytest.mark.parametrize(
    ("engine", "compose", "error"),
    [
        ("29.8.2", "5.6.0", None),
        ("27.1.1", "2.29.1", None),
        ("24.0.7", "2.29.1", "Docker Engine 24.0.7 is too old"),
        ("27.1.1", "2.23.9", "Docker Compose 2.23.9 is too old"),
        ("27.1.1", "1.29.2", "Docker Compose 1.29.2 is too old"),
    ],
)
def test_docker_version_gate(sandbox: dict, engine: str, compose: str, error: str | None) -> None:
    # Never upgrades an existing Docker (other projects depend on it): too old means stop.
    snippet = f"""
        docker() {{
            if [[ "$1" == compose && "$2" == version ]]; then [[ "${{3:-}}" == --short ]] && echo {compose}; return 0; fi
            if [[ "$1" == version ]]; then echo {engine}; return 0; fi
        }}
        install_docker
    """
    result = bootstrap_fn(sandbox, snippet)
    if error is None:
        assert result.returncode == 0, result.stderr
        assert f"Docker Engine {engine}, Compose {compose}" in result.stdout
    else:
        assert result.returncode != 0
        assert error in result.stderr


@pytest.mark.parametrize(
    "addresses,allow",
    [
        ('"172.19.0.1"\n""', "127.0.0.1,172.16.0.0/12"),
        ('null\n"fd00::1"', "127.0.0.1,fd00::/64"),
    ],
)
def test_proxy_check_uses_docker_json_for_zero_value_ip_addresses(sandbox, addresses, allow):
    """Docker 29's netip.Addr renders an absent IPv6 gateway as 'invalid IP', but JSON as ''."""
    make_env(sandbox)
    script = f'set -Eeuo pipefail; source "{sandbox["deploy"]}/deploy.sh"; check_proxy_trust'
    result = run(
        sandbox,
        ["bash", "-c", script],
        extra_env={
            "STUB_PROXY_JSON": addresses,
            "STUB_PROXY_PYTHON": sys.executable,
            "FORWARDED_ALLOW_IPS": allow,
        },
    )
    assert result.returncode == 0, result.stderr
    call = next(call for call in compose_calls(sandbox) if "check-proxy" in call)
    assert "invalid IP" not in call and "null" not in call and '"' not in call


def test_proxy_check_still_refuses_an_untrusted_json_address(sandbox):
    make_env(sandbox)
    script = f'set -Eeuo pipefail; source "{sandbox["deploy"]}/deploy.sh"; check_proxy_trust'
    result = run(
        sandbox,
        ["bash", "-c", script],
        extra_env={
            "STUB_PROXY_JSON": '"10.99.0.1"\n""',
            "STUB_PROXY_PYTHON": sys.executable,
            "FORWARDED_ALLOW_IPS": "127.0.0.1,172.16.0.0/12",
        },
    )
    assert result.returncode != 0 and "trusted proxy mismatch: 10.99.0.1" in result.stderr
