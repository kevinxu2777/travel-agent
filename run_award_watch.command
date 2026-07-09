#!/bin/bash
set -e

cd "$(dirname "$0")"

touch award_watch.log
exec > >(tee -a award_watch.log) 2>&1

export SMTP_HOST="smtp.gmail.com"
export SMTP_USERNAME="kevinxu2777@gmail.com"
export SMTP_FROM="kevinxu2777@gmail.com"
export ALERT_EMAIL_TO="kevinxu2777@gmail.com"

SERVICE="Market Watch Tool Gmail SMTP"
ACCOUNT="kevinxu2777@gmail.com"

echo "Award Watch Tool"
SMTP_PASSWORD="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w 2>/dev/null || true)"
if [ -z "$SMTP_PASSWORD" ]; then
  echo "No Gmail App Password found in macOS Keychain."
  echo "Run market-monitor-agent/setup_gmail_password.command first (same Gmail account is reused here)."
  exit 1
fi
export SMTP_PASSWORD

if [ -z "$SEATS_AERO_API_KEY" ]; then
  SEATS_AERO_API_KEY="$(security find-generic-password -a "$ACCOUNT" -s "Award Watch seats.aero API" -w 2>/dev/null || true)"
fi
if [ -z "$SEATS_AERO_API_KEY" ]; then
  echo "No seats.aero API key found in macOS Keychain or environment."
  echo "Save it once with:"
  echo "  security add-generic-password -a \"$ACCOUNT\" -s \"Award Watch seats.aero API\" -w YOUR_KEY -U"
  exit 1
fi
export SEATS_AERO_API_KEY

echo "Starting monitor. Press Ctrl+C to stop."
python3 award_watch.py
