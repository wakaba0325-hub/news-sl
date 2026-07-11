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
