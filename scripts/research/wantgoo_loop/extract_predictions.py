"""把 staged 部落格全文擷取成 author_predictions 列 (需 LLM)。

兩種執行模式:
  A. 有 ANTHROPIC_API_KEY → 直接呼叫 API, 逐篇萃取結構化預測, 寫進記分板, 標 extracted=1。
     0 預測的貼文也標 extracted=1 (= "見文無可計分", 供 specificity 指標)。
  B. 無金鑰 → 匯出「待擷取 bundle」(pending 貼文全文 + 擷取指令 + JSON schema) 到檔案,
     供 Claude session 或雲端 routine 處理; 不標 extracted (零遺失)。

擷取契約 (每篇 → 0..N 預測), 只收**可證偽**的 call, 丟棄格言/回顧:
  asset_type stock|index · symbol · direction up|down|range · horizon_days(1/5/20/60)
  · target_level(可空) · condition(觸發/證偽條件) · confidence · claim_text(逐字)

用法:
  python extract_predictions.py            # 有金鑰=擷取; 無金鑰=匯出 bundle
  python extract_predictions.py --bundle-only
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from author_predictions import DB, init_db, add_prediction  # noqa: E402

BUNDLE = Path(__file__).resolve().parents[3] / "data" / "wantgoo_loop_extract_bundle.json"

EXTRACT_INSTRUCTION = (
    "你是台股籌碼研究助理。逐篇讀作者全文, 只擷取**可證偽的前瞻預測** "
    "(有標的+方向+可驗證的時間/價位/條件)。丟棄: 純回顧、格言、無條件感想、廣告。"
    "每篇輸出 0..N 個預測物件。欄位: asset_type(stock|index), symbol(個股代號或 'TAIEX'), "
    "direction(up|down|range), horizon_days(1|5|20|60, 依語意估), target_level(數字或 null), "
    "condition(觸發或證偽條件, 逐字或濃縮), confidence(high|med|low, 依語氣), "
    "claim_text(關鍵句逐字)。寧缺勿濫 — 不確定是否可證偽就不要收。"
)

SCHEMA_HINT = {
    "predictions": [{
        "post_ref": "member_id/post_id",
        "asset_type": "stock|index", "symbol": "2303|TAIEX",
        "direction": "up|down|range", "horizon_days": 5,
        "target_level": None, "condition": "…", "confidence": "med",
        "claim_text": "…",
    }]
}


def _pending(con) -> list[dict]:
    cols = ["member_id", "post_id", "author_name", "publish_date", "title", "is_sell", "story_text"]
    rows = con.execute(
        f"SELECT {','.join(cols)} FROM blog_posts WHERE extracted=0 ORDER BY publish_ms DESC"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def emit_bundle() -> Path:
    con = sqlite3.connect(DB); pend = _pending(con); con.close()
    BUNDLE.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "instruction": EXTRACT_INSTRUCTION,
        "output_schema": SCHEMA_HINT,
        "note": "把每個 prediction 用 author_predictions.add_prediction() 寫入 (made_date=該篇 publish_date)。"
                "完成後 UPDATE blog_posts SET extracted=1 WHERE member_id/post_id。",
        "pending_posts": pend,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return BUNDLE


def _mark_extracted(con, member_id: str, post_id: str):
    con.execute("UPDATE blog_posts SET extracted=1 WHERE member_id=? AND post_id=?",
                (str(member_id), str(post_id)))


def extract_via_api() -> dict:
    """Requires ANTHROPIC_API_KEY + anthropic SDK. Returns counts."""
    import anthropic  # lazy
    client = anthropic.Anthropic()
    con = sqlite3.connect(DB); pend = _pending(con)
    n_posts, n_preds = 0, 0
    for post in pend:
        if not post["story_text"]:
            _mark_extracted(con, post["member_id"], post["post_id"]); continue
        msg = client.messages.create(
            model="claude-sonnet-5", max_tokens=2000,
            system=EXTRACT_INSTRUCTION,
            messages=[{"role": "user", "content":
                       f"作者:{post['author_name']} 日期:{post['publish_date']} 標題:{post['title']}\n"
                       f"全文:\n{post['story_text'][:8000]}\n\n"
                       f"回傳 JSON: {json.dumps(SCHEMA_HINT, ensure_ascii=False)}"}])
        try:
            txt = msg.content[0].text
            data = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
            preds = data.get("predictions", [])
        except Exception:
            preds = []
        for p in preds:
            try:
                add_prediction(
                    member_id=post["member_id"], author_name=post["author_name"],
                    post_id=post["post_id"], made_date=post["publish_date"],
                    asset_type=p["asset_type"], symbol=str(p["symbol"]), direction=p["direction"],
                    horizon_days=int(p.get("horizon_days", 5)), target_level=p.get("target_level"),
                    condition=p.get("condition"), claim_text=p.get("claim_text"),
                    confidence=p.get("confidence"), extracted_by="api-claude-sonnet-5")
                n_preds += 1
            except Exception as e:
                print(f"  ! add failed {post['post_id']}: {e}")
        _mark_extracted(con, post["member_id"], post["post_id"]); n_posts += 1
    con.commit(); con.close()
    return {"posts": n_posts, "predictions": n_preds}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle-only", action="store_true")
    a = ap.parse_args()
    init_db()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if a.bundle_only or not has_key:
        if not has_key and not a.bundle_only:
            print("(no ANTHROPIC_API_KEY — emitting bundle for session/routine extraction)")
        b = emit_bundle()
        con = sqlite3.connect(DB); n = con.execute(
            "SELECT COUNT(*) FROM blog_posts WHERE extracted=0").fetchone()[0]; con.close()
        print(f"bundle -> {b}  ({n} pending post(s))")
        return
    try:
        r = extract_via_api()
        print(f"extracted via API: {r['posts']} post(s) -> {r['predictions']} prediction(s)")
    except ImportError:
        print("anthropic SDK not installed; run `pip install anthropic` or use --bundle-only")
        emit_bundle()


if __name__ == "__main__":
    main()
