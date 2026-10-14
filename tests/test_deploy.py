"""Tests for the deployment files: deploy/*.sh, docker-compose.yml, .env.example, Caddyfile,
Dockerfile, GitHub workflows and the Arabic operator docs.

Shell scripts run for real under bash with stub `docker`, `ss`, `ip`, `curl`, `rclone` and
`systemctl` commands on PATH, inside a temporary copy of deploy/, so nothing touches the host.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
SCRIPTS = ("bootstrap.sh", "deploy.sh", "backup.sh")
WORKFLOWS = REPO / ".github" / "workflows"
DOCS = REPO / "docs" / "ar"
DOMAIN = "rotator.magic-lap.com"

DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
printf 'YTDLP_REFRESH=%s REPEATER_IMAGE_TAG=%s\n' "${YTDLP_REFRESH:-}" "${REPEATER_IMAGE_TAG:-}" >> "$STUB_ENV_LOG"
case "$1" in
  volume) [[ -n "${STUB_PG_VOLUME:-}" ]] && exit 0; exit 1 ;;
  ps) cat "${STUB_DOCKER_PS:-/dev/null}"; exit 0 ;;
  image) [[ "$2" == inspect ]] && exit 1; exit 0 ;;
  inspect) echo "${STUB_HEALTH:-healthy}"; exit 0 ;;
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
  build) exit "${STUB_BUILD_RC:-0}" ;;
  run) exit "${STUB_MIGRATE_RC:-0}" ;;
  ps)
    if [[ " $* " == *" --status running "* ]]; then printf '%s' "${STUB_DB_RUNNING-dbid}"; exit 0; fi
    if [[ " $* " == *" -q "* ]]; then echo "cid-${*: -1}"; exit 0; fi
    echo "SERVICE STATUS" ;;
  exec)
    [[ "${1:-}" == "-T" ]] && shift
    shift
    printf '%s\n' "$*" >> "$STUB_EXEC_LOG"
    case "${1:-}" in
      pg_dump)
        if [[ -n "${STUB_PGDUMP_FAIL:-}" ]]; then echo "pg_dump: connection refused" >&2; exit 1; fi
        printf -- '--\n-- PostgreSQL database dump\n--\nSELECT 1;\n' ;;
      psql) cat > "$STUB_STDIN_FILE"; exit "${STUB_PSQL_RC:-0}" ;;
      python)
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
esac
"""

RCLONE_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_RCLONE_LOG"
exit "${STUB_RCLONE_RC:-0}"
"""

OK_STUB = "#!/usr/bin/env bash\nexit 0\n"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A copy of deploy/ under tmp_path/app/deploy plus stub commands; returns paths and env."""
    deploy = tmp_path / "app" / "deploy"
    deploy.mkdir(parents=True)
    for name in (*SCRIPTS, ".env.example", "docker-compose.yml", "Caddyfile"):
        shutil.copy2(DEPLOY / name, deploy / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "docker": DOCKER_STUB,
        "ss": SS_STUB,
        "ip": IP_STUB,
        "rclone": RCLONE_STUB,
        "systemctl": OK_STUB,
        "curl": OK_STUB,
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


def compose_config(profiles: str = "") -> dict:
    env = {**os.environ, "COMPOSE_PROFILES": profiles}
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
    cmd = re.search(r"^CMD \[(.*?)\]$", text, re.MULTILINE | re.DOTALL)
    assert cmd
    for part in (
        '"repeater.web.app:create_app"',
        '"--factory"',
        '"--proxy-headers"',
        '"--forwarded-allow-ips", "*"',
    ):
        assert part in cmd.group(1)
    assert "COPY alembic.ini" in text or "alembic.ini ./" in text
    assert "COPY migrations" in text
    # Dependencies are installed before the code is copied (cached layer).
    assert text.index("pip install -r") < text.index("COPY repeater ./repeater")
    assert "[dev]" not in text


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
    assert cpus <= 2.0 + 1e-9, cpus
    assert memory <= 6 * 1024**3, memory


@requires_compose
def test_compose_media_and_secrets_wiring() -> None:
    services = compose_config("caddy")["services"]
    for name in ("web", "worker"):
        mounts = [v for v in services[name]["volumes"] if v["type"] == "bind"]
        assert [(m["source"], m["target"]) for m in mounts] == [
            ("/srv/repeater/media", "/srv/repeater/media")
        ]
        env = services[name]["environment"]
        assert env["REPEATER_MEDIA_ROOT"] == "/srv/repeater/media"
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


@pytest.mark.parametrize(
    ("ss_output", "docker_ps", "kind"),
    [
        ("", "", "none"),
        (
            'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6),("nginx",pid=811,fd=6))',
            "",
            "host-nginx",
        ),
        ('LISTEN 0 4096 *:443 *:* users:(("caddy",pid=90,fd=7))', "", "host-caddy"),
        ('LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("apache2",pid=5,fd=3))', "", "host-apache"),
        (
            'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=7,fd=4))',
            "coolify-proxy|traefik:v3.1|coolify|0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp",
            "docker-traefik",
        ),
        ("", "npm-app-1|jc21/nginx-proxy-manager:latest|npm|0.0.0.0:80->80/tcp", "docker-npm"),
        ("", "web|nginx:1.27|site|0.0.0.0:443->443/tcp", "docker-nginx"),
        ("", "repeater-caddy-1|caddy:2.11-alpine|repeater|0.0.0.0:80->80/tcp", "repeater-caddy"),
        ("", "db|postgres:16|x|5432/tcp", "none"),
        ('LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=7,fd=4))', "", "docker-other"),
        ('LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("haproxy",pid=7,fd=4))', "", "host-other"),
    ],
)
def test_detect_proxy(sandbox: dict, ss_output: str, docker_ps: str, kind: str) -> None:
    ss_file = sandbox["root"] / "ss.txt"
    ss_file.write_text(ss_output + ("\n" if ss_output else ""))
    ps_file = sandbox["root"] / "ps.txt"
    ps_file.write_text(docker_ps + ("\n" if docker_ps else ""))
    result = bootstrap_fn(
        sandbox, "detect_proxy", extra_env={"STUB_SS_WEB": str(ss_file), "STUB_DOCKER_PS": str(ps_file)}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split(" ", 1)[0] == kind


def _configure(sandbox: dict, ss_output: str = "", docker_ps: str = "", busy_ports: str = "") -> tuple:
    files = {}
    for name, content in (
        ("STUB_SS_WEB", ss_output),
        ("STUB_DOCKER_PS", docker_ps),
        ("STUB_SS_BUSY", busy_ports),
    ):
        path = sandbox["root"] / f"{name}.txt"
        path.write_text(content + ("\n" if content else ""))
        files[name] = str(path)
    result = bootstrap_fn(
        sandbox,
        "REPEATER_DOMAIN=rotator.magic-lap.com; create_env_file; set_domain; configure_proxy; "
        'echo "mode=$PROXY_MODE"; if [[ "$PROXY_MODE" == external ]]; then print_proxy_snippet; fi',
        extra_env=files,
    )
    return result, read_env(sandbox["deploy"] / ".env")


def test_configure_proxy_free_ports_enable_caddy(sandbox: dict) -> None:
    result, values = _configure(sandbox)
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == "caddy"
    assert values["REPEATER_WEB_BIND"] == "127.0.0.1"
    assert "mode=caddy" in result.stdout


def test_configure_proxy_host_nginx_prints_snippet_and_leaves_it_alone(sandbox: dict) -> None:
    nginx = 'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=812,fd=6))'
    result, values = _configure(sandbox, ss_output=nginx)
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == ""
    assert values["REPEATER_WEB_BIND"] == "127.0.0.1"
    assert "mode=external" in result.stdout
    assert f"server_name {DOMAIN};" in result.stdout
    assert "proxy_pass http://127.0.0.1:8090;" in result.stdout
    assert "client_max_body_size 600m;" in result.stdout
    log = docker_log(sandbox)
    assert not any(line.startswith(("stop", "rm", "kill", "restart")) for line in log)


def test_configure_proxy_container_proxy_binds_docker0(sandbox: dict) -> None:
    traefik = "coolify-proxy|traefik:v3.1|coolify|0.0.0.0:80->80/tcp"
    result, values = _configure(sandbox, docker_ps=traefik)
    assert result.returncode == 0, result.stderr
    assert values["COMPOSE_PROFILES"] == ""
    assert values["REPEATER_WEB_BIND"] == "172.18.0.1"
    assert 'url: "http://172.18.0.1:8090"' in result.stdout
    assert f"Host(`{DOMAIN}`)" in result.stdout


def test_configure_proxy_moves_off_a_busy_web_port(sandbox: dict) -> None:
    result, values = _configure(sandbox, busy_ports="8090\n8091")
    assert result.returncode == 0, result.stderr
    assert values["REPEATER_WEB_PORT"] == "8092"


def test_configure_proxy_rerun_keeps_decision(sandbox: dict) -> None:
    make_env(sandbox, COMPOSE_PROFILES="", REPEATER_DOMAIN=DOMAIN)
    result = bootstrap_fn(sandbox, 'configure_proxy; echo "mode=$PROXY_MODE"')
    assert result.returncode == 0, result.stderr
    assert "mode=external" in result.stdout
    assert "ports 80/443 are free" in result.stderr
    assert read_env(sandbox["deploy"] / ".env")["COMPOSE_PROFILES"] == ""


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
    assert compose_calls(sandbox) == []


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


def test_deploy_no_build_uses_requested_tag(sandbox: dict) -> None:
    make_env(sandbox)
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
    result = backup(sandbox, STUB_DB_RUNNING="")
    assert result.returncode != 0
    assert "db container is not running" in result.stderr


def test_backup_rejects_bad_retention(sandbox: dict) -> None:
    make_env(sandbox, BACKUP_RETENTION_DAYS="two weeks")
    result = backup(sandbox)
    assert result.returncode != 0
    assert "BACKUP_RETENTION_DAYS" in result.stderr


@pytest.mark.parametrize(("media_mode", "media_synced"), [("daily", True), ("off", False)])
def test_backup_offsite_copy(sandbox: dict, media_mode: str, media_synced: bool) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:magiclab-repeater", RCLONE_MEDIA_SYNC=media_mode)
    result = backup(sandbox)
    assert result.returncode == 0, result.stderr
    calls = (sandbox["logs"] / "rclone.log").read_text().splitlines()
    assert any(
        c.startswith("sync ") and "r2:magiclab-repeater/db" in c and "--include repeater-db-*.sql.gz" in c
        for c in calls
    )
    assert any("r2:magiclab-repeater/media" in c for c in calls) is media_synced
    if media_synced:
        media_call = next(c for c in calls if "/media" in c)
        assert "--exclude uploads/**" in media_call and "--timeout 5m" in media_call


def test_backup_offsite_failure_is_reported(sandbox: dict) -> None:
    make_env(sandbox, RCLONE_REMOTE="r2:magiclab-repeater")
    result = backup(sandbox, STUB_RCLONE_RC="1")
    assert result.returncode != 0
    assert "rclone sync of the database dumps failed" in result.stderr


def test_restore_requires_confirmation(sandbox: dict) -> None:
    make_env(sandbox)
    dump = _old_dump(sandbox["root"] / "b", "repeater-db-x.sql.gz", 1)
    result = backup(sandbox, "restore", str(dump))
    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert not any(c.startswith("stop") for c in compose_calls(sandbox))


def test_restore_loads_dump_and_restarts(sandbox: dict) -> None:
    make_env(sandbox)
    dump = _old_dump(sandbox["root"] / "b", "repeater-db-x.sql.gz", 1)
    result = backup(sandbox, "restore", str(dump), "--yes")
    assert result.returncode == 0, result.stderr
    assert (sandbox["logs"] / "stdin").read_text() == "-- PostgreSQL database dump\n"
    steps = compose_calls(sandbox)
    assert (
        steps.index("stop web worker")
        < steps.index("run --rm --no-TTY migrate")
        < steps.index("up -d web worker")
    )
    assert "psql -U repeater -d repeater -v ON_ERROR_STOP=1 -q" in (sandbox["logs"] / "exec.log").read_text()


def test_restore_failure_leaves_services_stopped(sandbox: dict) -> None:
    make_env(sandbox)
    dump = _old_dump(sandbox["root"] / "b", "repeater-db-x.sql.gz", 1)
    result = backup(sandbox, "restore", str(dump), "--yes", STUB_PSQL_RC="3")
    assert result.returncode != 0
    assert "restore failed" in result.stderr
    assert not any(c.startswith("up") for c in compose_calls(sandbox))


def test_restore_rejects_corrupt_file(sandbox: dict) -> None:
    make_env(sandbox)
    bad = sandbox["root"] / "bad.sql.gz"
    bad.write_text("not gzip")
    result = backup(sandbox, "restore", str(bad), "--yes")
    assert result.returncode != 0
    assert "not a valid gzip file" in result.stderr


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
    docker_runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["docker"]["steps"])
    assert "docker build" in docker_runs and "config --quiet" in docker_runs and "shellcheck" in docker_runs


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
    assert "ssh-keyscan" in runs and "::warning::" in runs
    assert "/healthz" in runs
    # Secrets are piped over SSH stdin, never interpolated into a remote command line.
    for line in runs.splitlines():
        if "ssh vps" in line:
            assert "ADMIN_PASSWORD" not in line and "CLIENT_SECRET" not in line
    assert workflow["jobs"]["tests"]["uses"] == "./.github/workflows/ci.yml"


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


# --------------------------------------------------------------------------- docs


def test_arabic_docs_exist_and_carry_exact_urls() -> None:
    from repeater.config import Settings

    settings = Settings(base_url=f"https://{DOMAIN}")
    expected = {
        "00-deploy.md": ["DNS only", "VPS_SSH_KEY", "VPS_KNOWN_HOSTS", "bootstrap.sh", "backup.sh restore"],
        "01-tiktok.md": [settings.oauth_redirect_uri("tiktok"), "video.publish", "SELF_ONLY", "5"],
        "02-google.md": [
            settings.oauth_redirect_uri("google"),
            "YouTube Data API v3",
            "Google Drive API",
            "7",
        ],
        "03-meta.md": [
            settings.oauth_redirect_uri("meta"),
            f"https://{DOMAIN}/meta/deauthorize",
            f"https://{DOMAIN}/meta/data-deletion",
            f"https://{DOMAIN}/privacy",
            "instagram_content_publish",
        ],
        "04-telegram.md": ["REPEATER_TELEGRAM_BOT_TOKEN", "REPEATER_TELEGRAM_CHAT_ID", "getUpdates"],
        "05-operations.md": ["--refresh-ytdlp", "docker compose", "logs"],
    }
    for name, needles in expected.items():
        text = (DOCS / name).read_text()
        assert re.search(r"[؀-ۿ]", text), f"{name} should be in Arabic"
        for needle in needles:
            assert needle in text, f"{name}: missing {needle!r}"


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
