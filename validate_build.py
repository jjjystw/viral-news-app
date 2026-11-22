#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""빌드 결과 검증기 — index.html에 인라인된 데이터와 마크업을 점검한다.

    python3 viral-news-app/validate_build.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent
HTML = APP / "index.html"
DATA = APP / "data.json"
KST = timezone(timedelta(hours=9))
CATS = {"사건사고", "얼탱없음", "감동", "이슈"}

errors: list[str] = []
warns: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def main() -> int:
    if not HTML.exists():
        print("✗ index.html 없음")
        return 1
    html = HTML.read_text(encoding="utf-8")

    # 1) 자리표시자 제거 여부
    if "/*__NEWS_DATA__*/" in html:
        err("index.html에 데이터 자리표시자가 남아 있음")

    # 2) 인라인 데이터 파싱
    m = re.search(r'<script id="news-data" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        print("✗ 인라인 데이터 스크립트를 찾지 못함")
        return 1
    raw = m.group(1).replace("<\\/", "</")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"✗ 인라인 JSON 파싱 실패: {exc}")
        return 1

    arts = data.get("articles") or []
    print(f"· 인라인 기사 {len(arts)}건 / 생성 {data.get('generated_at_label')}")

    if len(arts) < 30:
        err(f"기사가 {len(arts)}건뿐 (최소 30건 필요)")

    # 3) data.json과 동일한지
    if DATA.exists():
        disk = json.loads(DATA.read_text(encoding="utf-8"))
        if len(disk.get("articles", [])) != len(arts):
            err("data.json과 index.html의 기사 수가 다름")
        if disk.get("generated_at") != data.get("generated_at"):
            err("data.json과 index.html의 생성 시각이 다름")
    else:
        err("data.json 없음")

    now = datetime.now(KST)
    seen_links: set[str] = set()
    cats: dict[str, int] = {c: 0 for c in CATS}
    for i, a in enumerate(arts):
        tag = f"[{i + 1}] {str(a.get('title'))[:30]}"
        title = (a.get("title") or "").strip()
        if not title:
            err(f"{tag}: 제목이 비어 있음")
        if len(title) < 5:
            warn(f"{tag}: 제목이 너무 짧음")
        if re.search(r"<[a-z/]", title, re.I):
            err(f"{tag}: 제목에 HTML 태그가 남아 있음")

        s = a.get("score")
        if not isinstance(s, int) or isinstance(s, bool) or not (0 <= s <= 100):
            err(f"{tag}: 점수가 0~100 정수가 아님 ({s!r})")
        for k, v in (a.get("metrics") or {}).items():
            if not isinstance(v, int) or not (0 <= v <= 100):
                err(f"{tag}: 지표 {k} 값 이상 ({v!r})")

        if a.get("category") not in CATS:
            err(f"{tag}: 카테고리 이상 ({a.get('category')!r})")
        else:
            cats[a["category"]] += 1

        link = a.get("link") or ""
        if not link.startswith("http"):
            err(f"{tag}: 링크가 http(s)가 아님")
        if "news.google.com" in link:
            warn(f"{tag}: 구글뉴스 링크가 남아 있음")
        if link in seen_links:
            err(f"{tag}: 링크 중복")
        seen_links.add(link)

        if not a.get("publisher"):
            err(f"{tag}: 언론사 없음")

        summary = (a.get("summary") or "").strip()
        if len(summary) < 12:
            err(f"{tag}: 요약이 비었거나 너무 짧음")
        if "NaN" in json.dumps(a, ensure_ascii=False) or "None" in json.dumps(a, ensure_ascii=False):
            err(f"{tag}: NaN/None 값 포함")

        ts = a.get("published_ts")
        if not isinstance(ts, int):
            err(f"{tag}: 발행 타임스탬프 이상")
        else:
            dt = datetime.fromtimestamp(ts / 1000, KST)
            if dt > now + timedelta(minutes=10):
                err(f"{tag}: 발행 시각이 미래")
            if dt < now - timedelta(hours=49):
                warn(f"{tag}: 48시간 밖 기사")
            iso = a.get("published_at") or ""
            try:
                if datetime.fromisoformat(iso) != dt:
                    err(f"{tag}: published_at과 타임스탬프 불일치")
            except ValueError:
                err(f"{tag}: published_at 형식 오류 ({iso!r})")

        if not a.get("viral_point"):
            err(f"{tag}: 바이럴 포인트 없음")
        if not a.get("tags"):
            warn(f"{tag}: 태그 없음")

    # 4) 카테고리 분포
    print("· 카테고리:", ", ".join(f"{k} {v}" for k, v in cats.items()))
    for c, n in cats.items():
        if n == 0:
            warn(f"카테고리 '{c}' 0건")

    # 5) 순위 정합성
    ranks = [a.get("rank") for a in arts]
    if ranks != list(range(1, len(arts) + 1)):
        err("순위가 1..N 연속이 아님")

    # 6) 마크업/스크립트 기본 점검
    if html.count("<script") != html.count("</script>"):
        err("script 태그 개수 불일치")
    for dom_id in ["main", "filters", "tabbar", "sheet", "news-data", "toast", "timechips", "catchips"]:
        if f'id="{dom_id}"' not in html:
            err(f"필수 DOM id 누락: {dom_id}")
    if "localStorage" not in html:
        warn("localStorage 사용 코드 없음(북마크 저장 확인 필요)")
    if "430px" not in html:
        err("최대 폭 430px 스타일 없음")

    # 7) JSON 직렬화 왕복
    if json.loads(json.dumps(data, ensure_ascii=False)) != data:
        err("JSON 왕복 불일치")

    print()
    for w in warns[:12]:
        print("⚠", w)
    if len(warns) > 12:
        print(f"⚠ … 외 {len(warns) - 12}건")
    if errors:
        print(f"\n✗ 오류 {len(errors)}건")
        for e in errors[:25]:
            print("  -", e)
        return 1
    print(f"\n✓ 검증 통과: {len(arts)}건, 경고 {len(warns)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
