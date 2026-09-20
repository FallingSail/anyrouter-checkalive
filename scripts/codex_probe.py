import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


EXIT_CODES = {"success": 0, "error": 1, "config_error": 2, "transient_error": 3, "timeout": 4}
PROMPTS_FILE = Path(__file__).resolve().with_name("prompts.txt")
FALLBACK_PROMPT = "Explain the difference between Python == and is with one short example."
PROMPT_CONSTRAINTS = "Answer in at most three sentences or eight lines of code. Answer directly without using tools, running commands, or inspecting files."
READ_LIMIT = 1024 * 1024


def pick_prompt(path=None):
    try:
        lines = (PROMPTS_FILE if path is None else path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    prompts = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    question = random.choice(prompts) if prompts else FALLBACK_PROMPT
    return f"{question}\n\n{PROMPT_CONSTRAINTS}"


def redact(text, token):
    if token:
        text = text.replace(token, "<redacted>")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9._~+/=-]+", "<redacted>", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text[-2000:]


def validate_config(token, base_url, model, timeout):
    if not token.strip():
        raise ValueError("Set KEEPALIVE_TOKEN (Usage: keepalive.sh [token] [base_url] [model]).")
    address = urlsplit(base_url)
    if address.scheme != "https" or not address.hostname:
        raise ValueError("BASE_URL must be an explicit HTTPS Responses API base URL.")
    if address.username or address.password or address.query or address.fragment:
        raise ValueError("BASE_URL must not contain credentials, a query, or a fragment.")
    if not model.strip():
        raise ValueError("Set MODEL to a Codex-compatible model supported by your relay.")
    if not 1 <= timeout <= 600:
        raise ValueError("PROBE_TIMEOUT_SEC must be an integer between 1 and 600.")


def build_config(base_url, model, timeout):
    return f'''model = {json.dumps(model)}
model_provider = "keepalive"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"

[features]
shell_tool = false

[agents]
enabled = false

[history]
persistence = "none"

[model_providers.keepalive]
name = "Keepalive relay"
base_url = {json.dumps(base_url.rstrip('/'))}
env_key = "KEEPALIVE_API_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = {timeout * 1000}
'''


def classify(exit_code, timed_out, stdout, stderr, final_message):
    if timed_out:
        return "timeout"
    terminal_event = None
    errors = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in {"turn.completed", "turn.failed"}:
            terminal_event = event_type
        if event_type in {"error", "turn.failed"}:
            errors.append(json.dumps(event))
    if exit_code == 0 and terminal_event == "turn.completed" and final_message.strip():
        return "success"
    diagnostic = ("\n".join(errors) + "\n" + stderr).lower()
    if re.search(r"\b(?:401|403|404)\b", diagnostic) or any(
        marker in diagnostic
        for marker in (
            "invalid api key", "invalid token", "unauthorized", "authentication",
            "model not found", "model_not_found", "unsupported model", "does not exist",
            "unknown model", "unexpected argument", "unknown option", "error loading config",
            "failed to parse", "missing environment variable",
        )
    ):
        return "config_error"
    if re.search(r"\b(?:408|429|500|502|503|504|524)\b", diagnostic) or any(
        marker in diagnostic
        for marker in (
            "rate limit", "rate_limit", "overloaded", "temporarily unavailable",
            "currently experiencing high demand",
            "econnreset", "connection reset", "connection refused", "timed out",
            "timeout", "error sending request", "stream disconnected",
        )
    ):
        return "transient_error"
    return "error"


def read_tail(path):
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - READ_LIMIT))
        return stream.read(READ_LIMIT).decode("utf-8", errors="replace")


def stop_process(process):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


@contextmanager
def probe_directory():
    directory = tempfile.mkdtemp(prefix="codex-keepalive-")
    try:
        yield directory
    finally:
        for attempt in range(5):
            try:
                shutil.rmtree(directory)
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if attempt < 4:
                    time.sleep(0.25 * (attempt + 1))
                else:
                    print(
                        f"  WARNING: Temporary directory cleanup failed (errno={error.errno}, "
                        f"winerror={getattr(error, 'winerror', None)}): {directory}. "
                        "Probe result is unchanged. Logs may remain; remove this directory "
                        "after its file handles are released.",
                        file=sys.stderr,
                    )


def run_probe(token, base_url, model, timeout):
    validate_config(token, base_url, model, timeout)
    executable = shutil.which("codex")
    if executable is None:
        return "config_error", "Codex CLI not found. Install @openai/codex first."
    with probe_directory() as temporary:
        root = Path(temporary)
        codex_home = root / "codex"
        work_dir = root / "work"
        codex_home.mkdir()
        work_dir.mkdir()
        (codex_home / "config.toml").write_text(build_config(base_url, model, timeout), encoding="utf-8")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CODEX_", "OPENAI_", "ANTHROPIC_", "KEEPALIVE_"))
            and key not in {"ANYROUTER_TOKENS", "QQ_SMTP_AUTH_CODE", "GH_TOKEN", "GITHUB_TOKEN"}
        }
        environment.update(
            CODEX_HOME=str(codex_home), HOME=str(root), USERPROFILE=str(root),
            KEEPALIVE_API_KEY=token,
        )
        stdout_path = root / "stdout.jsonl"
        stderr_path = root / "stderr.txt"
        final_path = root / "final.txt"
        command = [
            executable, "exec", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--json", "--output-last-message", str(final_path), pick_prompt(),
        ]
        timed_out = False
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command, cwd=work_dir, env=environment, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process(process)
            except BaseException:
                stop_process(process)
                raise
        stdout_text = read_tail(stdout_path)
        stderr_text = read_tail(stderr_path)
        status = classify(process.returncode, timed_out, stdout_text, stderr_text, read_tail(final_path))
        diagnostic = "" if status == "success" else redact(stdout_text + "\n" + stderr_text, token)
        return status, diagnostic


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    token = os.environ.get("KEEPALIVE_TOKEN", "").strip()
    try:
        timeout = int(os.environ.get("PROBE_TIMEOUT_SEC", "120"))
        status, diagnostic = run_probe(
            token, os.environ.get("BASE_URL", "").strip(), os.environ.get("MODEL", "").strip(), timeout,
        )
    except (ValueError, OSError) as error:
        status, diagnostic = "config_error", redact(str(error), token)
    print(f"  STATUS={status}")
    if diagnostic.strip():
        for line in diagnostic.splitlines():
            print(f"    {line}")
    return EXIT_CODES[status]


if __name__ == "__main__":
    sys.exit(main())
