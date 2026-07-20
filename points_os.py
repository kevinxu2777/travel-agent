#!/usr/bin/env python3
"""Personal points & credits operating system (CLI).

个人点数/信用卡/报销 credit/旅行需求都在本地档案里（profile.json +
credits_catalog.json），这个入口负责回答三件事：

- status:  我现在有什么——点数余额、每张卡的 credit 用了没、哪些快过期
- use:     记一笔 credit 已使用（clear 撤销）
- advise:  盯到的里程票里哪些对我真正可执行——该不该订、从哪转、税费刷哪张卡

它不连接任何银行账户，所有数据手动维护，全部留在本地。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import award_watch

APP_DIR = Path(__file__).resolve().parent
PROFILE_FILE = APP_DIR / "profile.json"
CATALOG_FILE = APP_DIR / "credits_catalog.json"
DEFAULT_DB = APP_DIR / "points_os.sqlite3"
AWARD_DB = APP_DIR / "award_watch.sqlite3"

EXPIRY_WARN_DAYS = 7


def load_profile(path: Path = PROFILE_FILE) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit("profile.json 不存在：先 cp profile.example.json profile.json 并填入真实数据。")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_catalog(path: Path = CATALOG_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"cards": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def init_db(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_usage (
            card TEXT NOT NULL,
            credit_id TEXT NOT NULL,
            cycle TEXT NOT NULL,
            amount REAL,
            used_at TEXT NOT NULL,
            PRIMARY KEY (card, credit_id, cycle)
        )
        """
    )
    conn.commit()
    return conn


def cycle_key(cycle: str, today: date) -> str:
    if cycle == "monthly":
        return f"{today.year}-{today.month:02d}"
    if cycle == "quarterly":
        return f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    if cycle == "semiannual":
        return f"{today.year}-H{1 if today.month <= 6 else 2}"
    return str(today.year)


def cycle_end(cycle: str, today: date) -> date:
    def month_end(year: int, month: int) -> date:
        if month == 12:
            return date(year, 12, 31)
        return date(year, month + 1, 1) - timedelta(days=1)

    if cycle == "monthly":
        return month_end(today.year, today.month)
    if cycle == "quarterly":
        return month_end(today.year, ((today.month - 1) // 3 + 1) * 3)
    if cycle == "semiannual":
        return date(today.year, 6, 30) if today.month <= 6 else date(today.year, 12, 31)
    return date(today.year, 12, 31)


def credit_used(conn: sqlite3.Connection, card: str, credit_id: str, cycle: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM credit_usage WHERE card=? AND credit_id=? AND cycle=?",
        (card, credit_id, cycle),
    ).fetchone()
    return row is not None


def cmd_use(conn: sqlite3.Connection, profile: dict, catalog: dict, card: str, credit_id: str, today: date) -> str:
    entry = find_credit(catalog, card, credit_id)
    key = cycle_key(entry["cycle"], today)
    conn.execute(
        "INSERT OR REPLACE INTO credit_usage (card, credit_id, cycle, amount, used_at) VALUES (?, ?, ?, ?, ?)",
        (card, credit_id, key, entry.get("amount"), today.isoformat()),
    )
    conn.commit()
    return f"已记录：{card}.{credit_id}（{entry['name']}）本周期 {key} 已使用。"


def cmd_clear(conn: sqlite3.Connection, catalog: dict, card: str, credit_id: str, today: date) -> str:
    entry = find_credit(catalog, card, credit_id)
    key = cycle_key(entry["cycle"], today)
    conn.execute(
        "DELETE FROM credit_usage WHERE card=? AND credit_id=? AND cycle=?",
        (card, credit_id, key),
    )
    conn.commit()
    return f"已撤销：{card}.{credit_id} 本周期 {key} 的使用记录。"


def find_credit(catalog: dict, card: str, credit_id: str) -> dict:
    card_entry = catalog.get("cards", {}).get(card)
    if not card_entry:
        raise SystemExit(f"credits_catalog.json 里没有卡片 {card}。可用: {', '.join(catalog.get('cards', {}))}")
    for entry in card_entry.get("credits", []):
        if entry["id"] == credit_id:
            return entry
    ids = ", ".join(e["id"] for e in card_entry.get("credits", []))
    raise SystemExit(f"{card} 没有 credit '{credit_id}'。可用: {ids}")


def unused_airline_fee_credits(conn: sqlite3.Connection, profile: dict, catalog: dict, today: date) -> list[str]:
    tips = []
    for card in profile.get("cards", []):
        card_entry = catalog.get("cards", {}).get(card)
        if not card_entry:
            continue
        for entry in card_entry.get("credits", []):
            if entry.get("tag") != "airline_fees":
                continue
            if not credit_used(conn, card, entry["id"], cycle_key(entry["cycle"], today)):
                tips.append(f"{card_entry.get('display', card)} 的 {entry['name']}（${entry['amount']}）")
    return tips


def build_status(conn: sqlite3.Connection, profile: dict, catalog: dict, today: date) -> str:
    lines = [f"Points & Credits 状态 · {today.isoformat()}", ""]

    lines.append("== 点数钱包 ==")
    partners = award_watch.load_transfer_partners()
    currency_names = partners.get("currencies", {})
    for currency, balance in profile.get("points", {}).items():
        if currency.startswith("_"):
            continue
        lines.append(f"  {currency_names.get(currency, currency):<28} {int(balance):>10,}")
    for program, balance in profile.get("airline_miles", {}).items():
        if program.startswith("_"):
            continue
        display = partners.get("programs", {}).get(program, {}).get("display", program)
        lines.append(f"  {display + ' (航司里程)':<28} {int(balance):>10,}")

    lines += ["", "== 报销 credit（当前周期）=="]
    total_unused = 0.0
    expiring: list[str] = []
    for card in profile.get("cards", []):
        card_entry = catalog.get("cards", {}).get(card)
        if not card_entry:
            lines.append(f"  [{card}] 不在 credits_catalog.json 中，跳过")
            continue
        lines.append(f"  [{card_entry.get('display', card)}]")
        for entry in card_entry.get("credits", []):
            key = cycle_key(entry["cycle"], today)
            used = credit_used(conn, card, entry["id"], key)
            end = cycle_end(entry["cycle"], today)
            days_left = (end - today).days
            mark = "✅ 已用" if used else "⬜ 未用"
            lines.append(
                f"    {mark}  {entry['name']:<24} ${entry['amount']:>6}  周期 {key}（剩 {days_left} 天）  id={entry['id']}"
            )
            if not used:
                total_unused += float(entry.get("amount", 0))
                if days_left <= EXPIRY_WARN_DAYS:
                    expiring.append(f"{card_entry.get('display', card)} {entry['name']} ${entry['amount']}（{days_left} 天后过期）")

    lines += ["", f"本周期未使用 credit 合计: ${total_unused:,.0f}"]
    if expiring:
        lines += ["", "⚠️  即将过期："]
        lines += [f"  - {item}" for item in expiring]
    return "\n".join(lines)


def build_advise(
    conn: sqlite3.Connection,
    profile: dict,
    catalog: dict,
    today: date,
    top: int = 10,
    award_db: Path = AWARD_DB,
) -> str:
    if not award_db.exists():
        raise SystemExit("award_watch.sqlite3 不存在：先跑一次 award_watch.py 抓取放位。")

    prefs = profile.get("preferences", {})
    risk = prefs.get("risk_tolerance", "medium")
    effective = {
        "points": dict(profile.get("points", {})),
        "airline_miles": dict(profile.get("airline_miles", {})),
    }
    for currency, reserve in prefs.get("reserve_points", {}).items():
        if currency.startswith("_"):
            continue
        effective["points"][currency] = max(0, int(effective["points"].get(currency, 0)) - int(reserve))

    partners = award_watch.load_transfer_partners()
    award_conn = sqlite3.connect(award_db)
    rows = award_conn.execute(
        """
        SELECT date, origin, destination, cabin, program, mileage_cost, mileage_cost_raw,
               remaining_seats, airlines, taxes, taxes_currency
        FROM availability WHERE last_seen_available = 1 ORDER BY mileage_cost_raw ASC
        """
    ).fetchall()
    award_conn.close()

    bookable: list[tuple[int, str]] = []
    blocked = 0
    for r in rows:
        hit = award_watch.AvailabilityHit(
            origin=r[1], destination=r[2], date=r[0], cabin=r[3], program=r[4],
            mileage_cost=str(r[5]), mileage_cost_raw=int(r[6] or 0),
            remaining_seats=int(r[7] or 0), airlines=str(r[8]), taxes=int(r[9] or 0),
            taxes_currency=str(r[10]),
        )
        advice = award_watch.build_transfer_advice(hit, effective, partners)
        if not advice.startswith("✔"):
            blocked += 1
            continue
        risk_note = ""
        if risk == "low" and "即时" not in advice:
            risk_note = "  ⚠️ 转点非即时到账，超出你设定的风险偏好(low)"
        line = (
            f"  {hit.date} {hit.origin}→{hit.destination} [{hit.program}] {hit.airlines} "
            f"{hit.mileage_cost_raw:,} 里程 + 税费 {award_watch.format_taxes(hit.taxes, hit.taxes_currency)} "
            f"余位:{award_watch.format_seats(hit.remaining_seats)}\n    {advice}{risk_note}"
        )
        bookable.append((hit.mileage_cost_raw, line))

    lines = [
        f"可执行方案 · {today.isoformat()}（余额已扣除 reserve_points；风险偏好: {risk}）",
        "",
        f"当前监控到 {len(rows)} 条放位，其中 {len(bookable)} 条你的点数可执行，{blocked} 条不可执行已过滤。",
        "",
    ]
    if bookable:
        lines.append(f"== 按里程价排序的前 {min(top, len(bookable))} 条 ==")
        lines += [line for _, line in bookable[:top]]
        tips = unused_airline_fee_credits(conn, profile, catalog, today)
        if tips:
            lines += ["", "💳 税费支付建议：以下未使用的 credit 可覆盖机票税费/杂费（以各卡报销规则为准）："]
            lines += [f"  - {tip}" for tip in tips]
    else:
        lines.append("没有可执行方案。可以考虑降低目标（经济舱/其他日期），或等 award_watch 盯到新放位。")
    lines += ["", "提醒：转点不可逆。先到航司官网核实库存，再转点，立即出票。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal points & credits OS.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="点数余额 + 各卡 credit 使用状态 + 过期预警")
    p_status.add_argument("--email", action="store_true", help="把状态同时发送到配置的邮箱")

    p_use = sub.add_parser("use", help="记录一笔 credit 已使用，如: use amex_platinum uber")
    p_use.add_argument("card")
    p_use.add_argument("credit_id")

    p_clear = sub.add_parser("clear", help="撤销本周期的使用记录")
    p_clear.add_argument("card")
    p_clear.add_argument("credit_id")

    p_advise = sub.add_parser("advise", help="结合钱包和偏好，列出当前真正可执行的里程票方案")
    p_advise.add_argument("--top", type=int, default=10)

    args = parser.parse_args()
    today = date.today()
    profile = load_profile()
    catalog = load_catalog()
    conn = init_db()

    if args.command == "use":
        print(cmd_use(conn, profile, catalog, args.card, args.credit_id, today))
    elif args.command == "clear":
        print(cmd_clear(conn, catalog, args.card, args.credit_id, today))
    elif args.command == "status":
        report = build_status(conn, profile, catalog, today)
        print(report)
        if args.email:
            config = award_watch.load_config(award_watch.DEFAULT_CONFIG)
            sent = award_watch.send_email(config, "Points & Credits 状态报告", report)
            print("\n已发送邮件。" if sent else "\n邮件未发送（SMTP 未配置）。", file=sys.stderr)
    elif args.command == "advise":
        print(build_advise(conn, profile, catalog, today, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
