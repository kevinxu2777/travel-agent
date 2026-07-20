#!/bin/bash
set -e

cd "$(dirname "$0")"

# 个人配置放 .env.local（gitignored）：cp .env.example .env.local 后填入邮箱
if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi

ACCOUNT="${AWARD_WATCH_EMAIL:-${SMTP_USERNAME:-}}"
if [ -z "$ACCOUNT" ]; then
  echo "No email configured. Run: cp .env.example .env.local, then fill in AWARD_WATCH_EMAIL."
  exit 1
fi

export SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"
export SMTP_USERNAME="$ACCOUNT"
export SMTP_FROM="${SMTP_FROM:-$ACCOUNT}"
export ALERT_EMAIL_TO="${ALERT_EMAIL_TO:-$ACCOUNT}"

SERVICE="Market Watch Tool Gmail SMTP"

SMTP_PASSWORD="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w 2>/dev/null || true)"
if [ -z "$SMTP_PASSWORD" ]; then
  echo "No Gmail App Password found in macOS Keychain."
  echo "Run market-watch/setup_gmail_password.command first (same Gmail account is reused here)."
  exit 1
fi
export SMTP_PASSWORD

echo "Sending Award Watch test email..."
python3 award_watch.py --send-test-email
echo "Done."
