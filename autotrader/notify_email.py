#!/usr/bin/env python3
"""
notify_email.py — sends GreenScreen notifications to email, alongside the
existing macOS desktop notifications. Used by run_cycle.sh and
run_outcome_review.sh so updates reach you even when you're not at this
Mac to see the notification banner.

Usage: python3 notify_email.py "subject" "body text"

Auth: a Gmail App Password (not the real account password) read from
autotrader/state/gmail_app_password - gitignored, chmod 600, same
handling as every other credential in this project.
"""

import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
APP_PASSWORD_FILE = STATE_DIR / "gmail_app_password"
GMAIL_ADDRESS = "noventraecommercellc@gmail.com"


def send_email(subject, body):
    if not APP_PASSWORD_FILE.exists():
        print(f"notify_email: no app password file at {APP_PASSWORD_FILE} - skipping email", file=sys.stderr)
        return False

    app_password = APP_PASSWORD_FILE.read_text().strip()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, app_password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"notify_email: failed to send - {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 notify_email.py \"subject\" \"body\"", file=sys.stderr)
        sys.exit(1)
    ok = send_email(sys.argv[1], sys.argv[2])
    sys.exit(0 if ok else 1)
