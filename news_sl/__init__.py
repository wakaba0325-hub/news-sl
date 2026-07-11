"""news-sl: news_sources 共通土台(job-slと同じ設計パターン)。

層:
  L1  raw/news_sources/<source>/{YYYYMMDD}/<source>.csv      生データ・当日新規を追記(不変)
  L2  master/news_sources/<source>/{YYYYMMDD}/<source>.csv   article_urlキーでdedupしたソースmaster(最新日=全件)

設計決定:
- キーは全ソース共通で article_url(記事詳細URLは媒体側で一意)。
- job-slでは write_l1/build_l2 を何度も呼ぶ長時間巡回コレクタ(マイナビ)が現れた際、
  キー列名をコード内に `job_url` とハードコードしていたため使い回しにくかった反省を踏まえ、
  本パッケージは最初から `KEY_FIELD` を可変にしておく。
- 長時間・多数ページを巡回するコレクタ向けに、全履歴再スキャンを初回のみで済ませる
  `L1Checkpointer` を最初から提供する(job-slでは後付けだった)。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config as _BotoConfig

REGION = "ap-northeast-1"
BUCKET = "emooove-data-lake"
JST = timezone(timedelta(hours=9))
KEY_FIELD = "article_url"

COMMON_SCHEMA = [
    "source",
    "company_name",
    "title",
    "category",
    "keywords",
    "published_date",
    "body_snippet",
    "image_url",
    "article_url",
    "scraped_at",
]

_S3_CONFIG = _BotoConfig(
    region_name=REGION,
    read_timeout=900,
    connect_timeout=30,
    retries={"max_attempts": 5, "mode": "standard"},
)


def _s3():
    return boto3.client("s3", config=_S3_CONFIG)


def _today():
    return datetime.now(JST).strftime("%Y%m%d")


def key_for(row: dict):
    """L2 dedupキー: article_url。"""
    return (row.get(KEY_FIELD) or "").strip().lower()


def to_common(source: str, r: dict) -> dict:
    out = {"source": source}
    for col in COMMON_SCHEMA:
        if col == "source":
            continue
        out[col] = (r.get(col) or "").strip()
    out["body_snippet"] = out["body_snippet"][:500]
    return out


# ---- L1 読み書き -----------------------------------------------------------


def _read_csv_s3(key: str) -> list[dict]:
    body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(body)))


def read_all_l1(source: str) -> list[dict]:
    """そのソースの全L1パーティション(=追記ログ)を union して返す。"""
    s3 = _s3()
    rows = []
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=BUCKET, Prefix=f"raw/news_sources/{source}/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith(f"/{source}.csv") and "/_" not in o["Key"]:
                rows.extend(_read_csv_s3(o["Key"]))
    return rows


def write_l1(source: str, rows: list[dict], run_date: str | None = None):
    """生データを当日L1へ追記(既存全パーティションのarticle_urlで重複除外)。"""
    seen = {key_for(to_common(source, r)) for r in read_all_l1(source)}
    new = [r for r in rows if key_for(to_common(source, r)) not in seen and key_for(r)]
    rd = run_date or _today()
    key = f"raw/news_sources/{source}/{rd}/{source}.csv"
    existing = []
    try:
        existing = _read_csv_s3(key)
    except Exception:
        pass
    cols = (
        list(existing[0].keys()) if existing else list(rows[0].keys()) if rows else []
    )
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(existing + new)
    _s3().put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue().encode("utf-8-sig"))
    return key, len(new)


class L1Checkpointer:
    """1プロセス内で複数回に分けてL1へ追記するコレクタ用(長時間巡回の中間保存)。

    全履歴スキャンをインスタンス生成時の1回だけ行い、以降はプロセス内メモリで
    dedupを完結させる(job-slの`L1Checkpointer`と同じ設計、詳細はそちらのdocstring参照)。
    `build_l2`は呼ばないので、呼び出し元がrun完了時に1回だけ別途呼ぶこと。
    """

    def __init__(self, source: str, run_date: str | None = None):
        self.source = source
        self.run_date = run_date or _today()
        self._key = f"raw/news_sources/{source}/{self.run_date}/{source}.csv"
        self._seen = {key_for(to_common(source, r)) for r in read_all_l1(source)}
        try:
            self._existing = _read_csv_s3(self._key)
        except Exception:
            self._existing = []
        self._new_rows: list[dict] = []

    def flush(self, rows: list[dict]) -> int:
        new = [
            r
            for r in rows
            if key_for(to_common(self.source, r)) not in self._seen and key_for(r)
        ]
        for r in new:
            self._seen.add(key_for(to_common(self.source, r)))
        self._new_rows.extend(new)

        all_rows = self._existing + self._new_rows
        cols = (
            list(all_rows[0].keys())
            if all_rows
            else list(rows[0].keys())
            if rows
            else []
        )
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
        _s3().put_object(
            Bucket=BUCKET, Key=self._key, Body=buf.getvalue().encode("utf-8-sig")
        )
        return len(new)


# ---- L2 ビルド -------------------------------------------------------------


def build_l2(source: str, run_date: str | None = None, write: bool = True):
    """L1全パーティションを union→正規化→article_urlでdedup(最新scraped_at採用)→L2へ。"""
    commons = [to_common(source, r) for r in read_all_l1(source)]
    commons = [c for c in commons if c[KEY_FIELD] and c["company_name"]]

    groups: dict = {}
    for c in commons:
        groups.setdefault(key_for(c), []).append(c)

    out = []
    for _, items in groups.items():
        items.sort(key=lambda c: c["scraped_at"])  # 古→新
        base = dict(items[-1])
        for col in COMMON_SCHEMA:
            if not base.get(col):
                for c in reversed(items):
                    if c.get(col):
                        base[col] = c[col]
                        break
        out.append(base)

    rd = run_date or _today()
    key = f"master/news_sources/{source}/{rd}/{source}.csv"
    if write:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COMMON_SCHEMA, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
        _s3().put_object(
            Bucket=BUCKET, Key=key, Body=buf.getvalue().encode("utf-8-sig")
        )
    return out, key


# ---- 最新パーティション解決 -------------------------------------------------


def existing_urls(source: str) -> set[str]:
    """最新L2のarticle_url集合(=既知の記事)。無ければ空集合。
    詳細ページの再取得要否をコレクタ側で判定するための軽量ヘルパー
    (L1全履歴は読まず、既にdedup済みの最新L2 1ファイルだけを読む)。"""
    key = latest_key(f"master/news_sources/{source}/", f"{source}.csv")
    if not key:
        return set()
    try:
        rows = _read_csv_s3(key)
    except Exception:
        return set()
    return {r.get(KEY_FIELD, "").strip() for r in rows if r.get(KEY_FIELD)}


def latest_key(prefix: str, fname: str) -> str | None:
    """`{prefix}{YYYYMMDD}/{fname}` を list_objects_v2 + 正規表現で最新解決。"""
    s3 = _s3()
    pat = re.compile(rf"^{re.escape(prefix)}(\d{{8}})/{re.escape(fname)}$")
    dates = []
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            m = pat.match(o["Key"])
            if m:
                dates.append(m.group(1))
    return f"{prefix}{max(dates)}/{fname}" if dates else None
