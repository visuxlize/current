# current

A local budgeting app. Everything you enter stays on your machine, saved to a
file called `current.db` that sits next to the app.

## Run it

You need Python 3 (you have `python3` on your Mac already).

```bash
cd current
pip install flask
python3 app.py
```

Then open http://127.0.0.1:5000 in your browser.

That is the whole setup. No build step, no npm, no accounts.

## What it does

- Track income, expenses, and debts, all monthly.
- See where every dollar goes in the allocation bar at the top.
- Get concrete next moves built from your own numbers, not generic advice.
- Compare two debt payoff orders: avalanche (saves the most interest) and
  snowball (clears small debts first for momentum).
- Read your credit band and the levers that move it.

Edit anything, hit Save (or Cmd+S), and the numbers and advice update.

## Files

- `app.py` - the Python backend, storage, and all the money math. Commented
  so you can read and change it.
- `templates/index.html` - the whole interface, one file.
- `current.db` - created on first run. This is your data. Back it up by copying
  this one file. Delete it to start fresh.

## Notes

The money math is plain arithmetic, all in `app.py`, so you can check it
yourself. The payoff simulation walks month by month: interest accrues,
minimums get paid, then whatever is left attacks one debt based on the strategy.
