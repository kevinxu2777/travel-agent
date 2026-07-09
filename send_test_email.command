#!/bin/bash
set -e

cd "$(dirname "$0")"

export SMTP_HOST="smtp.gmail.com"
export SMTP_USERNAME="kevinxu2777@gmail.com"
export SMTP_FROM="kevinxu2777@gmail.com"
export ALERT_EMAIL_TO="kevinxu2777@gmail.com"

SERVICE="Market Watch Tool Gmail SMTP"
ACCOUNT="kevinxu2777@gmail.com"

SMTP_PASSWORD="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w 2>/dev/null || true)"
if [ -z "$SMTP_PASSWORD" ]; then
  echo "No Gmail App Password found in macOS Keychain."
  echo "Run market-monitor-agent/setup_gmail_password.command first (same Gmail account is reused here)."
  exit 1
fi
export SMTP_PASSWORD

echo "Sending Award Watch test email..."
python3 award_watch.py --send-test-email
echo "Done."
