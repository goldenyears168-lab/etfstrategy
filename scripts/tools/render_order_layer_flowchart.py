#!/usr/bin/env python3
"""Render Order layer（下單層）flowchart PDF · as-of champion-tp + sell-first."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "reports" / "order" / f"{date.today().strftime('%Y%m%d')}_order_layer_flowchart.pdf"


def _setup_font() -> None:
    plt.rcParams["font.sans-serif"] = [
        "PingFang TC",
        "Heiti TC",
        "Arial Unicode MS",
        "Noto Sans CJK TC",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


def _box(
    ax,
    xy: tuple[float, float],
    text: str,
    *,
    width: float = 2.6,
    height: float = 0.72,
    fc: str = "#f8fafc",
    ec: str = "#334155",
    fontsize: float = 8.2,
    weight: str = "normal",
) -> FancyBboxPatch:
    x, y = xy
    patch = FancyBboxPatch(
        (x - width / 2, y - height / 2),
        width,
        height,
        boxstyle="round,pad=0.03,rounding_size=0.08",
        linewidth=1.2,
        edgecolor=ec,
        facecolor=fc,
        zorder=2,
    )
    ax.add_patch(patch)
    ax.text(
        x,
        y,
        text,
        ha="center",
        va="center",
        fontsize=fontsize,
        fontweight=weight,
        zorder=3,
        linespacing=1.25,
    )
    return patch


def _arrow(
    ax,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    color: str = "#64748b",
    style: str = "-|>",
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle=style,
            mutation_scale=12,
            linewidth=1.1,
            color=color,
            shrinkA=4,
            shrinkB=4,
            zorder=1,
        )
    )


def render(out: Path) -> Path:
    _setup_font()
    fig, ax = plt.subplots(figsize=(16, 11))
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 11)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.text(
        8,
        10.55,
        "Order layer（下單層）流程圖",
        ha="center",
        va="center",
        fontsize=18,
        fontweight="bold",
        color="#0f172a",
    )
    ax.text(
        8,
        10.1,
        "ABC + C18acc · live_submit_guard · budget cap · sell_first · Fubon Neo",
        ha="center",
        va="center",
        fontsize=10,
        color="#475569",
    )

    # SSOT row
    _box(ax, (8, 9.2), "SSOT\nconfig/order.yaml  ·  .env  ·  account_risk (sell_first)", width=8.5, height=0.85, fc="#e2e8f0", weight="bold")

    # Launchd triggers
    _box(ax, (3.2, 8.0), "launchd\nbuy-signal-radar\n(5m · 09:00–13:20)", width=2.5, fc="#dbeafe")
    _box(ax, (12.8, 8.0), "launchd\nrrg-c18acc-poll\n(5m · 09:00–13:30 · NTB 13:00)", width=2.5, fc="#dbeafe")

    _arrow(ax, (8, 8.78), (3.2, 8.38))
    _arrow(ax, (8, 8.78), (12.8, 8.38))

    # ABC column
    _box(ax, (3.2, 6.95), "strategy_signal_radar\nC0 pool · abc-v3-f1-pullback observe", width=3.0, height=0.9)
    _box(ax, (3.2, 5.75), "process_abc_v3_f1_orders\nENTRY · chase_ask BUY\nre-entry gate · ledger", width=3.0, height=1.05, fc="#dcfce7", ec="#166534")
    _box(ax, (3.2, 4.35), "process_abc_v3_f1_position_orders\nEXIT champion TP SELL\nPYRAMID underwater_rebound BUY", width=3.0, height=1.1, fc="#fef9c3", ec="#a16207")

    _arrow(ax, (3.2, 6.5), (3.2, 6.28))
    _arrow(ax, (3.2, 5.22), (3.2, 4.92))

    ax.text(0.55, 5.75, "ABC\nsleeve", fontsize=11, fontweight="bold", color="#1e40af", rotation=90, va="center")

    # C18 column
    _box(ax, (12.8, 6.95), "rrg_mono_swap_accel_screen\nentry · swap · max_hold · extension", width=3.2, height=0.9)
    _box(ax, (12.8, 5.75), "c18acc_position_monitor\npyramid_add (30m kinematic)", width=3.2, height=0.85, fc="#fce7f3", ec="#9d174d")
    _box(ax, (12.8, 4.35), "process_c18acc_orders\nsort sell_first · budget cap\nswap buy retry · cap gate", width=3.2, height=1.05, fc="#dcfce7", ec="#166534")

    _arrow(ax, (12.8, 6.5), (12.8, 6.28))
    _arrow(ax, (12.8, 5.32), (12.8, 4.92))
    _arrow(ax, (11.35, 6.95), (12.8, 6.0), color="#9d174d")

    ax.text(15.45, 5.75, "C18acc\nsleeve", fontsize=11, fontweight="bold", color="#9d174d", rotation=90, va="center")

    # Shared order core — safety then broker
    _box(
        ax,
        (8, 3.15),
        "live_submit_guard\npytest / ORDER_LIVE_FORBIDDEN 拒單\nsession_date 必須=今日 · buy qty≤budget",
        width=5.6,
        height=0.95,
        fc="#ffedd5",
        ec="#c2410c",
        weight="bold",
    )
    _box(ax, (8, 1.95), "account_cap_gate → Fubon Neo\nreserved_cash · sell_first · place_order · poll", width=5.4, height=0.9, fc="#fee2e2", ec="#b91c1c", weight="bold")

    _arrow(ax, (3.2, 3.78), (6.0, 3.45))
    _arrow(ax, (12.8, 3.78), (10.0, 3.45))
    _arrow(ax, (8, 2.67), (8, 2.4))

    # Artifacts
    _box(ax, (2.0, 1.95), "Ledger / Intent\nabc_v3_f1_ledger.json\nreports/order/intents/", width=2.8, height=1.0, fc="#f1f5f9")
    _box(ax, (14.0, 1.95), "Ledger / State\nc18acc_order_ledger.json\nrrg_c18acc_slots.json", width=2.8, height=1.0, fc="#f1f5f9")

    _arrow(ax, (3.2, 1.45), (2.8, 1.95), style="-")
    _arrow(ax, (12.8, 1.45), (13.2, 1.95), style="-")

    # C18 sell-first detail box
    detail = (
        "C18 sell-first（賣優先）· ① exit SELL ② swap SELL ③ swap BUY / entry / pyramid\n"
        "資金不足：swap BUY → pending_buys（≤3 次）｜pytest 不得走 .venv-fubon 實單（2026-07-13 事故）"
    )
    _box(ax, (8, 0.55), detail, width=12.0, height=0.95, fc="#fff7ed", ec="#c2410c", fontsize=7.6)

    note = (
        "Env：ORDER_C18ACC_AUTO_SUBMIT · ORDER_LIVE_FORBIDDEN · C18ACC_BUDGET_TWD_PER_SLOT=20000 · NTB=13:00"
    )
    ax.text(8, 10.72, note, ha="center", va="top", fontsize=7.8, color="#64748b")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="pdf", bbox_inches="tight", dpi=200)
    plt.close(fig)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Order layer flowchart PDF")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    path = render(args.output)
    latest = ROOT / "reports" / "order" / "order_layer_flowchart.pdf"
    if path.resolve() != latest.resolve():
        latest.write_bytes(path.read_bytes())
    print(f"Wrote {path}")
    print(f"Latest {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
