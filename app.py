"""
current - a local budgeting app.

Run it:
    pip install flask
    python3 app.py

Then open http://127.0.0.1:5000 in your browser.

Everything you enter is saved to a file called current.db that lives
right next to this script. Nothing leaves your machine.
"""

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

# The database file sits next to this script so it is easy to find and back up.
DB_PATH = Path(__file__).parent / "current.db"

# This is what a brand new, empty setup looks like. It is what you see the
# first time you open the app, before you have entered anything.
DEFAULT_STATE = {
    "income": [],       # [{id, source, amount, frequency}]
    "expenses": [],     # [{id, category, name, amount, frequency}]
    "debts": [],        # [{id, name, balance, apr, minPayment, type}]
    "transactions": [], # [{id, date, description, category, method, amount}]
    "profile": {
        "creditScore": None,
        "savingsGoal": 20,   # target savings rate, as a percent of income
        "efMonths": 3,       # emergency fund size, in months of costs
        "savings": 0,        # what is already in savings today
        "breathingPct": 10,  # share of surplus to leave unallocated, just in case
    },
}


# ----------------------------------------------------------------------------
# Storage: a single row of JSON in SQLite. Simple, and plenty for one person.
# ----------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)"
        )
        # One row per calendar month, so we can show real month-over-month change.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots (month TEXT PRIMARY KEY, data TEXT)"
        )
        row = conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO state (id, data) VALUES (1, ?)",
                (json.dumps(DEFAULT_STATE),),
            )


def load_state():
    with get_db() as conn:
        row = conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        if row is None:
            return json.loads(json.dumps(DEFAULT_STATE))
        return json.loads(row["data"])


def save_state(state):
    with get_db() as conn:
        conn.execute(
            "UPDATE state SET data = ? WHERE id = 1", (json.dumps(state),)
        )


def current_month():
    return datetime.now().strftime("%Y-%m")


def save_snapshot(totals):
    """Record this month's headline numbers so trends can be shown later."""
    keep = {k: totals.get(k) for k in ("income", "expenses", "netFlow", "debt", "savingsRate")}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO snapshots (month, data) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET data = excluded.data",
            (current_month(), json.dumps(keep)),
        )


def prior_snapshot():
    """The most recent snapshot from a month before the current one."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT data FROM snapshots WHERE month < ? ORDER BY month DESC LIMIT 1",
            (current_month(),),
        ).fetchone()
        return json.loads(row["data"]) if row else None


# ----------------------------------------------------------------------------
# The money logic. This is the part that tries to be useful, not just a list.
# All of it is plain arithmetic so you can read it and trust it.
# ----------------------------------------------------------------------------

def _num(value, default=0.0):
    """Turn whatever came from the form into a safe number."""
    try:
        n = float(value)
        return n if n == n else default  # guard against NaN
    except (TypeError, ValueError):
        return default


# How many times each pay frequency lands in an average month. Weekly and
# biweekly are not simply 4 and 2, so we spread them across the year: 52 weeks
# and 26 biweekly cycles, divided by 12 months.
FREQ_TO_MONTHLY = {
    "weekly": 52 / 12,
    "biweekly": 26 / 12,
    "semimonthly": 2,   # twice a month
    "monthly": 1,
}


def to_monthly(amount, frequency):
    """Convert an amount at a given pay/bill frequency into a monthly figure."""
    return _num(amount) * FREQ_TO_MONTHLY.get(frequency or "monthly", 1)


def monthly_income(item):
    """Convert one income entry to what it actually brings in per month."""
    return to_monthly(item.get("amount"), item.get("frequency"))


def monthly_expense(item):
    """Convert one expense entry to what it actually costs per month."""
    return to_monthly(item.get("amount"), item.get("frequency"))


def simulate_payoff(debts, monthly_budget, order):
    """
    Walk month by month and pay these debts off.

    order = "avalanche" pays the highest interest rate first (saves the most money).
    order = "snowball"  pays the smallest balance first (fastest wins, better momentum).

    Returns months to debt free and total interest paid. If the budget cannot
    even cover the interest, we say it never gets paid off.
    """
    # Work on copies so we never touch the real data.
    working = [
        {
            "balance": _num(d.get("balance")),
            # A loan or money owed is treated as no interest, whatever is stored.
            "apr": 0.0 if d.get("type") == "loan" else _num(d.get("apr")),
            "min": _num(d.get("minPayment")),
        }
        for d in debts
        if _num(d.get("balance")) > 0
    ]
    if not working:
        return {"months": 0, "interest": 0.0, "paysOff": True}

    total_interest = 0.0
    months = 0
    cap = 1200  # 100 years. If we hit this, the budget is too small.

    while any(d["balance"] > 0 for d in working) and months < cap:
        months += 1

        # 1. Interest accrues on every balance.
        for d in working:
            if d["balance"] > 0:
                monthly_rate = d["apr"] / 100 / 12
                interest = d["balance"] * monthly_rate
                d["balance"] += interest
                total_interest += interest

        # 2. Pay the minimum on everything first.
        budget = monthly_budget
        for d in working:
            if d["balance"] > 0:
                pay = min(d["min"], d["balance"], budget)
                d["balance"] -= pay
                budget -= pay

        # 3. Whatever is left attacks one target, chosen by strategy.
        remaining = [d for d in working if d["balance"] > 0]
        if order == "avalanche":
            remaining.sort(key=lambda d: d["apr"], reverse=True)
        else:  # snowball
            remaining.sort(key=lambda d: d["balance"])

        for d in remaining:
            if budget <= 0:
                break
            pay = min(budget, d["balance"])
            d["balance"] -= pay
            budget -= pay

    pays_off = all(d["balance"] <= 0.01 for d in working)
    return {
        "months": months if pays_off else None,
        "interest": round(total_interest, 2),
        "paysOff": pays_off,
    }


def credit_band(score):
    if score is None:
        return None
    if score >= 800:
        return "exceptional"
    if score >= 740:
        return "very good"
    if score >= 670:
        return "good"
    if score >= 580:
        return "fair"
    return "poor"


def credit_tips(score):
    band = credit_band(score)
    if band is None:
        return []
    base = [
        "Keep every card under 30 percent of its limit, under 10 is better.",
        "Never miss a due date. Payment history is the biggest single factor.",
    ]
    if band in ("poor", "fair"):
        return base + [
            "Do not close old cards. Age of accounts helps you.",
            "One late payment can undo months of progress, so automate the minimums.",
        ]
    if band == "good":
        return base + [
            "Ask for a limit increase on a card you handle well. It lowers your usage ratio.",
        ]
    return base + [
        "You are in strong shape. Hold your usage low and let account age do the rest.",
    ]


def build_plan(totals, expenses, debts, profile):
    """
    Turn the numbers into an ordered game plan for the monthly surplus.

    The order is a standard budgeting sequence, not investment advice:
    a one-month starter cushion, then high-interest debt, then a full
    emergency fund, then growing savings. A slice of the surplus is always
    left unallocated as breathing room.
    """
    surplus = totals["netFlow"]
    monthly_cost = totals["expenses"] + totals["minDebt"]
    ef_months = int(_num(profile.get("efMonths"), 3)) or 3
    breathing_pct = min(50.0, max(0.0, _num(profile.get("breathingPct"), 10)))
    current_savings = _num(profile.get("savings"), 0)

    if surplus <= 0:
        if totals["income"] <= 0:
            return {
                "ok": False,
                "surplus": round(surplus, 2),
                "headline": "Your plan builds itself here.",
                "detail": "Add your income and expenses on the left, and a step-by-step plan for "
                          "your surplus appears in this spot.",
            }
        return {
            "ok": False,
            "surplus": round(surplus, 2),
            "headline": "Your plan starts with closing the monthly gap.",
            "detail": (
                f"Right now you spend about ${abs(surplus):,.0f} more than you bring in each "
                "month, so there is no surplus to put to work yet. Trimming your biggest "
                "category is the first move, and the plan opens up the moment net flow turns positive."
            ),
        }

    breathing = round(surplus * breathing_pct / 100, 2)
    allocatable = max(0.0, round(surplus - breathing, 2))

    starter_target = round(monthly_cost, 2)            # one month of costs
    full_target = round(ef_months * monthly_cost, 2)   # full cushion

    hi_debt = sum(
        _num(d.get("balance"))
        for d in debts
        if d.get("type") != "loan" and _num(d.get("apr")) >= 8 and _num(d.get("balance")) > 0
    )

    def months_for(amount):
        if allocatable <= 0 or amount <= 0:
            return None
        return math.ceil(amount / allocatable)

    steps = [
        {"title": "Starter cushion", "target": starter_target,
         "done": current_savings >= starter_target,
         "note": f"One month of costs, about ${starter_target:,.0f}."},
        {"title": "Clear high-interest debt", "target": round(hi_debt, 2),
         "done": hi_debt <= 0,
         "note": (f"Cards at 8% or more, about ${hi_debt:,.0f}." if hi_debt > 0
                  else "No high-interest debt. Nothing to clear.")},
        {"title": f"{ef_months}-month emergency fund", "target": full_target,
         "done": current_savings >= full_target,
         "note": f"Bring savings up to about ${full_target:,.0f}."},
        {"title": "Grow your savings", "target": None, "done": False,
         "note": "Send the surplus to savings or long-term investing that fits your goals."},
    ]

    current_idx = next((i for i, s in enumerate(steps) if not s["done"]), len(steps) - 1)
    for i, s in enumerate(steps):
        s["status"] = "done" if s["done"] else ("current" if i == current_idx else "upcoming")

    buckets = {"emergencyFund": 0.0, "debt": 0.0, "savings": 0.0, "breathingRoom": breathing}

    if current_idx == 0:
        buckets["emergencyFund"] = allocatable
        m = months_for(max(0, starter_target - current_savings))
        headline = "Build a starter cushion first."
        detail = (f"Put ${allocatable:,.0f} a month into savings until you have a one-month "
                  f"cushion of ${starter_target:,.0f}."
                  + (f" About {m} month{'s' if m != 1 else ''} to go." if m else ""))
    elif current_idx == 1:
        buckets["debt"] = allocatable
        sim = simulate_payoff([d for d in debts if d.get("type") != "loan"],
                              totals["minDebt"] + allocatable, "avalanche")
        m = sim["months"]
        headline = "Now crush the high-interest debt."
        detail = (f"Add ${allocatable:,.0f} a month on top of the minimums, aimed at your "
                  "highest-rate card first."
                  + (f" That clears the cards in about {m} months." if m else ""))
    elif current_idx == 2:
        buckets["emergencyFund"] = allocatable
        m = months_for(max(0, full_target - current_savings))
        headline = "Top up to a full emergency fund."
        detail = (f"Keep adding ${allocatable:,.0f} a month to savings until you reach "
                  f"${full_target:,.0f}, your {ef_months}-month cushion."
                  + (f" About {m} month{'s' if m != 1 else ''} to go." if m else ""))
    else:
        buckets["savings"] = allocatable
        headline = "You are set up. Now grow it."
        detail = (f"Debts are handled and your cushion is full. Send ${allocatable:,.0f} a month "
                  "to savings or long-term investing, whatever fits your goals.")

    return {
        "ok": True,
        "surplus": round(surplus, 2),
        "breathing": breathing,
        "allocatable": allocatable,
        "buckets": {k: round(v, 2) for k, v in buckets.items()},
        "steps": steps,
        "currentIndex": current_idx,
        "headline": headline,
        "detail": detail,
        "efMonths": ef_months,
    }


# Categories that count as needs for the 50/30/20 rule. Everything else, plus
# blank, counts as a want. Debt minimums are treated as a need (an obligation).
NEEDS_CATEGORIES = {"Housing", "Utilities", "Food", "Transport", "Health", "Insurance", "Kids & Pets"}


def build_rule_503020(total_income, expenses, total_min_debt, net_flow):
    """The classic 50/30/20 rule: 50% needs, 30% wants, 20% savings."""
    needs = total_min_debt
    wants = 0.0
    for e in expenses:
        amt = monthly_expense(e)
        if (e.get("category") or "") in NEEDS_CATEGORIES:
            needs += amt
        else:
            wants += amt
    savings = max(0.0, net_flow)
    return {
        "income": round(total_income, 2),
        "needs": {"actual": round(needs, 2), "target": round(total_income * 0.5, 2)},
        "wants": {"actual": round(wants, 2), "target": round(total_income * 0.3, 2)},
        "savings": {"actual": round(savings, 2), "target": round(total_income * 0.2, 2)},
    }


def build_income_sources(income):
    total = sum(monthly_income(i) for i in income) or 1
    rows = [
        {"source": i.get("source") or "Income", "amount": round(monthly_income(i), 2),
         "share": round(monthly_income(i) / total * 100)}
        for i in income if monthly_income(i) > 0
    ]
    return sorted(rows, key=lambda r: r["amount"], reverse=True)


def build_spectrum(category_breakdown, total_min_debt, leftover):
    """The wide equalizer: every place a monthly dollar goes, side by side."""
    bars = [{"label": c["category"], "amount": c["amount"]} for c in category_breakdown]
    if total_min_debt > 0:
        bars.append({"label": "Debt Repayment", "amount": round(total_min_debt, 2)})
    if leftover > 0:
        bars.append({"label": "Savings", "amount": round(leftover, 2)})
    return bars


def build_month_tracking(expenses, transactions):
    """Actual spend logged this month, against the monthly budget per category."""
    month = current_month()
    budget_by_cat = {}
    for e in expenses:
        cat = e.get("category") or "Misc"
        budget_by_cat[cat] = budget_by_cat.get(cat, 0.0) + monthly_expense(e)

    spent_by_cat = {}
    spent = 0.0
    count = 0
    for t in transactions:
        date = str(t.get("date") or "")
        if date.startswith(month):
            amt = _num(t.get("amount"))
            cat = t.get("category") or "Misc"
            spent_by_cat[cat] = spent_by_cat.get(cat, 0.0) + amt
            spent += amt
            count += 1

    cats = sorted(set(list(budget_by_cat) + list(spent_by_cat)))
    rows = []
    for c in cats:
        b = round(budget_by_cat.get(c, 0.0), 2)
        s = round(spent_by_cat.get(c, 0.0), 2)
        if b == 0 and s == 0:
            continue
        rows.append({"category": c, "budget": b, "spent": s, "over": s > b and b > 0})
    rows.sort(key=lambda r: r["spent"], reverse=True)

    total_budget = round(sum(budget_by_cat.values()), 2)
    return {
        "month": month,
        "spent": round(spent, 2),
        "budget": total_budget,
        "count": count,
        "byCategory": rows,
    }


def simulate_credit(score, levers):
    """
    A rough, transparent credit-score estimator. Not FICO, just a plain model
    of the well-known factors so you can see the direction each move pushes you.
    Each lever is a boolean the user toggles in the simulator.
    """
    base = int(score) if score not in (None, "") else 650
    deltas = [
        ("payOnTime", 40, "Pay every bill on time for 6 months"),
        ("lowerUtilization", 35, "Get every card under 30% used (under 10% is better)"),
        ("noNewLatePayments", 25, "No new late payments or collections"),
        ("keepOldCards", 15, "Keep your oldest cards open"),
        ("raiseLimit", 12, "Request a limit increase on a card you handle well"),
        ("fewerInquiries", 8, "Avoid new hard inquiries for a while"),
        ("creditMix", 10, "Add one different account type (e.g. a small installment loan)"),
    ]
    gain = 0
    applied = []
    for key, pts, label in deltas:
        on = bool(levers.get(key))
        applied.append({"key": key, "points": pts, "label": label, "on": on})
        if on:
            gain += pts
    projected = max(300, min(850, base + gain))
    return {
        "base": base,
        "projected": projected,
        "gain": projected - base,
        "levers": applied,
        "band": credit_band(projected),
    }


# Score bands and the everyday products they tend to unlock, so the guidance is
# concrete rather than vague. General ranges, not guarantees.
CREDIT_MILESTONES = [
    (500, "Secured cards and starter cards"),
    (620, "Most basic unsecured cards, some auto loans"),
    (670, "Most rewards cards and better auto rates"),
    (740, "Premium cards and the best loan rates"),
    (800, "Top-tier approvals and lowest rates"),
]


def build_credit_growth(score, total_income, savings_rate, debts):
    """Concrete, prioritized steps toward approvals and stronger finances."""
    band = credit_band(score)
    steps = []

    # Credit-approval track
    if score is None:
        steps.append(("Add your credit score", "Enter it in Your money so this can tailor the path."))
    else:
        nxt = next((m for m in CREDIT_MILESTONES if m[0] > score), None)
        if nxt:
            steps.append((f"Reach {nxt[0]}", f"Unlocks: {nxt[1]}. You are {nxt[0] - int(score)} points away."))
        util_cards = [d for d in debts if d.get("type") != "loan" and _num(d.get("balance")) > 0]
        if util_cards:
            steps.append(("Pay cards below 30% used",
                          "Utilization is the fastest lever. Paying balances down shows up within a cycle or two."))
        steps.append(("Automate every minimum",
                      "One on-time streak of 6+ months moves the needle more than almost anything else."))
        if band in ("poor", "fair"):
            steps.append(("Consider a secured card or credit-builder loan",
                          "Both add positive history without much risk while your score climbs."))

    # Income-growth track (general, not personalized investment advice)
    income_moves = [
        ("Stabilize before you scale", "Lenders like steady, documented income. Keep pay stubs and tax records clean."),
        ("Add one income stream", "A small recurring side income raises approval odds and your savings rate."),
        ("Raise your savings rate", f"You are saving about {savings_rate:.0f}%. Every point higher is buffer and leverage."),
    ]
    if total_income == 0:
        income_moves = [("Add your income", "Enter it so this can measure your savings rate and progress.")]

    return {
        "band": band,
        "milestones": [{"score": s, "unlocks": u, "reached": (score is not None and score >= s)} for s, u in CREDIT_MILESTONES],
        "creditSteps": [{"title": t, "detail": d} for t, d in steps],
        "incomeSteps": [{"title": t, "detail": d} for t, d in income_moves],
    }


def build_debt_freedom(debts, total_min_debt, net_flow):
    """
    A clearer replacement for the payoff comparison: one honest picture of when
    you are debt free and what the surplus buys you.
    """
    total_debt = sum(_num(d.get("balance")) for d in debts if _num(d.get("balance")) > 0)
    if total_debt <= 0:
        return {"hasDebt": False}

    extra = max(0.0, net_flow)
    with_extra = simulate_payoff(debts, total_min_debt + extra, "avalanche")
    mins_only = simulate_payoff(debts, total_min_debt, "avalanche")

    # Interest saved by throwing the surplus at debt instead of only paying minimums.
    saved = None
    if with_extra["paysOff"] and mins_only["paysOff"]:
        saved = round(mins_only["interest"] - with_extra["interest"], 2)

    biggest = max(
        (d for d in debts if _num(d.get("balance")) > 0),
        key=lambda d: _num(d.get("balance")),
        default=None,
    )
    return {
        "hasDebt": True,
        "totalDebt": round(total_debt, 2),
        "extra": round(extra, 2),
        "monthsWithExtra": with_extra["months"],
        "monthsMinsOnly": mins_only["months"],
        "interestWithExtra": with_extra["interest"],
        "interestSaved": saved,
        "target": {"name": (biggest.get("name") if biggest else ""),
                   "balance": round(_num(biggest.get("balance")), 2) if biggest else 0,
                   "apr": (0 if (biggest and biggest.get("type") == "loan") else _num(biggest.get("apr"))) if biggest else 0},
    }


def build_trend(totals):
    """Signed percent change versus the most recent prior month, if any."""
    prev = prior_snapshot()
    if not prev:
        return {"income": None, "expenses": None, "savings": None}

    def pct(now, before):
        before = _num(before)
        if before == 0:
            return None
        return round((now - before) / abs(before) * 100, 1)

    return {
        "income": pct(totals["income"], prev.get("income")),
        "expenses": pct(totals["expenses"], prev.get("expenses")),
        "savings": pct(totals["netFlow"], prev.get("netFlow")),
    }


def payoff_schedule(debts, monthly_budget, order):
    """Like simulate_payoff, but also records the month each debt hits zero."""
    working = []
    for d in debts:
        bal = _num(d.get("balance"))
        if bal <= 0:
            continue
        working.append({
            "name": d.get("name") or "Debt",
            "balance": bal,
            "apr": 0.0 if d.get("type") == "loan" else _num(d.get("apr")),
            "min": _num(d.get("minPayment")),
            "clear": None,
        })
    if not working:
        return {"months": 0, "interest": 0.0, "paysOff": True, "schedule": []}

    total_interest = 0.0
    month = 0
    cap = 1200
    while any(w["balance"] > 0 for w in working) and month < cap:
        month += 1
        for w in working:
            if w["balance"] > 0:
                intr = w["balance"] * (w["apr"] / 100 / 12)
                w["balance"] += intr
                total_interest += intr
        budget = monthly_budget
        for w in working:
            if w["balance"] > 0:
                pay = min(w["min"], w["balance"], budget)
                w["balance"] -= pay
                budget -= pay
        rem = [w for w in working if w["balance"] > 0]
        rem.sort(key=lambda w: w["apr"], reverse=True) if order == "avalanche" else rem.sort(key=lambda w: w["balance"])
        for w in rem:
            if budget <= 0:
                break
            pay = min(budget, w["balance"])
            w["balance"] -= pay
            budget -= pay
        for w in working:
            if w["balance"] <= 0.01 and w["clear"] is None:
                w["clear"] = month

    pays = all(w["balance"] <= 0.01 for w in working)
    schedule = sorted(
        ({"name": w["name"], "clearMonth": w["clear"]} for w in working),
        key=lambda x: (x["clearMonth"] is None, x["clearMonth"] or 0),
    )
    return {"months": month if pays else None, "interest": round(total_interest, 2),
            "paysOff": pays, "schedule": schedule}


def month_plus(n):
    """A friendly 'Mon YYYY' label n months from now."""
    now = datetime.now()
    idx = now.month - 1 + n
    year = now.year + idx // 12
    month = idx % 12 + 1
    return datetime(year, month, 1).strftime("%b %Y")


def build_debt_free(debts, total_min_debt, net_flow):
    """One clear answer: when are you debt free, and what clears in what order."""
    extra = max(0.0, net_flow)
    budget = total_min_debt + extra
    sched = payoff_schedule(debts, budget, "avalanche")
    faster = None
    if sched["paysOff"] and sched["months"]:
        sched["date"] = month_plus(sched["months"])
        # what an extra $100 a month would do, as a nudge
        bump = payoff_schedule(debts, budget + 100, "avalanche")
        if bump["paysOff"] and bump["months"] and bump["months"] < sched["months"]:
            faster = {"extra": 100, "monthsSaved": sched["months"] - bump["months"],
                      "interestSaved": round(sched["interest"] - bump["interest"], 2)}
    else:
        sched["date"] = None
    sched["faster"] = faster
    sched["extraPerMonth"] = round(extra, 2)
    return sched


def build_coaching(totals, credit, debts, income):
    """Plain guidance for two goals: get approved for credit, and grow income."""
    income_m = totals["income"]
    min_debt = totals["minDebt"]
    dti = round(min_debt / income_m * 100, 1) if income_m > 0 else None
    band = credit.get("band")

    approval = []
    if dti is not None:
        if dti > 36:
            approval.append(f"Your debt payments are {dti:.0f}% of income. Lenders want this under 36%. "
                            "Bringing it down is the single biggest lever on approval odds.")
        else:
            approval.append(f"Your debt-to-income is {dti:.0f}%, under the 36% lenders look for. That works in your favor.")
    else:
        approval.append("Add your income and debts so we can gauge your debt-to-income, a top factor lenders check.")

    if band in ("poor", "fair") or band is None:
        approval.append("Target a score of 670 or higher. Below that, approvals get harder and interest rates climb.")
        approval.append("A secured card or a credit-builder loan reports on-time history and can lift a thin or low file fast.")
    else:
        approval.append("Your score is in healthy territory. Keep usage low and apply sparingly to protect it.")

    approval.append("Get card balances under 30% of the limit, under 10% is better, in the month before you apply.")
    approval.append("Space applications out. Each hard inquiry dents your score for a few months, so don't shotgun them.")

    grow = []
    if len(income) <= 1:
        grow.append("You have one income source. A second stream, even a small recurring one, both raises income and "
                    "makes lenders more comfortable.")
    else:
        grow.append("More than one income stream strengthens both your cash flow and your loan applications.")
    grow.append("Document everything. Lenders weigh provable income, so keep pay stubs, 1099s, and bank deposits organized.")
    grow.append("Ask for a raise with numbers, market rate plus your specific wins, not a vibe. That is the case that gets a yes.")
    grow.append("Turn a skill you already have into recurring income before chasing one-off gigs. Recurring is what compounds.")

    return {"dti": dti, "approval": approval, "income": grow}


def build_insights(state):
    income = state.get("income", [])
    expenses = state.get("expenses", [])
    debts = state.get("debts", [])
    transactions = state.get("transactions", [])
    profile = state.get("profile", {})

    total_income = sum(monthly_income(i) for i in income)
    total_expenses = sum(monthly_expense(e) for e in expenses)
    total_min_debt = sum(_num(d.get("minPayment")) for d in debts)
    total_debt = sum(_num(d.get("balance")) for d in debts)

    # Net flow is what is left after living costs and the minimum debt payments.
    net_flow = total_income - total_expenses - total_min_debt

    savings_rate = (net_flow / total_income * 100) if total_income > 0 else 0

    # Anything positive left over is what we can throw at debt on top of minimums.
    extra = max(0.0, net_flow)
    debt_budget = total_min_debt + extra

    avalanche = simulate_payoff(debts, debt_budget, "avalanche")
    snowball = simulate_payoff(debts, debt_budget, "snowball")

    # Which categories are eating the most, so you know where to look first.
    by_category = {}
    cat_items = {}
    for e in expenses:
        cat = e.get("category") or "Misc"
        by_category[cat] = by_category.get(cat, 0.0) + monthly_expense(e)
        if e.get("name"):
            cat_items.setdefault(cat, []).append(e.get("name"))
    top_categories = sorted(
        ({"category": c, "amount": round(a, 2)} for c, a in by_category.items()),
        key=lambda x: x["amount"],
        reverse=True,
    )[:5]

    # Full breakdowns for the animated view.
    category_breakdown = sorted(
        (
            {"category": c, "amount": round(a, 2), "items": cat_items.get(c, [])}
            for c, a in by_category.items()
            if a > 0
        ),
        key=lambda x: x["amount"],
        reverse=True,
    )
    debt_breakdown = sorted(
        (
            {
                "name": d.get("name") or "Debt",
                "balance": round(_num(d.get("balance")), 2),
                "type": d.get("type", "card"),
                "apr": 0 if d.get("type") == "loan" else _num(d.get("apr")),
            }
            for d in debts
            if _num(d.get("balance")) > 0
        ),
        key=lambda x: x["balance"],
        reverse=True,
    )

    # The "next moves": plain, specific things to do, built from the numbers.
    moves = []
    goal = _num(profile.get("savingsGoal"), 20)

    if total_income == 0:
        moves.append("Add your income first so everything else has something to measure against.")
    elif net_flow < 0:
        gap = abs(net_flow)
        moves.append(
            f"You are short ${gap:,.0f} a month. Cutting from your biggest category "
            f"({top_categories[0]['category'] if top_categories else 'expenses'}) is the fastest fix."
        )
    else:
        if savings_rate < goal:
            needed = (goal / 100 * total_income) - net_flow
            moves.append(
                f"You are saving {savings_rate:.0f} percent. To hit your {goal:.0f} percent goal, "
                f"free up about ${needed:,.0f} more a month."
            )
        else:
            moves.append(
                f"You are saving {savings_rate:.0f} percent, at or above your {goal:.0f} percent goal. "
                f"Hold this and the rest takes care of itself."
            )

    if total_debt > 0:
        if avalanche["paysOff"] and snowball["paysOff"]:
            saved = snowball["interest"] - avalanche["interest"]
            if saved > 1:
                moves.append(
                    f"Pay highest interest first (avalanche). It clears your debt in "
                    f"{avalanche['months']} months and saves ${saved:,.0f} in interest versus snowball."
                )
            else:
                moves.append(
                    f"Either payoff order clears your debt in about {avalanche['months']} months. "
                    f"Snowball gives you quicker early wins if you want the momentum."
                )
        elif not avalanche["paysOff"]:
            moves.append(
                "Your current surplus does not cover the interest on your debt. "
                "Freeing up any monthly cash here matters more than anything else right now."
            )

    if top_categories:
        biggest = top_categories[0]
        share = (biggest["amount"] / total_expenses * 100) if total_expenses > 0 else 0
        if share >= 35:
            moves.append(
                f"{biggest['category']} is {share:.0f} percent of your spending. "
                f"A 10 percent trim there frees ${biggest['amount'] * 0.1:,.0f} a month."
            )

    totals = {
        "income": round(total_income, 2),
        "expenses": round(total_expenses, 2),
        "minDebt": round(total_min_debt, 2),
        "debt": round(total_debt, 2),
        "netFlow": round(net_flow, 2),
        "savingsRate": round(savings_rate, 1),
    }

    return {
        "totals": totals,
        "allocation": {
            "expenses": round(total_expenses, 2),
            "debt": round(total_min_debt, 2),
            "leftover": round(max(0.0, net_flow), 2),
        },
        "topCategories": top_categories,
        "categoryBreakdown": category_breakdown,
        "debtBreakdown": debt_breakdown,
        "spectrum": build_spectrum(category_breakdown, total_min_debt, max(0.0, net_flow)),
        "incomeSources": build_income_sources(income),
        "rule503020": build_rule_503020(total_income, expenses, total_min_debt, net_flow),
        "monthTracking": build_month_tracking(expenses, transactions),
        "trend": build_trend(totals),
        "payoff": {"avalanche": avalanche, "snowball": snowball},
        "debtFreedom": build_debt_freedom(debts, total_min_debt, net_flow),
        "creditGrowth": build_credit_growth(profile.get("creditScore"), total_income, savings_rate, debts),
        "debtFree": build_debt_free(debts, total_min_debt, net_flow),
        "credit": {
            "score": profile.get("creditScore"),
            "band": credit_band(profile.get("creditScore")),
            "tips": credit_tips(profile.get("creditScore")),
        },
        "plan": build_plan(totals, expenses, debts, profile),
        "coaching": build_coaching(totals, {
            "band": credit_band(profile.get("creditScore")),
        }, debts, income),
        "moves": moves,
    }


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def get_state():
    state = load_state()
    return jsonify({"state": state, "insights": build_insights(state)})


@app.route("/api/credit-sim", methods=["POST"])
def credit_sim():
    body = request.get_json(force=True)
    return jsonify(simulate_credit(body.get("score"), body.get("levers", {})))


@app.route("/api/state", methods=["POST"])
def post_state():
    incoming = request.get_json(force=True)
    # Keep only the shape we expect, so a bad payload cannot corrupt the file.
    state = {
        "income": incoming.get("income", []),
        "expenses": incoming.get("expenses", []),
        "debts": incoming.get("debts", []),
        "transactions": incoming.get("transactions", []),
        "profile": incoming.get("profile", DEFAULT_STATE["profile"]),
    }
    save_state(state)
    insights = build_insights(state)
    # Record this month's numbers so next month can show real change.
    save_snapshot(insights["totals"])
    return jsonify({"state": state, "insights": insights})


if __name__ == "__main__":
    init_db()
    # host stays on localhost so this is only reachable from your own machine.
    app.run(host="127.0.0.1", port=5000, debug=True)
