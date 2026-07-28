// compute.js — the whole money engine, ported from app.py so the app needs no server.
// Pure functions. Given a state object, computeInsights returns the same shape
// the Flask backend used to return.

const FREQ_TO_MONTHLY = { weekly: 52 / 12, biweekly: 26 / 12, semimonthly: 2, monthly: 1 };
const NEEDS_CATEGORIES = new Set(["Housing", "Utilities", "Food", "Transport", "Health", "Insurance", "Kids & Pets"]);
const CREDIT_MILESTONES = [
  [500, "Secured cards and starter cards"],
  [620, "Most basic unsecured cards, some auto loans"],
  [670, "Most rewards cards and better auto rates"],
  [740, "Premium cards and the best loan rates"],
  [800, "Top-tier approvals and lowest rates"],
];
const SIM_LEVERS = [
  ["payOnTime", 40, "Pay every bill on time for 6 months"],
  ["lowerUtilization", 35, "Get every card under 30% used (under 10% is better)"],
  ["noNewLatePayments", 25, "No new late payments or collections"],
  ["keepOldCards", 15, "Keep your oldest cards open"],
  ["raiseLimit", 12, "Request a limit increase on a card you handle well"],
  ["fewerInquiries", 8, "Avoid new hard inquiries for a while"],
  ["creditMix", 10, "Add one different account type (e.g. a small installment loan)"],
];

const round2 = (x) => Math.round((x + Number.EPSILON) * 100) / 100;
function num(v, d = 0) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function toMonthly(amount, frequency) { return num(amount) * (FREQ_TO_MONTHLY[frequency || "monthly"] ?? 1); }
function monthlyIncome(i) { return toMonthly(i.amount, i.frequency); }
function monthlyExpense(e) { return toMonthly(e.amount, e.frequency); }
function currentMonth() { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0"); }

function simulatePayoff(debts, monthlyBudget, order) {
  const working = debts
    .filter((d) => num(d.balance) > 0)
    .map((d) => ({ balance: num(d.balance), apr: d.type === "loan" ? 0 : num(d.apr), min: num(d.minPayment) }));
  if (!working.length) return { months: 0, interest: 0, paysOff: true };
  let totalInterest = 0, months = 0; const cap = 1200;
  while (working.some((d) => d.balance > 0) && months < cap) {
    months += 1;
    for (const d of working) if (d.balance > 0) { const it = d.balance * (d.apr / 100 / 12); d.balance += it; totalInterest += it; }
    let budget = monthlyBudget;
    for (const d of working) if (d.balance > 0) { const pay = Math.min(d.min, d.balance, budget); d.balance -= pay; budget -= pay; }
    const remaining = working.filter((d) => d.balance > 0);
    remaining.sort((a, b) => order === "avalanche" ? b.apr - a.apr : a.balance - b.balance);
    for (const d of remaining) { if (budget <= 0) break; const pay = Math.min(budget, d.balance); d.balance -= pay; budget -= pay; }
  }
  const paysOff = working.every((d) => d.balance <= 0.01);
  return { months: paysOff ? months : null, interest: round2(totalInterest), paysOff };
}

function creditBand(score) {
  if (score == null || score === "") return null;
  if (score >= 800) return "exceptional";
  if (score >= 740) return "very good";
  if (score >= 670) return "good";
  if (score >= 580) return "fair";
  return "poor";
}
function creditTips(score) {
  const band = creditBand(score);
  if (band == null) return [];
  const base = ["Keep every card under 30 percent of its limit, under 10 is better.", "Never miss a due date. Payment history is the biggest single factor."];
  if (band === "poor" || band === "fair") return base.concat(["Do not close old cards. Age of accounts helps you.", "One late payment can undo months of progress, so automate the minimums."]);
  if (band === "good") return base.concat(["Ask for a limit increase on a card you handle well. It lowers your usage ratio."]);
  return base.concat(["You are in strong shape. Hold your usage low and let account age do the rest."]);
}

function simulateCredit(score, levers) {
  const base = (score != null && score !== "") ? Math.trunc(score) : 650;
  let gain = 0; const applied = [];
  for (const [key, pts, label] of SIM_LEVERS) {
    const on = !!(levers && levers[key]);
    applied.push({ key, points: pts, label, on });
    if (on) gain += pts;
  }
  const projected = Math.max(300, Math.min(850, base + gain));
  return { base, projected, gain: projected - base, levers: applied, band: creditBand(projected) };
}

function buildCreditGrowth(score, totalIncome, savingsRate, debts) {
  const band = creditBand(score);
  const steps = [];
  if (score == null) {
    steps.push(["Add your credit score", "Enter it in Your money so this can tailor the path."]);
  } else {
    const nxt = CREDIT_MILESTONES.find((m) => m[0] > score);
    if (nxt) steps.push([`Reach ${nxt[0]}`, `Unlocks: ${nxt[1]}. You are ${nxt[0] - Math.trunc(score)} points away.`]);
    const utilCards = debts.filter((d) => d.type !== "loan" && num(d.balance) > 0);
    if (utilCards.length) steps.push(["Pay cards below 30% used", "Utilization is the fastest lever. Paying balances down shows up within a cycle or two."]);
    steps.push(["Automate every minimum", "One on-time streak of 6+ months moves the needle more than almost anything else."]);
    if (band === "poor" || band === "fair") steps.push(["Consider a secured card or credit-builder loan", "Both add positive history without much risk while your score climbs."]);
  }
  let incomeMoves = [
    ["Stabilize before you scale", "Lenders like steady, documented income. Keep pay stubs and tax records clean."],
    ["Add one income stream", "A small recurring side income raises approval odds and your savings rate."],
    ["Raise your savings rate", `You are saving about ${Math.round(savingsRate)}%. Every point higher is buffer and leverage.`],
  ];
  if (totalIncome === 0) incomeMoves = [["Add your income", "Enter it so this can measure your savings rate and progress."]];
  return {
    band,
    milestones: CREDIT_MILESTONES.map(([s, u]) => ({ score: s, unlocks: u, reached: (score != null && score >= s) })),
    creditSteps: steps.map(([title, detail]) => ({ title, detail })),
    incomeSteps: incomeMoves.map(([title, detail]) => ({ title, detail })),
  };
}

function buildDebtFreedom(debts, totalMinDebt, netFlow) {
  const totalDebt = debts.filter((d) => num(d.balance) > 0).reduce((s, d) => s + num(d.balance), 0);
  if (totalDebt <= 0) return { hasDebt: false };
  const extra = Math.max(0, netFlow);
  const withExtra = simulatePayoff(debts, totalMinDebt + extra, "avalanche");
  const minsOnly = simulatePayoff(debts, totalMinDebt, "avalanche");
  let saved = null;
  if (withExtra.paysOff && minsOnly.paysOff) saved = round2(minsOnly.interest - withExtra.interest);
  let biggest = null;
  for (const d of debts) if (num(d.balance) > 0 && (!biggest || num(d.balance) > num(biggest.balance))) biggest = d;
  return {
    hasDebt: true, totalDebt: round2(totalDebt), extra: round2(extra),
    monthsWithExtra: withExtra.months, monthsMinsOnly: minsOnly.months,
    interestWithExtra: withExtra.interest, interestSaved: saved,
    target: biggest ? { name: biggest.name || "", balance: round2(num(biggest.balance)), apr: biggest.type === "loan" ? 0 : num(biggest.apr) } : { name: "", balance: 0, apr: 0 },
  };
}

function buildRule503020(totalIncome, expenses, totalMinDebt, netFlow) {
  let needs = totalMinDebt, wants = 0;
  for (const e of expenses) { const amt = monthlyExpense(e); if (NEEDS_CATEGORIES.has(e.category || "")) needs += amt; else wants += amt; }
  const savings = Math.max(0, netFlow);
  return {
    income: round2(totalIncome),
    needs: { actual: round2(needs), target: round2(totalIncome * 0.5) },
    wants: { actual: round2(wants), target: round2(totalIncome * 0.3) },
    savings: { actual: round2(savings), target: round2(totalIncome * 0.2) },
  };
}

function buildIncomeSources(income) {
  const total = income.reduce((s, i) => s + monthlyIncome(i), 0) || 1;
  return income.filter((i) => monthlyIncome(i) > 0)
    .map((i) => ({ source: i.source || "Income", amount: round2(monthlyIncome(i)), share: Math.round(monthlyIncome(i) / total * 100) }))
    .sort((a, b) => b.amount - a.amount);
}

function buildSpectrum(categoryBreakdown, totalMinDebt, leftover) {
  const bars = categoryBreakdown.map((c) => ({ label: c.category, amount: c.amount }));
  if (totalMinDebt > 0) bars.push({ label: "Debt Repayment", amount: round2(totalMinDebt) });
  if (leftover > 0) bars.push({ label: "Savings", amount: round2(leftover) });
  return bars;
}

function buildMonthTracking(expenses, transactions) {
  const month = currentMonth();
  const budgetByCat = {};
  for (const e of expenses) { const c = e.category || "Misc"; budgetByCat[c] = (budgetByCat[c] || 0) + monthlyExpense(e); }
  const spentByCat = {}; let spent = 0, count = 0;
  for (const t of transactions) {
    if (String(t.date || "").startsWith(month)) { const c = t.category || "Misc"; const a = num(t.amount); spentByCat[c] = (spentByCat[c] || 0) + a; spent += a; count += 1; }
  }
  const cats = Array.from(new Set([...Object.keys(budgetByCat), ...Object.keys(spentByCat)])).sort();
  const rows = [];
  for (const c of cats) {
    const b = round2(budgetByCat[c] || 0), s = round2(spentByCat[c] || 0);
    if (b === 0 && s === 0) continue;
    rows.push({ category: c, budget: b, spent: s, over: s > b && b > 0 });
  }
  rows.sort((a, b) => b.spent - a.spent);
  return { month, spent: round2(spent), budget: round2(Object.values(budgetByCat).reduce((s, v) => s + v, 0)), count, byCategory: rows };
}

function buildTrend(totals, snapshots) {
  const month = currentMonth();
  const priorMonths = Object.keys(snapshots || {}).filter((m) => m < month).sort();
  if (!priorMonths.length) return { income: null, expenses: null, savings: null };
  const prev = snapshots[priorMonths[priorMonths.length - 1]];
  const pct = (now, before) => { before = num(before); if (before === 0) return null; return round2((now - before) / Math.abs(before) * 100); };
  return { income: pct(totals.income, prev.income), expenses: pct(totals.expenses, prev.expenses), savings: pct(totals.netFlow, prev.netFlow) };
}

function buildPlan(totals, expenses, debts, profile) {
  const surplus = totals.netFlow;
  const monthlyCost = totals.expenses + totals.minDebt;
  const efMonths = Math.trunc(num(profile.efMonths, 3)) || 3;
  const breathingPct = Math.min(50, Math.max(0, num(profile.breathingPct, 10)));
  const currentSavings = num(profile.savings, 0);

  if (surplus <= 0) {
    if (totals.income <= 0) return { ok: false, surplus: round2(surplus), headline: "Your plan builds itself here.", detail: "Add your income and expenses on the left, and a step-by-step plan for your surplus appears in this spot." };
    return { ok: false, surplus: round2(surplus), headline: "Your plan starts with closing the monthly gap.", detail: `Right now you spend about $${Math.abs(Math.round(surplus)).toLocaleString()} more than you bring in each month, so there is no surplus to put to work yet. Trimming your biggest category is the first move, and the plan opens up the moment net flow turns positive.` };
  }
  const breathing = round2(surplus * breathingPct / 100);
  const allocatable = Math.max(0, round2(surplus - breathing));
  const starterTarget = round2(monthlyCost);
  const fullTarget = round2(efMonths * monthlyCost);
  const hiDebt = debts.filter((d) => d.type !== "loan" && num(d.apr) >= 8 && num(d.balance) > 0).reduce((s, d) => s + num(d.balance), 0);
  const monthsFor = (amt) => (allocatable <= 0 || amt <= 0) ? null : Math.ceil(amt / allocatable);
  const dollars = (n) => "$" + Math.round(n).toLocaleString();

  const steps = [
    { title: "Starter cushion", target: starterTarget, done: currentSavings >= starterTarget, note: `One month of costs, about ${dollars(starterTarget)}.` },
    { title: "Clear high-interest debt", target: round2(hiDebt), done: hiDebt <= 0, note: hiDebt > 0 ? `Cards at 8% or more, about ${dollars(hiDebt)}.` : "No high-interest debt. Nothing to clear." },
    { title: `${efMonths}-month emergency fund`, target: fullTarget, done: currentSavings >= fullTarget, note: `Bring savings up to about ${dollars(fullTarget)}.` },
    { title: "Grow your savings", target: null, done: false, note: "Send the surplus to savings or long-term investing that fits your goals." },
  ];
  let currentIdx = steps.findIndex((s) => !s.done); if (currentIdx === -1) currentIdx = steps.length - 1;
  steps.forEach((s, i) => { s.status = s.done ? "done" : (i === currentIdx ? "current" : "upcoming"); });

  const buckets = { emergencyFund: 0, debt: 0, savings: 0, breathingRoom: breathing };
  let headline, detail;
  if (currentIdx === 0) {
    buckets.emergencyFund = allocatable; const m = monthsFor(Math.max(0, starterTarget - currentSavings));
    headline = "Build a starter cushion first.";
    detail = `Put ${dollars(allocatable)} a month into savings until you have a one-month cushion of ${dollars(starterTarget)}.` + (m ? ` About ${m} month${m !== 1 ? "s" : ""} to go.` : "");
  } else if (currentIdx === 1) {
    buckets.debt = allocatable; const sim = simulatePayoff(debts.filter((d) => d.type !== "loan"), totals.minDebt + allocatable, "avalanche"); const m = sim.months;
    headline = "Now crush the high-interest debt.";
    detail = `Add ${dollars(allocatable)} a month on top of the minimums, aimed at your highest-rate card first.` + (m ? ` That clears the cards in about ${m} months.` : "");
  } else if (currentIdx === 2) {
    buckets.emergencyFund = allocatable; const m = monthsFor(Math.max(0, fullTarget - currentSavings));
    headline = "Top up to a full emergency fund.";
    detail = `Keep adding ${dollars(allocatable)} a month to savings until you reach ${dollars(fullTarget)}, your ${efMonths}-month cushion.` + (m ? ` About ${m} month${m !== 1 ? "s" : ""} to go.` : "");
  } else {
    buckets.savings = allocatable;
    headline = "You are set up. Now grow it.";
    detail = `Debts are handled and your cushion is full. Send ${dollars(allocatable)} a month to savings or long-term investing, whatever fits your goals.`;
  }
  const r2 = (o) => { const out = {}; for (const k in o) out[k] = round2(o[k]); return out; };
  return { ok: true, surplus: round2(surplus), breathing, allocatable, buckets: r2(buckets), steps, currentIndex: currentIdx, headline, detail, efMonths };
}

function computeInsights(state) {
  const income = state.income || [], expenses = state.expenses || [], debts = state.debts || [],
    transactions = state.transactions || [], profile = state.profile || {}, snapshots = state.snapshots || {};

  const totalIncome = income.reduce((s, i) => s + monthlyIncome(i), 0);
  const totalExpenses = expenses.reduce((s, e) => s + monthlyExpense(e), 0);
  const totalMinDebt = debts.reduce((s, d) => s + num(d.minPayment), 0);
  const totalDebt = debts.reduce((s, d) => s + num(d.balance), 0);
  const netFlow = totalIncome - totalExpenses - totalMinDebt;
  const savingsRate = totalIncome > 0 ? netFlow / totalIncome * 100 : 0;
  const extra = Math.max(0, netFlow);
  const avalanche = simulatePayoff(debts, totalMinDebt + extra, "avalanche");
  const snowball = simulatePayoff(debts, totalMinDebt + extra, "snowball");

  const byCategory = {}, catItems = {};
  for (const e of expenses) { const c = e.category || "Misc"; byCategory[c] = (byCategory[c] || 0) + monthlyExpense(e); if (e.name) (catItems[c] = catItems[c] || []).push(e.name); }
  const topCategories = Object.entries(byCategory).map(([c, a]) => ({ category: c, amount: round2(a) })).sort((x, y) => y.amount - x.amount).slice(0, 5);
  const categoryBreakdown = Object.entries(byCategory).filter(([, a]) => a > 0).map(([c, a]) => ({ category: c, amount: round2(a), items: catItems[c] || [] })).sort((x, y) => y.amount - x.amount);
  const debtBreakdown = debts.filter((d) => num(d.balance) > 0).map((d) => ({ name: d.name || "Debt", balance: round2(num(d.balance)), type: d.type || "card", apr: d.type === "loan" ? 0 : num(d.apr) })).sort((a, b) => b.balance - a.balance);

  const moves = [];
  const goal = num(profile.savingsGoal, 20);
  if (totalIncome === 0) moves.push("Add your income first so everything else has something to measure against.");
  else if (netFlow < 0) moves.push(`You are short $${Math.abs(Math.round(netFlow)).toLocaleString()} a month. Cutting from your biggest category (${topCategories[0] ? topCategories[0].category : "expenses"}) is the fastest fix.`);
  else {
    if (savingsRate < goal) { const needed = (goal / 100 * totalIncome) - netFlow; moves.push(`You are saving ${Math.round(savingsRate)} percent. To hit your ${Math.round(goal)} percent goal, free up about $${Math.round(needed).toLocaleString()} more a month.`); }
    else moves.push(`You are saving ${Math.round(savingsRate)} percent, at or above your ${Math.round(goal)} percent goal. Hold this and the rest takes care of itself.`);
  }
  if (totalDebt > 0) {
    if (avalanche.paysOff && snowball.paysOff) {
      const saved = snowball.interest - avalanche.interest;
      if (saved > 1) moves.push(`Pay highest interest first (avalanche). It clears your debt in ${avalanche.months} months and saves $${Math.round(saved).toLocaleString()} in interest versus snowball.`);
      else moves.push(`Either payoff order clears your debt in about ${avalanche.months} months. Snowball gives you quicker early wins if you want the momentum.`);
    } else if (!avalanche.paysOff) moves.push("Your current surplus does not cover the interest on your debt. Freeing up any monthly cash here matters more than anything else right now.");
  }
  if (topCategories.length) {
    const biggest = topCategories[0]; const share = totalExpenses > 0 ? biggest.amount / totalExpenses * 100 : 0;
    if (share >= 35) moves.push(`${biggest.category} is ${Math.round(share)} percent of your spending. A 10 percent trim there frees $${Math.round(biggest.amount * 0.1).toLocaleString()} a month.`);
  }

  const totals = {
    income: round2(totalIncome), expenses: round2(totalExpenses), minDebt: round2(totalMinDebt),
    debt: round2(totalDebt), netFlow: round2(netFlow), savingsRate: round2(savingsRate),
  };

  return {
    totals,
    allocation: { expenses: round2(totalExpenses), debt: round2(totalMinDebt), leftover: round2(Math.max(0, netFlow)) },
    topCategories, categoryBreakdown, debtBreakdown,
    spectrum: buildSpectrum(categoryBreakdown, totalMinDebt, Math.max(0, netFlow)),
    incomeSources: buildIncomeSources(income),
    rule503020: buildRule503020(totalIncome, expenses, totalMinDebt, netFlow),
    monthTracking: buildMonthTracking(expenses, transactions),
    trend: buildTrend(totals, snapshots),
    payoff: { avalanche, snowball },
    debtFreedom: buildDebtFreedom(debts, totalMinDebt, netFlow),
    creditGrowth: buildCreditGrowth(profile.creditScore, totalIncome, savingsRate, debts),
    credit: { score: profile.creditScore ?? null, band: creditBand(profile.creditScore), tips: creditTips(profile.creditScore) },
    plan: buildPlan(totals, expenses, debts, profile),
    moves,
  };
}

if (typeof module !== "undefined") module.exports = { computeInsights, simulateCredit, currentMonth };
