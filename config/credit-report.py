#!/usr/bin/env python3
"""
Monthly credit consumption report for LibreChat.

Usage:
    python config/credit-report.py                  # last 12 months, all users
    python config/credit-report.py --months 6       # last 6 months
    python config/credit-report.py --user <email>   # single user
    python config/credit-report.py --by-model       # break down by model
    python config/credit-report.py --by-user        # break down by user

Reads MONGO_URI from environment or .env file in the repo root.
Requires: pip install pymongo python-dotenv
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    from pymongo import MongoClient
except ImportError:
    print("Missing dependencies. Run:  pip install pymongo python-dotenv")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDITS_PER_USD = 1_000_000  # 1000 tokenCredits = $0.001  →  1M per dollar

# ── Helpers ───────────────────────────────────────────────────────────────────

def credits_to_usd(credits: float) -> str:
    return f"${abs(credits) / CREDITS_PER_USD:.4f}"


def load_mongo_uri() -> str:
    load_dotenv(REPO_ROOT / ".env")
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI not set. Add it to .env or export it as an env var.")
        sys.exit(1)
    return uri


def get_db(uri: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db_name = uri.rstrip("/").split("/")[-1].split("?")[0] or "LibreChat"
    return client[db_name]


def month_label(year: int, month: int) -> str:
    return datetime(year, month, 1).strftime("%Y-%m  (%b %Y)")


# ── Aggregation ───────────────────────────────────────────────────────────────

def build_pipeline(months: int, user_id=None) -> list:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    # Start of the earliest month we care about
    cutoff_month = now.month - months
    cutoff_year = now.year + cutoff_month // 12
    cutoff_month = cutoff_month % 12 or 12
    cutoff = datetime(cutoff_year, cutoff_month, 1, tzinfo=timezone.utc)

    match: dict = {"createdAt": {"$gte": cutoff}}
    if user_id:
        from bson import ObjectId
        match["user"] = ObjectId(user_id)

    return [
        {"$match": match},
        {
            "$group": {
                "_id": {
                    "year":  {"$year":  "$createdAt"},
                    "month": {"$month": "$createdAt"},
                    "tokenType": "$tokenType",
                    "model": "$model",
                    "user": "$user",
                },
                "totalCredits": {"$sum": "$tokenValue"},
                "totalRawTokens": {"$sum": "$rawAmount"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id.year": 1, "_id.month": 1}},
    ]


def fetch_rows(db, months: int, user_id=None) -> list:
    pipeline = build_pipeline(months, user_id)
    return list(db["transactions"].aggregate(pipeline))


# ── Display ───────────────────────────────────────────────────────────────────

def summarize_by_month(rows: list) -> dict:
    """Returns {(year, month): {spend_credits, topup_credits, raw_tokens, requests}}"""
    months: dict = defaultdict(lambda: {"spend": 0, "topup": 0, "raw_tokens": 0, "requests": 0})
    for row in rows:
        key = (row["_id"]["year"], row["_id"]["month"])
        token_type = row["_id"]["tokenType"]
        credits = row["totalCredits"] or 0
        if token_type == "credits":
            months[key]["topup"] += credits
        else:
            months[key]["spend"] += abs(credits)
        months[key]["raw_tokens"] += row.get("totalRawTokens") or 0
        months[key]["requests"] += row["count"]
    return months


def print_monthly_summary(rows: list) -> None:
    data = summarize_by_month(rows)
    if not data:
        print("No transactions found.")
        return

    header = f"{'Month':<20} {'Tokens':>14} {'Credits Spent':>16} {'USD Equiv':>12} {'Top-ups':>14} {'Requests':>10}"
    print("\n" + header)
    print("─" * len(header))

    total_spend = total_topup = total_raw = total_req = 0
    for (year, month) in sorted(data):
        d = data[(year, month)]
        print(
            f"{month_label(year, month):<20}"
            f"{d['raw_tokens']:>14,}"
            f"{d['spend']:>16,.0f}"
            f"{credits_to_usd(d['spend']):>12}"
            f"{d['topup']:>14,.0f}"
            f"{d['requests']:>10,}"
        )
        total_spend  += d["spend"]
        total_topup  += d["topup"]
        total_raw    += d["raw_tokens"]
        total_req    += d["requests"]

    print("─" * len(header))
    print(
        f"{'TOTAL':<20}"
        f"{total_raw:>14,}"
        f"{total_spend:>16,.0f}"
        f"{credits_to_usd(total_spend):>12}"
        f"{total_topup:>14,.0f}"
        f"{total_req:>10,}"
    )


def print_by_model(rows: list) -> None:
    """Aggregate spend by (month, model)."""
    data: dict = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "raw_tokens": 0}))
    for row in rows:
        if row["_id"]["tokenType"] == "credits":
            continue
        key = (row["_id"]["year"], row["_id"]["month"])
        model = row["_id"].get("model") or "unknown"
        data[key][model]["spend"] += abs(row["totalCredits"] or 0)
        data[key][model]["raw_tokens"] += row.get("totalRawTokens") or 0

    for (year, month) in sorted(data):
        print(f"\n── {month_label(year, month)} ──")
        print(f"  {'Model':<40} {'Tokens':>12} {'Credits':>14} {'USD':>10}")
        print(f"  {'─'*40} {'─'*12} {'─'*14} {'─'*10}")
        for model, d in sorted(data[(year, month)].items(), key=lambda x: -x[1]["spend"]):
            print(
                f"  {model:<40}"
                f"{d['raw_tokens']:>12,}"
                f"{d['spend']:>14,.0f}"
                f"{credits_to_usd(d['spend']):>10}"
            )


def print_by_user(rows: list, db) -> None:
    """Aggregate spend by (month, user), resolving emails."""
    from bson import ObjectId

    user_ids: set = {row["_id"]["user"] for row in rows if row["_id"].get("user")}
    users = {
        str(u["_id"]): u.get("email", str(u["_id"]))
        for u in db["users"].find({"_id": {"$in": list(user_ids)}}, {"email": 1})
    }

    data: dict = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "raw_tokens": 0}))
    for row in rows:
        if row["_id"]["tokenType"] == "credits":
            continue
        key = (row["_id"]["year"], row["_id"]["month"])
        uid = str(row["_id"].get("user", ""))
        email = users.get(uid, uid or "unknown")
        data[key][email]["spend"] += abs(row["totalCredits"] or 0)
        data[key][email]["raw_tokens"] += row.get("totalRawTokens") or 0

    for (year, month) in sorted(data):
        print(f"\n── {month_label(year, month)} ──")
        print(f"  {'User':<40} {'Tokens':>12} {'Credits':>14} {'USD':>10}")
        print(f"  {'─'*40} {'─'*12} {'─'*14} {'─'*10}")
        for email, d in sorted(data[(year, month)].items(), key=lambda x: -x[1]["spend"]):
            print(
                f"  {email:<40}"
                f"{d['raw_tokens']:>12,}"
                f"{d['spend']:>14,.0f}"
                f"{credits_to_usd(d['spend']):>10}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LibreChat monthly credit consumption report")
    parser.add_argument("--months",   type=int, default=12,  help="How many months back to include (default: 12)")
    parser.add_argument("--user",     type=str, default=None, help="Filter to a single user email")
    parser.add_argument("--by-model", action="store_true",   help="Break down by model within each month")
    parser.add_argument("--by-user",  action="store_true",   help="Break down by user within each month")
    args = parser.parse_args()

    uri = load_mongo_uri()
    db = get_db(uri)

    user_id = None
    if args.user:
        user_doc = db["users"].find_one({"email": args.user}, {"_id": 1})
        if not user_doc:
            print(f"User not found: {args.user}")
            sys.exit(1)
        user_id = user_doc["_id"]

    rows = fetch_rows(db, args.months, user_id)

    title = f"LibreChat Credit Consumption — last {args.months} months"
    if args.user:
        title += f"  [{args.user}]"
    print(f"\n{'═' * len(title)}")
    print(title)
    print(f"{'═' * len(title)}")

    print_monthly_summary(rows)

    if args.by_model:
        print("\n\nBreakdown by model:")
        print_by_model(rows)

    if args.by_user:
        print("\n\nBreakdown by user:")
        print_by_user(rows, db)


if __name__ == "__main__":
    main()
