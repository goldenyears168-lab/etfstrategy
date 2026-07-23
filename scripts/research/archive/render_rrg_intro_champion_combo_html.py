"""RRG intro + Champion backtest · single scroll page (intro in collapsed details)."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
INTRO_MD = ROOT / "docs" / "RRG相對輪動圖入門.md"

LIVE_CONTRACT_CSS = """
.live-contract {
  margin: 16px 0 18px; padding: 14px 16px 16px;
  background: linear-gradient(180deg, #1a2420 0%, #161616 100%);
  border: 1px solid #2f4a3c; border-radius: 10px;
}
.live-contract h2 {
  margin: 0 0 8px; font-size: 16px; color: #b8e0c8; font-weight: 650;
}
.live-contract .lede { margin: 0 0 12px; color: #9ab5a6; font-size: 13px; line-height: 1.55; }
.live-contract h3 { margin: 14px 0 6px; font-size: 13px; color: #cde8d8; }
.live-contract ul { margin: 0 0 0 1.1em; padding: 0; color: #c8c8c8; font-size: 13px; line-height: 1.55; }
.live-contract li { margin: 3px 0; }
.live-contract table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
.live-contract th, .live-contract td {
  border: 1px solid #333; padding: 6px 8px; text-align: left; color: #ccc;
}
.live-contract th { background: #1c1c1c; color: #ddd; }
.live-contract code { background: #222; padding: 1px 4px; border-radius: 3px; font-size: 11px; }
.live-contract .note { margin: 10px 0 0; font-size: 12px; color: #888; }
"""

COMBO_CSS = """
.combo-page { max-width: 1320px; margin: 0 auto; padding: 0 20px 40px; }
.rrg-intro-details {
  margin: 16px 0 20px; background: #181818; border: 1px solid #333;
  border-radius: 8px; padding: 0 14px 12px;
}
.rrg-intro-details summary {
  cursor: pointer; color: #ccc; font-weight: 600; font-size: 14px;
  padding: 12px 0; list-style-position: outside;
}
.rrg-intro-details[open] summary { margin-bottom: 4px; border-bottom: 1px solid #2a2a2a; }
.intro-article {
  max-width: 920px; margin: 0 auto; padding: 8px 0 0;
  font-size: 14px; line-height: 1.65; color: #ccc;
}
.intro-article h1 { font-size: 18px; color: #eee; margin: 0 0 10px; }
.intro-article h2 { font-size: 17px; color: #eee; margin: 28px 0 10px; border-bottom: 1px solid #333; padding-bottom: 6px; }
.intro-article h3 { font-size: 15px; color: #ddd; margin: 18px 0 8px; }
.intro-article p { margin: 8px 0; }
.intro-article ul, .intro-article ol { margin: 8px 0 8px 1.2em; }
.intro-article li { margin: 4px 0; }
.intro-article table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
.intro-article th, .intro-article td { border: 1px solid #333; padding: 6px 8px; text-align: left; }
.intro-article th { background: #1a1a1a; color: #ddd; }
.intro-article pre {
  background: #1a1a1a; border: 1px solid #333; border-radius: 6px;
  padding: 10px 12px; overflow-x: auto; font-size: 12px; color: #aaa;
}
.intro-article blockquote {
  margin: 10px 0; padding: 8px 12px; border-left: 3px solid #6B8CAE; color: #aaa; background: #181818;
}
.intro-article code { background: #222; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
.intro-lede { font-size: 15px; color: #bbb; margin-bottom: 20px; }
.intro-source { font-size: 12px; color: #666; margin-top: 24px; }
.combo-page .doc { max-width: none; padding: 0; }
""" + LIVE_CONTRACT_CSS

INTRO_SECTION_KEYS = (
    "這張圖在回答什麼",
    "在本專案中的位置",
    "第一步",
    "第二步",
    "第三步",
    "第四步",
    "第五步",
    "怎麼讀一張 RRG 圖",
    "一句話記住 RRG",
    "採納策略範例",
)


def render_live_contract_panel_html() -> str:
    """Executable-contract explainer · timeline T-3→T · selection funnel."""
    return """
<section class="live-contract" id="live-contract">
  <h2>本頁契約 · 現行可下單 C18acc（合理版）</h2>
  <p class="lede">
    回測對齊 Order / Strategy SSOT，<strong>不是</strong>舊站「早盤 cinema／POOL1」上限故事。
    槽複利會明顯低於舊展示頁——那是拿掉前視與過寬規則後的合理結果，不是算壞。
  </p>
  <h3>從 T−3 起的時間軸</h3>
  <ul>
    <li><strong>T−3～T</strong>：四日加速窗（<code>accel_lookback=4</code>）· 比誰在加速／減速 · <strong>不是</strong> T−3 下單</li>
    <li><strong>T 日 13:00 前</strong>：觀測／持倉管理；live 不開新倉</li>
    <li><strong>T 日 13:00–13:30</strong>：鎖當日暫定 fresh（≈EOD）→ confirm → 進場／換倉</li>
    <li><strong>持有</strong>：min_hold≈5 日可輪動 · max_hold≈10 日邊界</li>
  </ul>
  <h3>選股漏斗（寬→窄）</h3>
  <table>
    <thead><tr><th>關卡</th><th>規則</th></tr></thead>
    <tbody>
      <tr><td>1 · 宇宙</td><td>ETF 成分 watchlist</td></tr>
      <tr><td>2 · 結構</td><td>mono 軌跡條件</td></tr>
      <tr><td>3 · 定池</td><td><code>fresh-only</code>（同日近收盤暫定）</td></tr>
      <tr><td>4 · 池排序</td><td><code>seg_last</code> · 全池 passthrough</td></tr>
      <tr><td>5 · 進場</td><td>≥13:00 · <code>avg_accel</code> · confirm=1 · avoid_mixed · G_R5_12</td></tr>
      <tr><td>6 · 部位</td><td>最多 3 槽 · S2 出場／四日加速換倉</td></tr>
    </tbody>
  </table>
  <p class="note">
    buy-signal-radar 早盤信 = advisory 觀測軸，不等於本頁三槽成交軸。
    SSOT：<code>config/strategy.yaml</code> · <code>config/order.yaml</code> · <code>champion_score_swap_c_config()</code>
  </p>
</section>
"""


def _split_html_parts(html_str: str) -> tuple[str, str, str]:
    styles = ""
    m = re.search(r"<style>(.*?)</style>", html_str, re.DOTALL | re.IGNORECASE)
    if m:
        styles = m.group(1).strip()
    m = re.search(r"<body[^>]*>(.*)</body>", html_str, re.DOTALL | re.IGNORECASE)
    body = m.group(1).strip() if m else html_str.strip()
    scripts: list[str] = []
    for sm in re.finditer(r"<script[^>]*>(.*?)</script>", body, re.DOTALL | re.IGNORECASE):
        scripts.append(sm.group(1).strip())
    body_inner = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE).strip()
    return styles, body_inner, "\n".join(scripts)


def apply_dom_prefix(html_str: str, prefix: str) -> str:
    if not prefix:
        return html_str
    ids = set(re.findall(r'\bid="([a-zA-Z][a-zA-Z0-9_-]*)"', html_str))
    return prefix_dom_refs(html_str, prefix, ids)


def prefix_dom_refs(html_str: str, prefix: str, ids: set[str]) -> str:
    if not prefix or not ids:
        return html_str
    for id_ in sorted(ids, key=len, reverse=True):
        if id_.startswith(prefix):
            continue
        new_id = prefix + id_
        html_str = html_str.replace(f'id="{id_}"', f'id="{new_id}"')
        html_str = html_str.replace(f"getElementById('{id_}')", f"getElementById('{new_id}')")
        html_str = html_str.replace(f'getElementById("{id_}")', f'getElementById("{new_id}")')
        html_str = html_str.replace(f"url(#{id_})", f"url(#{new_id})")
        html_str = re.sub(
            rf"(querySelector(?:All)?\(['\"])#{re.escape(id_)}",
            rf"\1#{new_id}",
            html_str,
        )
    return html_str


def prefix_script_for_body(body_html: str, script: str, prefix: str) -> tuple[str, str]:
    """Prefix DOM ids in body + matching getElementById/querySelector in script."""
    ids = set(re.findall(r'\bid="([a-zA-Z][a-zA-Z0-9_-]*)"', body_html))
    return apply_dom_prefix(body_html, prefix), prefix_dom_refs(script, prefix, ids)


def _inline_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def _md_table_to_html(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for line in lines:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.match(r"^[-:]+$", c) for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return ""
    head = rows[0]
    body = rows[1:]
    out = ["<table><thead><tr>"]
    out.extend(f"<th>{_inline_md(c)}</th>" for c in head)
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        out.extend(f"<td>{_inline_md(c)}</td>" for c in row)
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _md_section_to_html(block: str) -> str:
    lines = block.splitlines()
    if not lines:
        return ""
    title = lines[0].lstrip("#").strip()
    level = len(lines[0]) - len(lines[0].lstrip("#"))
    tag = "h2" if level <= 2 else "h3"
    parts = [f"<{tag}>{html.escape(title)}</{tag}>"]
    i = 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            parts.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
            continue
        if line.strip().startswith("|"):
            tbl_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i])
                i += 1
            parts.append(_md_table_to_html(tbl_lines))
            continue
        if line.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip(">").strip())
                i += 1
            parts.append(f"<blockquote>{_inline_md(' '.join(quote))}</blockquote>")
            continue
        if re.match(r"^[-*] ", line):
            items: list[str] = []
            while i < len(lines) and re.match(r"^[-*] ", lines[i]):
                items.append(lines[i][2:].strip())
                i += 1
            parts.append("<ul>" + "".join(f"<li>{_inline_md(it)}</li>" for it in items) + "</ul>")
            continue
        if re.match(r"^\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                items.append(re.sub(r"^\d+\.\s*", "", lines[i]))
                i += 1
            parts.append("<ol>" + "".join(f"<li>{_inline_md(it)}</li>" for it in items) + "</ol>")
            continue
        if line.startswith("#"):
            lvl = len(line) - len(line.lstrip("#"))
            t = line.lstrip("#").strip()
            ht = "h3" if lvl <= 3 else "h4"
            parts.append(f"<{ht}>{html.escape(t)}</{ht}>")
            i += 1
            continue
        para: list[str] = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "|", "-", "*")):
            if re.match(r"^\d+\. ", lines[i]):
                break
            if lines[i].startswith("```"):
                break
            para.append(lines[i].strip())
            i += 1
        parts.append(f"<p>{_inline_md(' '.join(para))}</p>")
    return "\n".join(parts)


def _sanitize_combo_intro_prose(text: str) -> str:
    """Combo 頁嵌入 intro：移除 ETF 相關用語。"""
    for old, new in (
        ("股票、ETF、產業", "股票、產業"),
        ("（例如 ETF 成分股）", "（例如監控清單內多檔個股）"),
        ("例如 ETF 成分股", "例如監控清單內多檔個股"),
        ("ETF 成分股", "監控清單個股"),
    ):
        text = text.replace(old, new)
    return text


def render_rrg_intro_article_html(*, md_path: Path | None = None) -> str:
    path = md_path or INTRO_MD
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=## )", text)
    sections: list[str] = []
    lede = ""
    for block in blocks:
        if block.startswith("# ") and not block.startswith("## "):
            first = block.split("\n", 1)
            title = first[0].lstrip("#").strip()
            rest = first[1] if len(first) > 1 else ""
            for line in rest.splitlines():
                if line.startswith(">"):
                    lede = line.lstrip("> ").strip()
                    break
            sections.append(f"<h1>{html.escape(title)}</h1>")
            if lede:
                sections.append(f'<p class="intro-lede">{_inline_md(lede)}</p>')
            continue
        title_line = block.split("\n", 1)[0]
        if not any(k in title_line for k in INTRO_SECTION_KEYS):
            continue
        sections.append(_md_section_to_html("## " + block if not block.startswith("##") else block))
    sections.append(
        '<p class="intro-source">文案來源：<code>docs/RRG相對輪動圖入門.md</code>'
        " · 下方圖表為監控清單 watchlist（WMA20 · 加權指數 IX0001）"
        " · 金色為 C18acc 採納版持倉區間"
        " · 最新 snapshot 聯集，非逐日 PIT</p>"
    )
    return _sanitize_combo_intro_prose(f'<article class="intro-article">{"".join(sections)}</article>')


def render_rrg_intro_champion_combo_html(
    *,
    champion_html: str,
    intro_md_path: Path | None = None,
    page_title: str | None = None,
    universe_html: str | None = None,
    live_contract: bool = False,
) -> str:
    """Single scroll page: collapsed RRG intro + champion cinema (KPI · chart · feed · table)."""
    _ = universe_html  # legacy arg · no longer embedded
    intro_html = render_rrg_intro_article_html(md_path=intro_md_path)
    champ_styles, champ_body, champ_script = _split_html_parts(champion_html)
    contract = render_live_contract_panel_html() if live_contract else ""

    title = page_title or "RRG 入門 · C18acc 回測案例"
    if live_contract and "可下單" not in title and "live" not in title.lower():
        title = title.replace("C18acc 回測", "C18acc 可下單契約回測")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; }}
    {COMBO_CSS}
    {champ_styles}
  </style>
</head>
<body>
  <div class="combo-page">
    {contract}
    <details class="rrg-intro-details">
      <summary>RRG 相對輪動圖入門（點開閱讀）</summary>
      {intro_html}
    </details>
    {champ_body}
  </div>
  <script>{champ_script}</script>
</body>
</html>
"""
