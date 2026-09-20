import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


VARIABLE = "CODEX_SCHEDULE_STATE"
BEIJING = timezone(timedelta(hours=8))


def transition(state, action, now, interval="15", start_time="04:00"):
    if action == "stop":
        return {"enabled": False}, False, "Stopped. Future checks will not call the model."
    if action in {"immediate", "scheduled"}:
        minutes = int(interval)
        if not 5 <= minutes <= 1440:
            raise ValueError("Interval must be between 5 and 1440 minutes.")
        due = now
        if action == "scheduled":
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start_time):
                raise ValueError("Start time must be HH:MM in Beijing time.")
            hour, minute = map(int, start_time.split(":"))
            due = now.astimezone(BEIJING).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        state = {"enabled": True, "interval_minutes": minutes, "next_due": due.isoformat()}
    elif action != "tick":
        raise ValueError("Unknown scheduler action.")
    if not state or not state.get("enabled"):
        return state, False, "Schedule is stopped or has not been started."
    due = datetime.fromisoformat(state["next_due"])
    if due.tzinfo is None:
        raise ValueError("Stored schedule must include a timezone.")
    minutes = int(state["interval_minutes"])
    if not 5 <= minutes <= 1440:
        raise ValueError("Invalid stored interval.")
    if now < due:
        return state, False, f"Waiting until {due.astimezone(BEIJING).isoformat()} (Beijing)."
    state = dict(state, next_due=(now + timedelta(minutes=minutes)).isoformat())
    return state, True, f"Probe due. Next eligible time: {datetime.fromisoformat(state['next_due']).astimezone(BEIJING).isoformat()} (Beijing)."


class RepositoryState:
    def __init__(self):
        repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository name.")
        self.token = os.environ.get("SCHEDULER_TOKEN", "")
        if not self.token:
            raise ValueError("Create repository secret SCHEDULER_TOKEN with Variables read/write permission.")
        self.url = f"https://api.github.com/repos/{repository}/actions/variables"

    def request(self, method, suffix="", payload=None):
        request = Request(self.url + suffix, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }, data=None if payload is None else json.dumps(payload).encode("utf-8"))
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None

    def read(self):
        try:
            result = self.request("GET", f"/{VARIABLE}")
        except HTTPError as error:
            if error.code == 404:
                return None
            raise
        return json.loads(result["value"])

    def save(self, state, exists):
        payload = {"name": VARIABLE, "value": json.dumps(state)}
        self.request("PATCH" if exists else "POST", f"/{VARIABLE}" if exists else "", payload)


def main():
    try:
        repository = RepositoryState()
        original = repository.read()
        state, run, message = transition(
            original, os.environ.get("SCHEDULE_ACTION", "tick"), datetime.now(timezone.utc),
            os.environ.get("INTERVAL_MINUTES", "15"), os.environ.get("START_TIME", "04:00"),
        )
        if state != original:
            repository.save(state, original is not None)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"run_probe={'true' if run else 'false'}\n")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"## Scheduler\n{message}\n")
        print(message)
        return 0
    except HTTPError as error:
        print(f"Scheduler GitHub API error: HTTP {error.code}. Check repository access and Variables permission.", file=sys.stderr)
    except (ValueError, KeyError, TypeError, OSError, URLError):
        print("Scheduler failed: check inputs, stored state, token configuration, and network. No probe started.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
