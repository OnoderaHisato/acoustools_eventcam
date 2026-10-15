"""Notify the operator when an unattended recording session needs them (paper sessions, 2026-10-15).

The sessions run for hours with nobody at the PC. This module sends a short message when the session ends
(complete, current guard, failure, Ctrl+C), when the particle must be changed, and when the PAT is holding the
particle for collection. Three channels, all optional, chosen by ``notify_config.json`` next to the scripts
(``notify_config.example.json`` is the template; the real file is ignored by git because it names an account):

* ``email``: SMTP with STARTTLS or SSL. The password is never written in the config: it is read from the
  environment variable named by ``password_env`` or from the file named by ``password_file`` (a plain text file
  outside the repository). Gmail needs an app password.
* ``event_dir``: every event is also appended as one JSON line to ``<event_dir>/notify_events.jsonl`` and the
  latest event is written to ``<event_dir>/latest_event.json``. A Claude session (or any watcher) can poll that
  file and relay the message; this works without any account.
* ``beep``: ``winsound`` beeps on the PC speaker (the particle-hold loop beeps on its own; this flag only adds
  a beep on the other events).

Nothing here raises: a failed notification is printed as ``[NOTIFY][WARN]`` and the session goes on.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parent / "notify_config.json"
EVENTS_FILE = "notify_events.jsonl"
LATEST_FILE = "latest_event.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the notify config; an absent file means 'no notifications' (empty dict)."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[NOTIFY][WARN] Could not read {config_path}: {exc}", flush=True)
        return {}
    if not isinstance(data, dict):
        print(f"[NOTIFY][WARN] {config_path} must hold a JSON object", flush=True)
        return {}
    return data


def _user_environment_value(name: str) -> str | None:
    """The user-level environment variable as stored now (HKCU\Environment).

    A variable set with [Environment]::SetEnvironmentVariable(..., "User") reaches only processes started
    afterwards from Explorer; terminals that were already open (or started by an app that was) do not see it.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return str(value) if value else None
    except OSError:
        return None


def _email_password(email_cfg: dict[str, Any]) -> str | None:
    env_name = str(email_cfg.get("password_env", "") or "")
    if env_name:
        value = os.environ.get(env_name) or _user_environment_value(env_name)
        if value:
            return value
    password_file = str(email_cfg.get("password_file", "") or "")
    if password_file:
        try:
            return Path(password_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"[NOTIFY][WARN] password_file could not be read: {exc}", flush=True)
    return None


def send_email(email_cfg: dict[str, Any], subject: str, body: str, timeout_sec: float = 20.0) -> bool:
    """Send one plain-text message; False (with a warning) when anything is missing or fails."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formatdate

    host = str(email_cfg.get("smtp_host", "") or "")
    port = int(email_cfg.get("smtp_port", 587) or 587)
    sender = str(email_cfg.get("from", "") or "")
    recipients = email_cfg.get("to", [])
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(r) for r in recipients if str(r).strip()]
    if not host or not sender or not recipients:
        print("[NOTIFY][WARN] email: smtp_host, from and to are required", flush=True)
        return False
    username = str(email_cfg.get("username", "") or sender)
    password = _email_password(email_cfg)
    if password is None:
        print(
            "[NOTIFY][WARN] email: no SMTP password (set the user environment variable named by password_env, "
            "or password_file); Gmail refuses mail without it.",
            flush=True,
        )
        return False
    use_ssl = bool(email_cfg.get("ssl", False))
    use_starttls = bool(email_cfg.get("starttls", not use_ssl))
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)
    try:
        if use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout_sec)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout_sec)
        with server:
            server.ehlo()
            if use_starttls and not use_ssl:
                server.starttls()
                server.ehlo()
            if password is not None:
                server.login(username, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException, socket.timeout) as exc:
        print(f"[NOTIFY][WARN] email to {recipients} failed: {exc}", flush=True)
        return False
    print(f"[NOTIFY] email sent to {', '.join(recipients)}: {subject}", flush=True)
    return True


def write_event(event_dir: Path, event: dict[str, Any]) -> bool:
    """Append the event to notify_events.jsonl and rewrite latest_event.json in event_dir."""
    try:
        event_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with open(event_dir / EVENTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        latest = event_dir / LATEST_FILE
        tmp = latest.with_suffix(".json.tmp")
        tmp.write_text(line + "\n", encoding="utf-8")
        os.replace(tmp, latest)
    except OSError as exc:
        print(f"[NOTIFY][WARN] could not write the event file in {event_dir}: {exc}", flush=True)
        return False
    return True


def beep(times: int = 3, frequency_hz: int = 1200, duration_ms: int = 250) -> None:
    """PC-speaker beep on Windows; the BEL character elsewhere."""
    try:
        if sys.platform == "win32":
            import winsound

            # The system sound goes through the default playback device and its volume like any other sound;
            # Beep adds a plain tone. Neither is heard if the default device has no speaker (e.g. a monitor's HDMI).
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
            for _ in range(max(1, int(times))):
                winsound.Beep(int(frequency_hz), int(duration_ms))
            return
    except Exception:  # noqa: BLE001 - a missing speaker must not stop anything
        pass
    sys.stdout.write("\a" * max(1, int(times)))
    sys.stdout.flush()


def notify(
    event: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    event_dir: Path | None = None,
    subject_prefix: str = "[PAT]",
) -> dict[str, Any]:
    """Send one event through every configured channel; returns what was attempted.

    ``event`` is a short tag (session_complete, session_stopped, particle_change, particle_hold, ...),
    ``summary`` one line for a human, ``details`` anything worth keeping in the event file.
    """
    cfg = load_config() if config is None else dict(config)
    record = {
        "event": str(event),
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "summary": str(summary),
        "details": dict(details or {}),
    }
    result: dict[str, Any] = {"event": record["event"], "at": record["at"]}
    print(f"[NOTIFY] {record['event']}: {summary}", flush=True)

    target_dir = event_dir
    if target_dir is None and cfg.get("event_dir"):
        target_dir = Path(str(cfg["event_dir"])).expanduser()
    if target_dir is not None:
        result["event_file"] = write_event(Path(target_dir), record)

    email_cfg = cfg.get("email")
    if isinstance(email_cfg, dict) and email_cfg.get("enabled", True):
        subject = f"{subject_prefix} {record['event']}: {summary}"[:200]
        body_lines = [summary, "", f"event: {record['event']}", f"at: {record['at']}", f"host: {record['host']}"]
        for key, value in record["details"].items():
            body_lines.append(f"{key}: {value}")
        result["email"] = send_email(email_cfg, subject, "\n".join(body_lines))

    if cfg.get("beep"):
        beep()
        result["beep"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Send a test notification through every channel in the config.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--test", action="store_true", help="send one 'test' event (email, event file, beep)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if not cfg:
        print(f"[NOTIFY] No config at {args.config}; copy notify_config.example.json first.")
        return 2
    if not args.test:
        channels = [k for k in ("email", "event_dir", "beep") if cfg.get(k)]
        print(f"[NOTIFY] {args.config}: channels {', '.join(channels) or 'none'}. Use --test to send one.")
        return 0
    result = notify("test", "Test notification from the PAT measurement PC.", config=cfg)
    email_cfg = cfg.get("email")
    if isinstance(email_cfg, dict) and email_cfg.get("enabled", True) and not result.get("email"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
