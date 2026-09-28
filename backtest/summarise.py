"""Quick summary printer for latest backtest result CSV."""
import sys
import pandas as pd
import pathlib

days = sys.argv[1] if len(sys.argv) > 1 else "60"
p = pathlib.Path("backtest/results")
combined = sorted(p.glob(f"combined_{days}d_*.csv"))
if not combined:
    print(f"No combined_{days}d_*.csv found in {p}")
    sys.exit(1)

cfile = combined[-1]
print(f"Using: {cfile.name}\n")

df    = pd.read_csv(cfile)
clean = df[df["exit_reason"] != "end-of-data"].copy()

wins   = int((clean["pnl_usd"] > 0).sum())
losses = int((clean["pnl_usd"] <= 0).sum())
total  = wins + losses
wr     = wins / total * 100 if total else 0
gp     = float(clean.loc[clean["pnl_usd"] > 0, "pnl_usd"].sum())
gl     = float(-clean.loc[clean["pnl_usd"] <= 0, "pnl_usd"].sum())
pf     = gp / gl if gl > 0 else float("inf")
total_pnl = float(df["pnl_usd"].sum())
avg_r  = float(clean["r_multiple"].mean()) if total else 0

# Max drawdown via equity curve
initial = 10_000.0 * df["symbol"].nunique()
peak, mdd = initial, 0.0
for e in [initial] + list(df["equity_after"]):
    if e > peak:
        peak = e
    dd = (peak - e) / peak if peak > 0 else 0
    if dd > mdd:
        mdd = dd

print("=" * 55)
print(f"  BACKTEST SUMMARY — {days}-day window")
print("=" * 55)
print(f"  Period        : {days} days")
print(f"  Symbols       : {sorted(df['symbol'].unique())}")
print(f"  Total trades  : {total}  ({wins}W / {losses}L)")
print(f"  Win rate      : {wr:.1f}%")
print(f"  Profit factor : {pf:.3f}")
print(f"  Total PnL     : ${total_pnl:+.4f}")
print(f"  Gross profit  : ${gp:.4f}")
print(f"  Gross loss    : ${gl:.4f}")
print(f"  Avg R-mult    : {avg_r:.3f}R")
print(f"  Max drawdown  : {mdd*100:.2f}%")
print()
print("--- Per-symbol breakdown ---")
by_sym = (
    df.groupby("symbol")
    .agg(
        trades=("pnl_usd", "count"),
        wins=("pnl_usd", lambda x: (x > 0).sum()),
        total_pnl=("pnl_usd", "sum"),
        avg_r=("r_multiple", "mean"),
    )
    .round(4)
)
by_sym["win_rate_pct"] = (by_sym["wins"] / by_sym["trades"] * 100).round(1)
print(by_sym[["trades", "wins", "win_rate_pct", "total_pnl", "avg_r"]].to_string())
print()
print("--- Exit reason breakdown ---")
print(df["exit_reason"].value_counts().to_string())
