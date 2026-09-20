import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "codex_probe", Path(__file__).resolve().parents[1] / "scripts" / "codex_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_prompt_pool_filters_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompts.txt"
            path.write_text("\ufeff# Header\n\n  # Comment\n Explain SQL NULL. \nExplain Python is.\n", encoding="utf-8")
            with patch.object(probe.random, "choice", return_value="Explain Python is.") as choose:
                prompt = probe.pick_prompt(path)
            choose.assert_called_once_with(["Explain SQL NULL.", "Explain Python is."])
            self.assertTrue(prompt.startswith("Explain Python is.\n\n"))
            self.assertIn(probe.PROMPT_CONSTRAINTS, prompt)

    def test_prompt_pool_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.txt"
            self.assertIn(probe.FALLBACK_PROMPT, probe.pick_prompt(path))
            path.write_text("# Only comments\n\n", encoding="utf-8")
            self.assertIn(probe.FALLBACK_PROMPT, probe.pick_prompt(path))

    def test_bundled_prompt_pool(self):
        lines = probe.PROMPTS_FILE.read_text(encoding="utf-8").splitlines()
        questions = [line for line in lines if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(questions), 20)
        self.assertEqual(len(questions), len(set(questions)))

    def test_cleanup_retries_temporary_lock(self):
        with patch.object(probe.tempfile, "mkdtemp", return_value="mock-directory"), \
             patch.object(probe.shutil, "rmtree", side_effect=[PermissionError(13, "locked"), None]) as remove, \
             patch.object(probe.time, "sleep") as sleep:
            with probe.probe_directory() as directory:
                self.assertEqual(directory, "mock-directory")
            self.assertEqual(remove.call_count, 2)
            sleep.assert_called_once()

    def test_cleanup_failure_preserves_result(self):
        with patch.object(probe.tempfile, "mkdtemp", return_value="mock-directory"), \
             patch.object(probe.shutil, "rmtree", side_effect=PermissionError(13, "locked")) as remove, \
             patch.object(probe.time, "sleep"), \
             patch.object(probe.sys, "stderr", new_callable=io.StringIO) as stderr:
            def operation():
                with probe.probe_directory():
                    return "transient_error"

            self.assertEqual(operation(), "transient_error")
            self.assertEqual(remove.call_count, 5)
            self.assertIn("WARNING", stderr.getvalue())
            self.assertIn("mock-directory", stderr.getvalue())

    def test_cleanup_does_not_mask_original_exception(self):
        with patch.object(probe.tempfile, "mkdtemp", return_value="mock-directory"), \
             patch.object(probe.shutil, "rmtree", side_effect=PermissionError(13, "locked")), \
             patch.object(probe.time, "sleep"), \
             patch.object(probe.sys, "stderr", new_callable=io.StringIO):
            with self.assertRaisesRegex(ValueError, "original failure"):
                with probe.probe_directory():
                    raise ValueError("original failure")

    def test_success_requires_completed_event_and_reply(self):
        completed = json.dumps({"type": "turn.completed"})
        self.assertEqual(probe.classify(0, False, completed, "", "OK"), "success")
        for code, output, reply in [(1, completed, "OK"), (0, "", "OK"), (0, completed, "")]:
            self.assertEqual(probe.classify(code, False, output, "", reply), "error")

    def test_failed_event_overrides_completion(self):
        events = '\n'.join(json.dumps({"type": event}) for event in ["turn.completed", "turn.failed"])
        self.assertEqual(probe.classify(0, False, events, "", "OK"), "error")

    def test_high_demand_events_are_transient(self):
        message = "We’re currently experiencing high demand, which may cause temporary errors."
        events = "\n".join(json.dumps(event) for event in [
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
        ])
        self.assertEqual(probe.classify(1, False, events, "", ""), "transient_error")
        self.assertEqual(probe.classify(1, False, "", message, ""), "transient_error")
        self.assertEqual(probe.classify(1, False, events, "HTTP 401", ""), "config_error")

    def test_error_categories(self):
        for message, expected in [("HTTP 401", "config_error"), ("HTTP 403", "config_error"),
                                  ("HTTP 429", "transient_error"), ("HTTP 503", "transient_error"),
                                  ("ECONNRESET", "transient_error"), ("unknown", "error")]:
            with self.subTest(message=message):
                self.assertEqual(probe.classify(1, False, "", message, ""), expected)
        self.assertEqual(probe.classify(0, True, "", "", "OK"), "timeout")

    def test_config_is_valid_toml_without_secret(self):
        config = tomllib.loads(probe.build_config('https://relay.example/v1/', 'model"quoted', 120))
        self.assertEqual(config["model"], 'model"quoted')
        self.assertEqual(config["model_providers"]["keepalive"]["wire_api"], "responses")
        self.assertEqual(config["model_providers"]["keepalive"]["base_url"], "https://relay.example/v1")
        self.assertFalse(config["features"]["shell_tool"])

    def test_invalid_settings(self):
        for token, url, model, timeout in [("", "https://relay.example/v1", "test", 1),
                ("token", "http://relay.example", "test", 1),
                ("token", "https://user:pass@relay.example", "test", 1),
                ("token", "https://relay.example?key=secret", "test", 1),
                ("token", "https://relay.example", "", 1),
                ("token", "https://relay.example", "test", 601)]:
            with self.subTest(url=url, timeout=timeout), self.assertRaises(ValueError):
                probe.validate_config(token, url, model, timeout)

    def test_redaction_before_truncation(self):
        result = probe.redact("x" * 3000 + " token-secret Bearer another-secret sk-abc123", "token-secret")
        self.assertLessEqual(len(result), 2000)
        for secret in ["token-secret", "another-secret", "sk-abc123"]:
            self.assertNotIn(secret, result)

    def test_missing_cli(self):
        with patch.object(probe.shutil, "which", return_value=None):
            self.assertEqual(probe.run_probe("token", "https://relay.example", "test", 1)[0], "config_error")

    def test_isolated_execution_and_cleanup(self):
        captured = {}

        class FakeProcess:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        def launch(command, **kwargs):
            captured.update(kwargs)
            self.assertNotIn("token-secret", command)
            self.assertNotIn("ANYROUTER_TOKENS", kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertEqual(kwargs["env"]["KEEPALIVE_API_KEY"], "token-secret")
            kwargs["stdout"].write(b'{"type":"turn.completed"}\n')
            Path(command[command.index("--output-last-message") + 1]).write_text("OK")
            return FakeProcess()

        with patch.object(probe.shutil, "which", return_value="mock-codex"), \
             patch.object(probe.subprocess, "Popen", side_effect=launch), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated", "ANYROUTER_TOKENS": "other"}):
            self.assertEqual(probe.run_probe("token-secret", "https://relay.example", "test", 1), ("success", ""))
        self.assertFalse(Path(captured["env"]["CODEX_HOME"]).exists())
        self.assertFalse(Path(captured["cwd"]).exists())

    def test_timeout_stops_process(self):
        class HangingProcess:
            returncode = 1

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("mock-codex", timeout)

        with patch.object(probe.shutil, "which", return_value="mock-codex"), \
             patch.object(probe.subprocess, "Popen", return_value=HangingProcess()), \
             patch.object(probe, "stop_process") as stop:
            self.assertEqual(probe.run_probe("token", "https://relay.example", "test", 1)[0], "timeout")
            stop.assert_called_once()


class RunnerTests(unittest.TestCase):
    def test_batch_and_monitor_offline(self):
        bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
        if not bash or not Path(bash).exists():
            self.skipTest("Bash not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for source in (Path(__file__).resolve().parents[1] / "scripts").glob("*.sh"):
                (scripts / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            (scripts / "mock-python").write_text(
                '#!/usr/bin/env bash\n'
                'if [ "${MOCK_STATUS:-success}" = config_error ]; then\n'
                'echo "STATUS=config_error"; exit 2; fi\n'
                'echo "STATUS=success"\n', encoding="utf-8", newline="\n",
            )
            (scripts / "mock-python").chmod(0o700)
            environment = dict(os.environ)
            environment.update(BASE_URL="https://relay.example/v1", MODEL="test-model",
                               ANYROUTER_TOKENS="token-one\r\n\n token-one \ntoken-two",
                               MAX_DURATION_SEC="10", SLEEP_BETWEEN_TOKENS="0",
                               QQ_EMAIL="", QQ_SMTP_AUTH_CODE="", MOCK_STATUS="success")
            for script, status, expected in [
                ("run-all.sh --once", "success", 0),
                ("run-all.sh", "config_error", 1),
                ("monitor-recovery.sh", "success", 0),
                ("monitor-recovery.sh", "config_error", 1),
            ]:
                with self.subTest(script=script, status=status):
                    environment["MOCK_STATUS"] = status
                    command = 'export PATH=/usr/bin:$PATH; export PYTHON_BIN="$PWD/scripts/mock-python"; bash scripts/' + script
                    result = subprocess.run([str(bash), "-c", command], cwd=root, env=environment,
                                            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertIn("Loaded 2 token(s)", result.stdout)
                    self.assertNotIn("token-one", result.stdout)


if __name__ == "__main__":
    unittest.main()
