#!/usr/bin/env python3
"""Business-class mileage award availability monitor.

Polls the seats.aero Partner API for US <-> Japan award space, keeps a local
SQLite record of what has been seen, and sends an email alert whenever new
availability appears (a route/date/program combo that was not bookable on the
previous poll and now is).
"""

from __future__ import annotations

import argparse
import email.message
import html
import json
import os
import random
import smtplib
import ssl
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.example.json"
DEFAULT_DB = APP_DIR / "award_watch.sqlite3"
DEFAULT_DASHBOARD = APP_DIR / "dashboard.html"
DEFAULT_LOG = APP_DIR / "award_watch.log"
USER_AGENT = "AwardWatchTool/1.0 (+local-monitor)"

SEATS_AERO_SEARCH_URL = "https://seats.aero/partnerapi/search"
SEATS_AERO_AUTH_HEADER = "Partner-Authorization"
CABIN_LETTERS = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}
TRANSFER_PARTNERS_FILE = APP_DIR / "transfer_partners.json"
DEFAULT_PROFILE = APP_DIR / "profile.json"
DEFAULT_WALLET = APP_DIR / "wallet.json"  # 旧版钱包文件，向后兼容


@dataclass
class AvailabilityHit:
    origin: str
    destination: str
    date: str
    cabin: str
    program: str
    mileage_cost: str
    mileage_cost_raw: int
    remaining_seats: int
    airlines: str
    taxes: int
    taxes_currency: str

    @property
    def key(self) -> str:
        return f"{self.origin}|{self.destination}|{self.date}|{self.cabin}|{self.program}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def local_now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")


def log_exception(context: str, exc: BaseException) -> None:
    message = f"[{utc_now()}] {context}: {type(exc).__name__}: {exc}"
    print(f"[error] {message}", file=sys.stderr)
    try:
        with DEFAULT_LOG.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def env_value(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def env_values(value: str | None) -> list[str]:
    raw = env_value(value)
    return [item.strip() for item in raw.split(",") if item.strip()]


def ssl_context() -> ssl.SSLContext:
    if os.environ.get("AWARD_WATCH_INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            avail_key TEXT UNIQUE NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            date TEXT NOT NULL,
            cabin TEXT NOT NULL,
            program TEXT NOT NULL,
            mileage_cost TEXT,
            mileage_cost_raw INTEGER,
            remaining_seats INTEGER,
            airlines TEXT,
            taxes INTEGER,
            taxes_currency TEXT,
            last_seen_available INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def fetch_availability_page(cfg: dict[str, Any], cursor: int | None) -> dict[str, Any]:
    seats_cfg = cfg["seats_aero"]
    api_key = env_value(seats_cfg.get("api_key"))
    if not api_key:
        raise SystemExit("seats_aero.api_key is not set (see README for SEATS_AERO_API_KEY).")

    # A trip window (旅行需求) takes precedence over the rolling search window.
    trip = cfg.get("trip", {})
    start_date = trip.get("start_date") or date.today().isoformat()
    end_date = trip.get("end_date") or (
        date.today() + timedelta(days=int(seats_cfg.get("search_window_days", 60)))
    ).isoformat()
    if start_date < date.today().isoformat():
        start_date = date.today().isoformat()
    params = {
        "origin_airport": ",".join(seats_cfg["origins"]),
        "destination_airport": ",".join(seats_cfg["destinations"]),
        "cabins": seats_cfg.get("cabin", "business"),
        "start_date": start_date,
        "end_date": end_date,
        "order_by": "lowest_mileage",
        "take": 1000,
    }
    if seats_cfg.get("carriers"):
        params["carriers"] = ",".join(seats_cfg["carriers"])
    if seats_cfg.get("only_direct_flights"):
        params["only_direct_flights"] = "true"
    if cursor:
        params["cursor"] = cursor

    url = f"{SEATS_AERO_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            SEATS_AERO_AUTH_HEADER: api_key,
        },
    )
    with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_availability(cfg: dict[str, Any], max_pages: int = 40) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: int | None = None
    for _ in range(max_pages):
        page = fetch_availability_page(cfg, cursor)
        results.extend(page.get("data", []))
        if not page.get("hasMore"):
            return results
        cursor = page.get("cursor")
        if not cursor:
            return results
    print(
        f"[warn] Result set truncated after {max_pages} pages ({len(results)} records); "
        "narrow origins/destinations or the date window.",
        file=sys.stderr,
    )
    return results


def load_transfer_partners(path: Path = TRANSFER_PARTNERS_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"currencies": {}, "programs": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_wallet(path: Path | None = None) -> dict[str, Any] | None:
    """Loads the points balances from profile.json (preferred) or the legacy
    wallet.json. Returns None when neither exists."""
    candidates = [path] if path else [DEFAULT_PROFILE, DEFAULT_WALLET]
    for candidate in candidates:
        if candidate and candidate.exists():
            with candidate.open("r", encoding="utf-8") as f:
                return json.load(f)
    return None


def format_seats(remaining: int) -> str:
    return str(remaining) if remaining > 0 else "未知"


def build_transfer_advice(hit: AvailabilityHit, wallet: dict[str, Any] | None, partners: dict[str, Any]) -> str:
    """One-line, per-hit answer to: 我的点数够不够，应该从哪转，转多少."""
    if wallet is None:
        return ""
    needed = hit.mileage_cost_raw
    if needed <= 0:
        return ""

    program = partners.get("programs", {}).get(hit.program)
    currency_names = partners.get("currencies", {})
    display = program.get("display", hit.program) if program else hit.program

    direct = int(wallet.get("airline_miles", {}).get(hit.program, 0) or 0)
    if direct >= needed:
        return f"✔ 直接用 {display} 里程（余额 {direct:,}，需 {needed:,}）"
    remaining_needed = needed - direct

    if program is None or not program.get("transfers"):
        note = (program or {}).get("note", "无美国信用卡转点渠道")
        return f"✘ {note}" + (f"（{display} 直接余额差 {remaining_needed:,}）" if direct else "")

    affordable: list[tuple[int, int, str, str, str]] = []
    best_partial: tuple[int, str] | None = None  # (effective_miles, currency display)
    for currency, rule in program["transfers"].items():
        balance = int(wallet.get("points", {}).get(currency, 0) or 0)
        ratio = float(rule.get("ratio", 1.0))
        if balance <= 0 or ratio <= 0:
            continue
        effective = int(balance * ratio)
        name = currency_names.get(currency, currency)
        if effective >= remaining_needed:
            to_transfer = int(-(-remaining_needed // ratio))  # ceil，比例不足 1:1 时要多转
            instant = 0 if rule.get("time", "") == "即时" else 1
            note = f"；{rule['note']}" if rule.get("note") else ""
            affordable.append((instant, to_transfer, name, rule.get("time", "?"), note))
        elif best_partial is None or effective > best_partial[0]:
            best_partial = (effective, name)

    if affordable:
        affordable.sort()
        _, to_transfer, name, time_text, note = affordable[0]
        prefix = f"先用 {display} 余额 {direct:,}，再" if direct else ""
        more = f"（另有 {len(affordable) - 1} 个可选来源）" if len(affordable) > 1 else ""
        return f"✔ {prefix}从 {name} 转 {to_transfer:,}（{time_text}到账）{more}{note}"

    direct_note = f"已有 {display} 余额 {direct:,}，" if direct else ""
    if best_partial:
        return (
            f"✘ 点数不足：{direct_note}最佳来源 {best_partial[1]} 只能凑 {best_partial[0]:,}，"
            f"还差 {remaining_needed - best_partial[0]:,}"
        )
    return f"✘ 点数不足：{direct_note}无可用余额可转入（需再凑 {remaining_needed:,}）"


def generate_demo_items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Fake seats.aero responses so the full pipeline can run without an API key.

    Each call returns a random subset of route/date combos, so repeated demo
    polls exercise both the "new availability" and "disappeared" paths.
    """
    seats_cfg = cfg["seats_aero"]
    letter = CABIN_LETTERS.get(seats_cfg.get("cabin", "business"), "J")
    origins = seats_cfg["origins"]
    destinations = seats_cfg["destinations"]
    carriers = seats_cfg.get("carriers") or ["NH", "JL", "UA", "AA"]
    window = int(seats_cfg.get("search_window_days", 60))
    programs = ["aeroplan", "united", "american", "alaska", "qantas"]
    rng = random.Random()

    items = []
    for _ in range(rng.randint(6, 14)):
        cost = rng.choice([60000, 70000, 75000, 80000, 85000, 110000])
        items.append(
            {
                "Route": {
                    "OriginAirport": rng.choice(origins),
                    "DestinationAirport": rng.choice(destinations),
                },
                "Date": (date.today() + timedelta(days=rng.randint(7, max(window, 8)))).isoformat(),
                "Source": rng.choice(programs),
                f"{letter}Available": True,
                f"{letter}RemainingSeats": rng.randint(1, 4),
                f"{letter}Airlines": rng.choice(carriers),
                f"{letter}MileageCost": str(cost),
                f"{letter}MileageCostRaw": cost,
                f"{letter}TotalTaxes": rng.randint(600, 60000),
                "TaxesCurrency": rng.choice(["USD", "JPY"]),
            }
        )
    return items


def build_hits(raw_items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[AvailabilityHit]:
    seats_cfg = cfg["seats_aero"]
    cabin = seats_cfg.get("cabin", "business")
    letter = CABIN_LETTERS.get(cabin, "J")
    min_seats = int(seats_cfg.get("min_remaining_seats", 1))
    wanted_carriers = {c.upper() for c in seats_cfg.get("carriers", [])}

    hits = []
    for item in raw_items:
        if not item.get(f"{letter}Available"):
            continue
        # Some programs (notably American) report 0 remaining seats meaning
        # "count unknown" while the space is bookable — only filter when the
        # count is actually known.
        remaining = item.get(f"{letter}RemainingSeats") or 0
        if 0 < remaining < min_seats:
            continue
        airlines = item.get(f"{letter}Airlines", "") or ""
        if wanted_carriers and not (wanted_carriers & {a.strip().upper() for a in airlines.split(",") if a.strip()}):
            continue
        route = item.get("Route", {})
        hits.append(
            AvailabilityHit(
                origin=route.get("OriginAirport", "?"),
                destination=route.get("DestinationAirport", "?"),
                date=item.get("Date", "?"),
                cabin=cabin,
                program=item.get("Source", "?"),
                mileage_cost=item.get(f"{letter}MileageCost", ""),
                mileage_cost_raw=item.get(f"{letter}MileageCostRaw", 0) or 0,
                remaining_seats=remaining,
                airlines=airlines,
                taxes=item.get(f"{letter}TotalTaxes", 0) or 0,
                taxes_currency=item.get("TaxesCurrency", ""),
            )
        )
    return hits


def sync_availability(conn: sqlite3.Connection, hits: list[AvailabilityHit], now: str) -> list[AvailabilityHit]:
    current = {hit.key: hit for hit in hits}
    existing = dict(conn.execute("SELECT avail_key, last_seen_available FROM availability").fetchall())

    new_hits = []
    for key, hit in current.items():
        was_available = existing.get(key)
        cur = conn.execute(
            """
            UPDATE availability SET
                mileage_cost=?,
                mileage_cost_raw=?,
                remaining_seats=?,
                airlines=?,
                taxes=?,
                taxes_currency=?,
                last_seen_available=1,
                last_seen_at=?
            WHERE avail_key=?
            """,
            (
                hit.mileage_cost, hit.mileage_cost_raw, hit.remaining_seats, hit.airlines,
                hit.taxes, hit.taxes_currency, now, key,
            ),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO availability (
                    avail_key, origin, destination, date, cabin, program,
                    mileage_cost, mileage_cost_raw, remaining_seats, airlines,
                    taxes, taxes_currency, last_seen_available, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    key, hit.origin, hit.destination, hit.date, hit.cabin, hit.program,
                    hit.mileage_cost, hit.mileage_cost_raw, hit.remaining_seats, hit.airlines,
                    hit.taxes, hit.taxes_currency, now, now,
                ),
            )
        if was_available is None or was_available == 0:
            new_hits.append(hit)

    disappeared = set(existing.keys()) - set(current.keys())
    for key in disappeared:
        conn.execute(
            "UPDATE availability SET last_seen_available = 0 WHERE avail_key = ? AND last_seen_available = 1",
            (key,),
        )
    conn.commit()
    return new_hits


def build_email_body(
    hits: list[AvailabilityHit],
    wallet: dict[str, Any] | None = None,
    partners: dict[str, Any] | None = None,
) -> tuple[str, str]:
    partners = partners or load_transfer_partners()
    title = f"发现 {len(hits)} 条新的商务舱里程放位"
    text_lines = [title, f"Generated: {local_now_text()}", ""]
    html_parts = [
        "<html><body style=\"margin:0;background:#f6f7f7;font-family:Arial,sans-serif;color:#172022;\">",
        "<div style=\"max-width:960px;margin:0 auto;padding:22px;\">",
        f"<h2 style=\"margin:0 0 6px;\">{html.escape(title)}</h2>",
        f"<p style=\"margin:0 0 18px;color:#5b666a;\">Generated: {html.escape(local_now_text())}</p>",
        "<table cellpadding=\"8\" cellspacing=\"0\" style=\"width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9dfdc;\">",
        "<tr style=\"background:#eef2ef;\"><th align=\"left\">日期</th><th align=\"left\">航线</th>"
        "<th align=\"left\">项目</th><th align=\"left\">承运人</th><th align=\"left\">里程</th>"
        "<th align=\"left\">余位</th><th align=\"left\">税费</th><th align=\"left\">转点建议</th></tr>",
    ]
    for hit in sorted(hits, key=lambda h: h.date):
        advice = build_transfer_advice(hit, wallet, partners)
        text_lines.append(
            f"- {hit.date} {hit.origin}->{hit.destination} [{hit.program}] "
            f"{hit.airlines} 里程:{hit.mileage_cost} 余位:{format_seats(hit.remaining_seats)} "
            f"税费:{hit.taxes} {hit.taxes_currency}"
            + (f" | {advice}" if advice else "")
        )
        advice_color = "#1a7f37" if advice.startswith("✔") else "#9a3412"
        html_parts.append(
            "<tr>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{html.escape(hit.date)}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\"><b>{html.escape(hit.origin)} → {html.escape(hit.destination)}</b></td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{html.escape(hit.program)}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{html.escape(hit.airlines)}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{html.escape(hit.mileage_cost)}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{html.escape(format_seats(hit.remaining_seats))}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;\">{hit.taxes} {html.escape(hit.taxes_currency)}</td>"
            f"<td style=\"border-top:1px solid #e6ebe8;color:{advice_color};\">{html.escape(advice)}</td>"
            "</tr>"
        )
    html_parts.append("</table>")
    footer = (
        "数据来自 seats.aero 缓存，下单前请到航司官网或 seats.aero 上核实实时库存。"
        "转点不可逆：务必先确认库存，再转点，并立即出票。余位“未知”表示该计划不公布数量，不代表没有位。"
    )
    if wallet is None:
        footer += " 提示：复制 wallet.example.json 为 wallet.json 并填入余额，即可在提醒里看到个性化转点建议。"
    text_lines += ["", footer]
    html_parts.append(f"<p style=\"margin-top:16px;color:#5b666a;\">{html.escape(footer)}</p>")
    html_parts.append("</div></body></html>")
    return "\n".join(text_lines), "\n".join(html_parts)


def send_email(config: dict[str, Any], subject: str, text_body: str, html_body: str = "") -> bool:
    if not text_body:
        return False
    email_cfg = config.get("email", {})
    smtp_host = env_value(email_cfg.get("smtp_host"))
    smtp_port = int(env_value(str(email_cfg.get("smtp_port", 587))) or 587)
    username = env_value(email_cfg.get("username"))
    password = env_value(email_cfg.get("password"))
    sender = env_value(email_cfg.get("from")) or username
    recipients: list[str] = []
    for item in email_cfg.get("to", []):
        recipients.extend(env_values(item))
    if not smtp_host or not sender or not recipients:
        print("[warn] Email is not fully configured; alerts saved but not emailed.", file=sys.stderr)
        return False

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        if email_cfg.get("starttls", True):
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)
    return True


def write_dashboard(
    path: Path,
    conn: sqlite3.Connection,
    wallet: dict[str, Any] | None = None,
    partners: dict[str, Any] | None = None,
) -> None:
    partners = partners or load_transfer_partners()
    rows = conn.execute(
        """
        SELECT date, origin, destination, program, airlines, mileage_cost,
               remaining_seats, taxes, taxes_currency, first_seen_at, last_seen_at,
               mileage_cost_raw, cabin
        FROM availability
        WHERE last_seen_available = 1
        ORDER BY date ASC
        """
    ).fetchall()

    def row_advice(r: tuple) -> str:
        hit = AvailabilityHit(
            origin=str(r[1]), destination=str(r[2]), date=str(r[0]), cabin=str(r[12]),
            program=str(r[3]), mileage_cost=str(r[5]), mileage_cost_raw=int(r[11] or 0),
            remaining_seats=int(r[6] or 0), airlines=str(r[4]), taxes=int(r[7] or 0),
            taxes_currency=str(r[8]),
        )
        return build_transfer_advice(hit, wallet, partners)

    table_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(r[0]))}</td>"
        f"<td><b>{html.escape(str(r[1]))} → {html.escape(str(r[2]))}</b></td>"
        f"<td>{html.escape(str(r[3]))}</td>"
        f"<td>{html.escape(str(r[4]))}</td>"
        f"<td>{html.escape(str(r[5]))}</td>"
        f"<td>{html.escape(format_seats(int(r[6] or 0)))}</td>"
        f"<td>{r[7]} {html.escape(str(r[8]))}</td>"
        f"<td>{html.escape(row_advice(r))}</td>"
        f"<td>{html.escape(str(r[9]))}</td>"
        f"<td>{html.escape(str(r[10]))}</td>"
        "</tr>"
        for r in rows
    )

    doc = f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Award Watch Dashboard</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; padding: 28px; background: #f5f6f4; color: #1d2528; }}
    h1 {{ font-size: 22px; }}
    p {{ color: #5d676b; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ text-align: left; padding: 8px; border-top: 1px solid #e6ebe8; }}
    th {{ background: #eef2ef; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111716; color: #e7ece9; }}
      table {{ background: #182220; }}
      th {{ background: #1f2b28; }}
      td, th {{ border-top: 1px solid #26332f; }}
    }}
  </style>
</head>
<body>
  <h1>Award Watch — 美日商务舱里程放位</h1>
  <p>Last updated: {html.escape(local_now_text())} · 当前记录到的可订位次数: {len(rows)}</p>
  <table>
    <tr><th>日期</th><th>航线</th><th>项目</th><th>承运人</th><th>里程</th><th>余位</th><th>税费</th><th>转点建议</th><th>首次发现</th><th>最近确认</th></tr>
    {table_rows}
  </table>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def send_test_email(config: dict[str, Any]) -> bool:
    sample = AvailabilityHit(
        origin="SFO",
        destination="NRT",
        date=(date.today() + timedelta(days=45)).isoformat(),
        cabin="business",
        program="united",
        mileage_cost="70000",
        mileage_cost_raw=70000,
        remaining_seats=2,
        airlines="NH",
        taxes=12000,
        taxes_currency="JPY",
    )
    text_body, html_body = build_email_body([sample], load_wallet(), load_transfer_partners())
    subject = f"[TEST] {config.get('email', {}).get('subject', 'Award Watch Alert')}"
    return send_email(config, subject, text_body, html_body)


def run_once(config: dict[str, Any], conn: sqlite3.Connection, dashboard: Path, demo: bool = False) -> int:
    wallet = load_wallet()
    partners = load_transfer_partners()
    raw_items = generate_demo_items(config) if demo else fetch_all_availability(config)
    hits = build_hits(raw_items, config)
    new_hits = sync_availability(conn, hits, utc_now())
    write_dashboard(dashboard, conn, wallet, partners)

    if new_hits:
        subject = f"{config.get('email', {}).get('subject', 'Award Watch Alert')} ({len(new_hits)})"
        if demo:
            subject = f"[DEMO] {subject}"
        text_body, html_body = build_email_body(new_hits, wallet, partners)
        sent = send_email(config, subject, text_body, html_body)
        print(f"[{utc_now()}] Found {len(new_hits)} new availability, emailed={sent}")
    else:
        print(f"[{utc_now()}] Polled {len(hits)} currently available options, no new availability.")
    return len(new_hits)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor US<->Japan business-class award availability and email alerts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config JSON.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite database.")
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD, help="Path to generated dashboard HTML.")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit.")
    parser.add_argument("--demo", action="store_true", help="Use generated fake availability instead of the seats.aero API (no API key needed).")
    parser.add_argument("--send-test-email", action="store_true", help="Send a sample test email and exit.")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.send_test_email:
        sent = send_test_email(config)
        print("Sent test email." if sent else "Test email was not sent.")
        return 0

    if args.demo:
        # Keep fake rows out of the real database/dashboard unless paths were given explicitly.
        if args.db == DEFAULT_DB:
            args.db = APP_DIR / "award_watch_demo.sqlite3"
        if args.dashboard == DEFAULT_DASHBOARD:
            args.dashboard = APP_DIR / "dashboard_demo.html"
        print("[demo] Using generated fake availability; no seats.aero API key required.")

    conn = init_db(args.db)
    interval = int(config.get("poll_interval_seconds", 1800))
    if args.once:
        run_once(config, conn, args.dashboard, demo=args.demo)
        return 0

    print(f"Award Watch running. Poll interval: {interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(config, conn, args.dashboard, demo=args.demo)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log_exception("Polling cycle failed; monitor will continue", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
