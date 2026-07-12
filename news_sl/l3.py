"""L3: 全ソースL2 → news_master 統合。

company_master突合ロジックは job-sl の `job_sl/l3.py` と同一設計(vendor版の重複実装)。
job-slを直接依存にしないのはjob-sl側と同じ理由(パッケージの独立性を保つため)。

- company_name を company_master の `商号_nor` と同一規則(`core_key`。company-normパッケージの
  vendor版で、法人格36種+異体字統一まで対応)で正規化して法人番号突合。
- 同名複数がヒットする場合は以下を順に試して一意化:
    1. 処理区分(国税庁法人番号データ)が閉鎖・取消系の候補を除外
    2. なお複数残る場合、`body_snippet`からプレスリリース冒頭でよくある
       "(本社：東京都渋谷区、...)"のような自己申告の所在地表記を都道府県名で抽出し、
       本店所在都道府県が一致する候補に絞り込み(求人のlocationに相当する信号が
       ニュース記事には無いため、本文の自己紹介文から代用)
    3. 完全未マッチの場合のみ、注記・拠点表記を除去して再突合
  どちらも効かなければ `is_ambiguous_company=1` のまま。
- 採用した解決方法は `match_method` に記録する。
- 新規掲載判定は article_url を前回 news_master と比較する日次差分(is_new_today)。
"""

from __future__ import annotations

import re
import unicodedata

_DASH_RE = re.compile(r"[‐‑‒–—―−﹣－]")


def _unify_dash(s: str) -> str:
    return _DASH_RE.sub("-", s or "")


_LEGAL_FORMS = [
    "株式会社",
    "有限会社",
    "合同会社",
    "合資会社",
    "合名会社",
    "一般社団法人",
    "公益社団法人",
    "一般財団法人",
    "公益財団法人",
    "特定非営利活動法人",
    "npo法人",
    "医療法人社団",
    "医療法人財団",
    "医療法人",
    "社会福祉法人",
    "学校法人",
    "宗教法人",
    "農事組合法人",
    "事業協同組合",
    "協同組合",
    "労働組合",
    "農業協同組合",
    "漁業協同組合",
    "信用金庫",
    "信用組合",
    "相互会社",
    "独立行政法人",
    "地方独立行政法人",
    "国立大学法人",
    "公立大学法人",
    "特殊法人",
    "認可法人",
    "管理組合法人",
    "税理士法人",
    "弁護士法人",
    "監査法人",
    "(株)",
    "(有)",
    "(合)",
    "(同)",
]
_FORMS_SORTED = sorted(_LEGAL_FORMS, key=len, reverse=True)

_ITAIJI = {
    "學": "学",
    "國": "国",
    "會": "会",
    "體": "体",
    "應": "応",
    "髙": "高",
    "﨑": "崎",
    "澤": "沢",
    "齋": "斎",
    "齊": "斉",
    "邊": "辺",
    "邉": "辺",
    "濱": "浜",
    "廣": "広",
    "藪": "薮",
    "籔": "薮",
    "驒": "騨",
}


def core_key(name: str) -> str:
    """company_master の商号_norと同一規則で正規化(company-normパッケージのvendor版)。"""
    if not name:
        return ""
    s = _unify_dash(unicodedata.normalize("NFKC", str(name).strip()).lower())
    if not s:
        return ""
    for f in _FORMS_SORTED:
        e = re.escape(f)
        s = re.sub(f"^{e}\\s*", "", s)
        s = re.sub(f"\\s*{e}$", "", s)
    s = s.replace("・", "")
    for o, n in _ITAIJI.items():
        s = s.replace(o, n)
    return s.strip()


_CLOSED_SHOBUN_CODES = {"71", "72", "81"}

_PREFS = [
    "北海道",
    "青森県",
    "岩手県",
    "宮城県",
    "秋田県",
    "山形県",
    "福島県",
    "茨城県",
    "栃木県",
    "群馬県",
    "埼玉県",
    "千葉県",
    "東京都",
    "神奈川県",
    "新潟県",
    "富山県",
    "石川県",
    "福井県",
    "山梨県",
    "長野県",
    "岐阜県",
    "静岡県",
    "愛知県",
    "三重県",
    "滋賀県",
    "京都府",
    "大阪府",
    "兵庫県",
    "奈良県",
    "和歌山県",
    "鳥取県",
    "島根県",
    "岡山県",
    "広島県",
    "山口県",
    "徳島県",
    "香川県",
    "愛媛県",
    "高知県",
    "福岡県",
    "佐賀県",
    "長崎県",
    "熊本県",
    "大分県",
    "宮崎県",
    "鹿児島県",
    "沖縄県",
]
_PREF_RE = re.compile("|".join(_PREFS))

_NOTE_RE = re.compile(r"【[^】]*】|\([^)]*\)|（[^）]*）|/.*$|\|.*$")
_BRANCH_SUFFIX_RE = re.compile(
    r"(本社|東京本社|.{0,6}?(支店|支社|営業所|事業所|事業部|工場|支局|センター|オフィス|店))$"
)

NEWS_MASTER_SCHEMA = [
    "article_url",
    "source",
    "company_name",
    "houjin_bangou",
    "is_ambiguous_company",
    "match_method",
    "title",
    "category",
    "keywords",
    "published_date",
    "body_snippet",
    "image_url",
    "scraped_at",
    "first_seen_date",
    "is_new_today",
]


def index_company_master(
    cm_rows,
    shogo_col: str = "商号",
    houjin_col: str = "法人番号",
    pref_col: str = "都道府県",
    shobun_col: str = "処理区分",
):
    """company_master行(iterable of dict) → {core_key: [(法人番号, 都道府県, 処理区分), ...]}。"""
    idx: dict = {}
    for r in cm_rows:
        h = (r.get(houjin_col) or "").strip()
        if not h:
            continue
        key = core_key(r.get(shogo_col, ""))
        if not key:
            continue
        idx.setdefault(key, []).append(
            (h, (r.get(pref_col) or "").strip(), (r.get(shobun_col) or "").strip())
        )
    return idx


def _resolve(cands: list[tuple[str, str, str]], location_text: str):
    active = [c for c in cands if c[2] not in _CLOSED_SHOBUN_CODES]
    pool = active if active else cands
    uniq = sorted({c[0] for c in pool})
    if len(uniq) == 1:
        method = "closure_excluded" if len(pool) < len(cands) else "exact"
        return uniq[0], False, method

    m = _PREF_RE.search(location_text or "")
    if m:
        narrowed = sorted({c[0] for c in pool if c[1] == m.group(0)})
        if len(narrowed) == 1:
            return narrowed[0], False, "pref_disambiguated"

    return "", True, "ambiguous"


def match_houjin(company_name: str, index: dict, location_text: str = ""):
    """(法人番号 or "", 同名複数で未解決か, match_method) を返す。

    `location_text`にはbody_snippet等、都道府県名が言及されていそうな自由文を渡す
    (求人のlocationのような構造化フィールドがニュース記事には無いため)。
    """
    key = core_key(company_name)
    cands = index.get(key)
    if cands:
        return _resolve(cands, location_text)

    s = unicodedata.normalize("NFKC", str(company_name or "").strip())
    s = _NOTE_RE.sub("", s)
    note_key = core_key(s)
    if note_key and note_key != key:
        cands = index.get(note_key)
        if cands:
            houjin, ambiguous, method = _resolve(cands, location_text)
            return houjin, ambiguous, ("note_stripped" if method == "exact" else method)

    branch_key = _BRANCH_SUFFIX_RE.sub("", key) if key else ""
    if branch_key and branch_key != key:
        cands = index.get(branch_key)
        if cands:
            houjin, ambiguous, method = _resolve(cands, location_text)
            return (
                houjin,
                ambiguous,
                ("branch_stripped" if method == "exact" else method),
            )

    return "", False, "unmatched"
