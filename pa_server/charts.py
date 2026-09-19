from __future__ import annotations

from pathlib import Path


def render_chart(snapshot: list[dict], decision: dict, path: Path, symbol: str, timeframe: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bars = list(reversed(snapshot[:100]))
    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#10151d")
    ax.set_facecolor("#10151d")
    closes = [float(b["close"]) for b in bars]
    ema = []
    alpha = 2 / 21
    for close in closes:
        ema.append(close if not ema else alpha * close + (1 - alpha) * ema[-1])
    for i, bar in enumerate(bars):
        op, hi, lo, cl = map(float, (bar["open"], bar["high"], bar["low"], bar["close"]))
        color = "#24c78e" if cl >= op else "#ef5350"
        ax.vlines(i, lo, hi, color=color, linewidth=1)
        ax.add_patch(Rectangle((i - .32, min(op, cl)), .64, max(abs(cl - op), 1e-9), color=color))
    ax.plot(range(len(ema)), ema, color="#f2b705", linewidth=1.2, label="EMA20")
    for field, color, label in (
        ("entry_price", "#42a5f5", "Entry"),
        ("stop_loss_price", "#ef5350", "SL"),
        ("take_profit_price", "#24c78e", "TP1"),
        ("take_profit_price_2", "#66bb6a", "TP2"),
    ):
        try:
            value = float(decision.get(field))
        except (TypeError, ValueError):
            continue
        ax.axhline(value, color=color, linestyle="--", linewidth=1, label=f"{label} {value:g}")
    ax.set_title(f"{symbol} {timeframe}", color="white")
    ax.tick_params(colors="#aab4c3")
    for spine in ax.spines.values():
        spine.set_color("#344050")
    ax.grid(alpha=.15)
    ax.legend(loc="best")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
