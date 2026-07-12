# news-sl

プレスリリース/ニュース配信サイト収集(`<source>-news-collector` 群)の共通土台。
`job-sl` と同じ設計パターン(L1追記・L2 URLキーdedup)を踏襲。

## 仕組み

- L1: `raw/news_sources/<source>/{YYYYMMDD}/<source>.csv` — 当日取得分の追記ログ
- L2: `master/news_sources/<source>/{YYYYMMDD}/<source>.csv` — `article_url`キーでdedupした正規化マスタ(最新日=全件)

各媒体は schema.org `NewsArticle` 相当のフィールド(`COMMON_SCHEMA`)に正規化して渡す。
全ソース共通で `article_url` を一意キーとする。

`job-sl`との違い: job-slは長時間巡回コレクタ(マイナビ)が現れてから`L1Checkpointer`を
後付けした(全履歴再スキャンのコストが呼び出し回数に比例して際限なく重くなっていた)。
本パッケージは最初から`L1Checkpointer`を提供する。

## 使い方

```python
import news_sl as nsl

rows = [{"company_name": "...", "title": "...", "article_url": "...", "scraped_at": "...", ...}]
_, n_new = nsl.write_l1("prtimes", rows)
_, l2_key = nsl.build_l2("prtimes")

# 長時間巡回コレクタ向け:
cp = nsl.L1Checkpointer("prtimes")
for batch in ...:
    n_new = cp.flush(batch)
nsl.build_l2("prtimes")  # run完了時に1回だけ
```

## L3: news_master (全ソース統合・company_master突合・新規掲載検知)

`python -m news_sl.build_l3 [--apply]` (実行は別リポ `news-master-consolidate` から)で:

- `master/news_sources/<source>/{最新}/<source>.csv` を全ソースunion(現状`prtimes`のみ)
- `company_name` を `company_master` の `商号_nor` と同一規則(`news_sl/l3.py` の `core_key`。
  `job_sl/l3.py`と同じcore-normパッケージのvendor版)で正規化して法人番号突合
- 同名複数がヒットする場合は以下を順に試して一意化:
  1. 処理区分(国税庁法人番号データ)が閉鎖・取消系の候補を除外
  2. なお複数残れば、`body_snippet`(プレスリリース冒頭の「本社：東京都〜」等の
     自己紹介文)から都道府県を抽出し本店所在都道府県が一致する候補に絞り込み
     (求人のlocationに相当する構造化フィールドがニュースには無いため代用)
  3. 完全未マッチの場合のみ、注記・拠点表記を除去して再突合
  - 解決できなければ `is_ambiguous_company=1`(法人番号は空)のまま
  - 採用した解決方法は `match_method` 列に記録
- `article_url` を前回 `news_master` と比較し `first_seen_date` / `is_new_today` を付与
- `master/news_master/{YYYYMMDD}/news_master.csv` へスナップショット型で書込(全件洗い替え)
