#!/usr/bin/env python3
"""TX 台指期 Donchian 突破 + regime filter 複合策略 — smoke test 回測。

整合 7 個 GitHub 參考 repo 的技法（詳見 reports/research/tx-donchian-regime/README.md）：
- 進出場不對稱週期 Donchian（crypto-turtle：20 日進場 / 10 日出場，海龜系統原型）
- ADX(14) Wilder 平滑動能濾網 + ATR 波動率比濾網（alpaca-donchian-adx-vf-bot）
- 固定風險比例部位大小（EigenEngineer 的「Kelly」宣稱查無實據，改用 fixed-fractional risk）
- 保守 worst-case 同根K棒路徑模擬（先觸停損）

刻意排除：Hurst exponent regime 濾網（MSR-DE 版本經查是單窗口 R/S 近似值，日線樣本數不足時
不穩定，且該 repo 宣稱的 WFA/Ablation 驗證程式碼裡查無實據——見上述 README）。

⚠️ 這是「試水溫」smoke test，不是通過驗證的策略：
- 僅單一固定視窗回測，沒有 walk-forward / ablation 分析
- 前月合約單純用當日成交量最大的月份，沒有處理結算日轉倉的價格跳空調整
- Look-ahead bias 防護：Donchian 通道與 ADX/ATR 皆對齊「前一根收盤已知」再比較（shift(1)）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from finmind_client import fetch_finmind  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
CONTRACT_POINT_VALUE = 200  # TX 台指期：每點 NT$200
ENTRY_PERIOD = 20
EXIT_PERIOD = 10
ADX_PERIOD = 14
ATR_PERIOD = 14
ADX_THRESHOLD = 20.0
STARTING_EQUITY = 3_000_000.0
RISK_PCT = 0.005
ASSUMED_MARGIN_PER_CONTRACT = 200_000.0
MARGIN_UTIL_CAP = 0.6


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def fetch_tx_front_month_daily(start: date, end: date) -> pd.DataFrame:
    """FinMind TaiwanFuturesDaily → 近月主力合約日盤 OHLCV 連續序列。

    每個交易日 FinMind 會回傳多個到期月份 x 日盤/夜盤 x 含跨月價差組合的列；
    篩選規則：trading_session == 'position'（日盤）、contract_date 不含 '/'（排除價差組合），
    同日內取成交量最大的合約月份視為近月主力（換月時流動性會自然轉移到次月）。
    """
    rows = fetch_finmind("TaiwanFuturesDaily", "TX", start, end)
    if not rows:
        raise RuntimeError("FinMind TaiwanFuturesDaily 回傳空值")
    df = pd.DataFrame(rows)
    df = df[(df["trading_session"] == "position") & (~df["contract_date"].str.contains("/"))]
    df = df[df["volume"] > 0]
    df = df.sort_values(["date", "volume"], ascending=[True, False])
    front = df.groupby("date", as_index=False).first()
    front["date"] = pd.to_datetime(front["date"])
    front = front.sort_values("date").reset_index(drop=True)
    front = front.rename(columns={"max": "high", "min": "low"})
    return front[["date", "contract_date", "open", "high", "low", "close", "volume"]]


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    high_diff = high - np.roll(high, 1)
    low_diff = np.roll(low, 1) - low
    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)
    eps = 1e-10
    tr_s = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean()
    plus_s = pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False).mean()
    minus_s = pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_s / (tr_s + eps)
    minus_di = 100 * minus_s / (tr_s + eps)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + eps)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return pd.Series(adx.values, index=df.index)


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["entry_high"] = df["high"].rolling(ENTRY_PERIOD).max()
    df["entry_low"] = df["low"].rolling(ENTRY_PERIOD).min()
    df["exit_high"] = df["high"].rolling(EXIT_PERIOD).max()
    df["exit_low"] = df["low"].rolling(EXIT_PERIOD).min()
    # look-ahead 防護：只能用「前一根收盤已知」的通道值跟今天比
    df["prev_entry_high"] = df["entry_high"].shift(1)
    df["prev_entry_low"] = df["entry_low"].shift(1)
    df["prev_exit_high"] = df["exit_high"].shift(1)
    df["prev_exit_low"] = df["exit_low"].shift(1)

    df["adx"] = compute_adx(df)
    df["atr"] = compute_atr(df)
    df["vol_ratio"] = df["atr"] / df["close"]
    df["vol_ratio_p40"] = df["vol_ratio"].rolling(252, min_periods=60).quantile(0.40)

    df["regime_ok"] = (df["adx"].shift(1) > ADX_THRESHOLD) & (
        df["vol_ratio"].shift(1) > df["vol_ratio_p40"].shift(1)
    )
    df["long_entry"] = (df["close"] > df["prev_entry_high"]) & df["regime_ok"]
    df["short_entry"] = (df["close"] < df["prev_entry_low"]) & df["regime_ok"]
    df["long_exit"] = df["close"] < df["prev_exit_low"]
    df["short_exit"] = df["close"] > df["prev_exit_high"]
    return df


def size_contracts(equity: float, stop_distance_pts: float) -> tuple[int, float]:
    """回傳 (口數, 實際風險%)。

    台指期單口停損距離常達 700~1000+ 點（×NT$200/點），用嚴格風險比例常會算出
    不足 1 口——這裡採業界慣例：只要保證金額度夠、且訊號成立，至少進 1 口，
    但誠實回報「這一口實際佔用的風險%」可能超過 RISK_PCT 目標值，不做隱藏。
    """
    if stop_distance_pts <= 0:
        return 0, 0.0
    risk_amount = equity * RISK_PCT
    by_margin = int((equity * MARGIN_UTIL_CAP) / ASSUMED_MARGIN_PER_CONTRACT)
    if by_margin <= 0:
        return 0, 0.0
    by_risk = int(risk_amount / (stop_distance_pts * CONTRACT_POINT_VALUE))
    qty = max(1, by_risk) if by_margin >= 1 else 0
    qty = min(qty, by_margin)
    actual_risk_pct = (qty * stop_distance_pts * CONTRACT_POINT_VALUE) / equity * 100
    return qty, actual_risk_pct


def run_backtest(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], float]:
    equity = STARTING_EQUITY
    equity_curve = []
    trades: list[dict] = []
    position = None  # dict: direction, entry_price, entry_date, contracts, stop

    for _, row in df.iterrows():
        if pd.isna(row["prev_entry_high"]) or pd.isna(row["adx"]):
            equity_curve.append(equity)
            continue

        if position is None:
            if row["long_entry"]:
                stop = row["prev_exit_low"]
                stop_dist = row["close"] - stop
                qty, risk_pct = size_contracts(equity, stop_dist)
                if qty > 0:
                    position = {
                        "direction": "long",
                        "entry_price": row["close"],
                        "entry_date": row["date"],
                        "contracts": qty,
                        "stop": stop,
                        "risk_pct": risk_pct,
                    }
            elif row["short_entry"]:
                stop = row["prev_exit_high"]
                stop_dist = stop - row["close"]
                qty, risk_pct = size_contracts(equity, stop_dist)
                if qty > 0:
                    position = {
                        "direction": "short",
                        "entry_price": row["close"],
                        "entry_date": row["date"],
                        "contracts": qty,
                        "stop": stop,
                        "risk_pct": risk_pct,
                    }
        else:
            direction = position["direction"]
            # worst-case 同根K棒路徑：先看停損有沒有被觸及（保守假設）
            hit_stop = (
                row["low"] <= position["stop"]
                if direction == "long"
                else row["high"] >= position["stop"]
            )
            struct_exit = row["long_exit"] if direction == "long" else row["short_exit"]

            exit_price = None
            reason = None
            if hit_stop:
                exit_price = position["stop"]
                reason = "stop"
            elif struct_exit:
                exit_price = row["close"]
                reason = "channel_exit"

            if exit_price is not None:
                pnl_pts = (
                    exit_price - position["entry_price"]
                    if direction == "long"
                    else position["entry_price"] - exit_price
                )
                pnl = pnl_pts * CONTRACT_POINT_VALUE * position["contracts"]
                equity += pnl
                trades.append(
                    {
                        "direction": direction,
                        "entry_date": position["entry_date"],
                        "exit_date": row["date"],
                        "entry_price": position["entry_price"],
                        "exit_price": exit_price,
                        "contracts": position["contracts"],
                        "pnl": pnl,
                        "reason": reason,
                        "risk_pct_at_entry": position["risk_pct"],
                    }
                )
                position = None

        equity_curve.append(equity)

    df = df.copy()
    df["equity"] = equity_curve
    return df, trades, equity


def summarize(trades: list[dict], final_equity: float) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    return {
        "total_trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "total_pnl": total_pnl,
        "final_equity": final_equity,
        "return_pct": (final_equity / STARTING_EQUITY - 1) * 100,
    }


def plot_results(df: pd.DataFrame, trades: list[dict], today: pd.Timestamp, out_dir: Path) -> None:
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 9), gridspec_kw={"height_ratios": [2.2, 1]}, sharex=True
    )

    ax1.plot(df["date"], df["close"], color="black", linewidth=1, label="TX 近月收盤")
    ax1.plot(
        df["date"], df["prev_entry_high"], color="#1F8A65", linestyle="--",
        linewidth=0.9, label=f"Donchian 進場上軌 ({ENTRY_PERIOD}日, shift1)"
    )
    ax1.plot(
        df["date"], df["prev_entry_low"], color="#C0392B", linestyle="--",
        linewidth=0.9, label=f"Donchian 進場下軌 ({ENTRY_PERIOD}日, shift1)"
    )
    ax1.plot(
        df["date"], df["prev_exit_high"], color="#E67E22", linestyle=":",
        linewidth=0.8, label=f"出場通道 ({EXIT_PERIOD}日, shift1)"
    )
    ax1.plot(df["date"], df["prev_exit_low"], color="#2980B9", linestyle=":", linewidth=0.8)

    for t in trades:
        color = "#1F8A65" if t["direction"] == "long" else "#C0392B"
        marker_in = "^" if t["direction"] == "long" else "v"
        marker_out = "v" if t["direction"] == "long" else "^"
        ax1.scatter(t["entry_date"], t["entry_price"], marker=marker_in, color=color, s=70, zorder=5)
        ax1.scatter(t["exit_date"], t["exit_price"], marker=marker_out, color="gray", s=70, zorder=5)

    today_row = df[df["date"] == today]
    if not today_row.empty:
        ax1.axvline(today, color="purple", linestyle="-", linewidth=1.2, alpha=0.6)
        ax1.annotate(
            "8/6 test-the-water",
            xy=(today, today_row["close"].iloc[0]),
            xytext=(10, 20),
            textcoords="offset points",
            color="purple",
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="purple"),
        )

    ax1.set_title("TX 台指期 Donchian(20/10) + ADX/ATR regime filter — smoke test")
    ax1.set_ylabel("指數點")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    ax2.plot(df["date"], df["equity"], color="#2C3E50", linewidth=1.3)
    ax2.axhline(STARTING_EQUITY, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_ylabel("權益 (NT$)")
    ax2.set_xlabel("日期")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    path = fig_dir / f"tx_donchian_regime_{today.strftime('%Y%m%d')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"chart saved: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-08-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"fetching TX front-month daily bars {start} -> {end} ...")
    raw = fetch_tx_front_month_daily(start, end)
    print(f"rows: {len(raw)}, date range: {raw['date'].min()} -> {raw['date'].max()}")

    sig = build_signals(raw)
    bt, trades, final_equity = run_backtest(sig)
    stats = summarize(trades, final_equity)

    today = bt["date"].max()
    last = bt.iloc[-1]
    print("\n=== 8/6（最新交易日）通道狀態 ===")
    print(f"日期: {last['date'].date()}  合約: {last['contract_date']}  收盤: {last['close']}")
    print(f"進場上軌(前日): {last['prev_entry_high']:.0f}  進場下軌(前日): {last['prev_entry_low']:.0f}")
    print(f"ADX(前日): {sig['adx'].shift(1).iloc[-1]:.1f}  regime_ok: {bool(last['regime_ok'])}")
    print(f"今日是否觸發多單訊號: {bool(last['long_entry'])}  空單訊號: {bool(last['short_entry'])}")

    print("\n=== 回測摘要（僅供 smoke test 參考，非驗證結果） ===")
    for k, v in stats.items():
        print(f"{k}: {v}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_df = pd.DataFrame(trades)
    trades_path = OUT_DIR / f"trades_{today.strftime('%Y%m%d')}.csv"
    trades_df.to_csv(trades_path, index=False)
    print(f"\ntrades saved: {trades_path}")

    plot_results(bt, trades, today, OUT_DIR)


if __name__ == "__main__":
    main()
