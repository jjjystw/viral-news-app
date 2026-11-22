#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
바이럴 국내뉴스 수집기
=====================
국내 주요 언론사의 공개 RSS 피드(연합뉴스·경향·조선·노컷·오마이·머니투데이, 그리고
구글 뉴스(한국어)를 경유한 SBS·KBS·MBC·한겨레)를 모아
  - 카테고리 자동 분류 (사건사고 / 얼탱없음 / 감동 / 이슈)
  - 바이럴 점수(0~100) + 세부 지표(확산도·속보성·화제성) 산출
  - 한 줄 요약 / 바이럴 포인트 생성
후, `data.json`과 `index.html`(데이터 인라인 주입)을 함께 최신화한다.

실행:
    python3 viral-news-app/fetch_news.py            # 수집 + 빌드
    python3 viral-news-app/fetch_news.py --rebuild   # 기존 data.json으로 HTML만 재빌드
    python3 viral-news-app/fetch_news.py --hours 24  # 수집 시간창 변경
"""
from __future__ import annotations

import argparse
import base64
import gzip
import html as html_lib
import io
import json
import math
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9))
APP_DIR = Path(__file__).resolve().parent
DATA_PATH = APP_DIR / "data.json"
HTML_PATH = APP_DIR / "index.html"
TEMPLATE_PATH = APP_DIR / "template.html"
CACHE_DIR = APP_DIR / ".cache"
GOOGLE_CACHE = CACHE_DIR / "google_links.json"
META_CACHE = CACHE_DIR / "page_meta.json"

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TIME_OUT = 20
_HOST_LOCK = threading.Lock()
_HOST_LAST: dict[str, float] = {}

# ----------------------------------------------------------------------------
# 수집 소스
# ----------------------------------------------------------------------------
# kind: "rss"  -> 언론사 공개 RSS
#       "gnews" -> 구글뉴스 한국어 RSS(언론사별 검색/토픽). 기사 링크는 원문 언론사
#                  주소로 해석(resolve)한다.
SOURCES = [
    {"id": "yna",    "name": "연합뉴스",     "kind": "rss",   "url": "https://www.yna.co.kr/rss/news.xml", "publisher": "연합뉴스"},
    {"id": "ynasoc", "name": "연합뉴스 사회", "kind": "rss",   "url": "https://www.yna.co.kr/rss/society.xml", "publisher": "연합뉴스"},
    {"id": "khan",   "name": "경향신문",     "kind": "rss",   "url": "https://www.khan.co.kr/rss/rssdata/total_news.xml", "publisher": "경향신문"},
    {"id": "chosun", "name": "조선일보",     "kind": "rss",   "url": "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml", "publisher": "조선일보"},
    {"id": "nocut",  "name": "노컷뉴스",     "kind": "rss",   "url": "https://rss.nocutnews.co.kr/nocutnews.xml", "publisher": "노컷뉴스"},
    {"id": "ohmy",   "name": "오마이뉴스",   "kind": "rss",   "url": "https://rss.ohmynews.com/rss/ohmynews.xml", "publisher": "오마이뉴스"},
    {"id": "mt",     "name": "머니투데이",   "kind": "rss",   "url": "https://rss.mt.co.kr/mt_news.xml", "publisher": "머니투데이"},
    {"id": "sbs",    "name": "SBS 뉴스",     "kind": "gnews", "q": "site:news.sbs.co.kr", "publisher": "SBS 뉴스"},
    {"id": "kbs",    "name": "KBS 뉴스",     "kind": "gnews", "q": "site:news.kbs.co.kr", "publisher": "KBS 뉴스"},
    {"id": "mbc",    "name": "MBC 뉴스",     "kind": "gnews", "q": "site:imnews.imbc.com", "publisher": "MBC 뉴스"},
    {"id": "hani",   "name": "한겨레",       "kind": "gnews", "q": "site:hani.co.kr", "publisher": "한겨레"},
    {"id": "ghead",  "name": "구글뉴스 헤드라인", "kind": "gnews", "url": "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko", "publisher": ""},
    {"id": "gsoc",   "name": "구글뉴스 사회", "kind": "gnews", "url": "https://news.google.com/rss/headlines/section/topic/NATION?hl=ko&gl=KR&ceid=KR:ko", "publisher": ""},
]


def gnews_search_url(q: str, when: str = "when:2d") -> str:
    return (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(f"{when} {q}")
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _decode(raw: bytes, declared: str | None = None) -> str:
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif raw[:3] == b"BZh":
        import bz2
        try:
            raw = bz2.decompress(raw)
        except OSError:
            pass
    elif raw[:1] == b"\x78":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    for enc in [declared, "utf-8", "euc-kr", "cp949"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(url: str, timeout: int = TIME_OUT, retries: int = 2,
             ua: str = UA_MOBILE, headers: dict | None = None) -> tuple[str, str]:
    """(text, final_url) 반환. 실패 시 예외."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            hdrs = {
                "User-Agent": ua,
                "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Connection": "close",
            }
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                ctype = resp.headers.get_content_charset()
                return _decode(raw, ctype), resp.geturl()
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
                ssl.SSLError, ConnectionError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                wait = 0.8 * (attempt + 1) + random.random() * 0.4
                code = getattr(exc, "code", None)
                if code == 429:
                    wait = 3.0 + attempt * 2
                time.sleep(wait)
    raise last if last else RuntimeError("unknown fetch error")


# ----------------------------------------------------------------------------
# 텍스트 유틸
# ----------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
WS_RE = re.compile(r"\s+")

NOISE_PATTERNS = [
    re.compile(r"^\([가-힣]{2,8}=[^)]{2,20}\)\s*"),           # (서울=연합뉴스)
    re.compile(r"^\([가-힣]{2,8}=[^)]{2,20}\)\s*[가-힣]{2,5}\s*기자\s*=\s*"),
    re.compile(r"^[가-힣]{2,5}\s*(기자|특파원)\s*=\s*"),
    re.compile(r"^\[?(앵커|리포트|영상|사진|그래픽|인터뷰)\]?\s*[:：]?\s*"),
    re.compile(r"^(사진|자료사진|연합뉴스 자료사진)\s*=\s*.*$"),
    re.compile(r"▶\s*[^▶]{0,80}$"),
    re.compile(r"☞\s*[^☞]{0,80}$"),
    re.compile(r"(무단\s*전재|재배포\s*금지|저작권자|Copyright|ⓒ|©).*$", re.I),
    re.compile(r"(구독|제보|팟캐스트|카카오톡|텔레그램)\s*(하기|채널|바로가기|:@).*$"),
    re.compile(r"\[[^\]]{0,20}(속보|종합|단독|1보|2보)[^\]]{0,20}\]\s*"),
]

TITLE_PREFIX_RE = re.compile(
    r"^\s*[\[【(]?\s*(속보|긴급|단독|종합\d?보?|1보|2보|3보|앵커|인터뷰|포토|영상|사설|칼럼|특징|기고)\s*[\]】)]?\s*[:：]?\s*"
)
GSOURCE_SUFFIX_RE = re.compile(r"\s+[-–—]\s+[^-–—]{2,24}$")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = SCRIPT_RE.sub(" ", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return WS_RE.sub(" ", text).strip()


def clean_summary(text: str, limit: int = 120) -> str:
    t = strip_html(text)
    if not t:
        return ""
    for pat in NOISE_PATTERNS:
        t = pat.sub("", t).strip()
    t = re.sub(r"^[\s·•\-–—|]+", "", t)
    t = re.sub(r"([.?!])\1+", r"\1", t)
    t = WS_RE.sub(" ", t).strip()
    if len(t) < 8:
        return ""
    # 문장 단위로 자르기
    if len(t) > limit:
        cut = t[:limit]
        m = list(re.finditer(r"[.!?。]\s", cut))
        if m and m[-1].start() > limit * 0.45:
            t = cut[: m[-1].start() + 1]
        else:
            sp = cut.rfind(" ")
            t = (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,·…") + "…"
    t = t.strip()
    if t.endswith("다.") or t.endswith(".") or t.endswith("…") or t.endswith("?"):
        return t
    return t + "…" if not t.endswith(("요", "죠")) else t + "."


def clean_title(title: str) -> str:
    t = strip_html(title)
    t = re.sub(r"^\[?(속보|긴급|단독|종합|1보|2보|3보)\]?\s*", lambda m: f"[{m.group(1)}] " if m.group(1) in ("속보", "단독") else "", t)
    t = WS_RE.sub(" ", t).strip()
    return t


def norm_key(title: str) -> str:
    t = re.sub(r"\[(속보|단독|긴급|종합|1보|2보)\]", "", title)
    t = re.sub(r"[^가-힣a-zA-Z0-9]", "", t)
    return t.lower()


TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}|\d+")
STOPWORDS = set("""
그리고 그러나 하지만 이번 지난 오늘 어제 내일 관련 대해 통해 위한 이라고 라고 에서 으로 하고 있는 있다 없다
사람 명 참여 이날 지금 우리 이들 그런 이런 저런 가장 모두 다시 이어 또 대한 경우 예정 밝혀 따르 이라
""".split())


def tokens(text: str) -> set[str]:
    out = set()
    for tok in TOKEN_RE.findall(text):
        if tok in STOPWORDS:
            continue
        if len(tok) >= 4 and tok.endswith(("에서", "으로", "하고", "했다", "이다", "됐다")):
            tok = tok[: -2]
        if len(tok) >= 2:
            out.add(tok)
    return out


def bigrams(text: str) -> set[str]:
    t = re.sub(r"[^가-힣a-zA-Z0-9]", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a_title: str, b_title: str) -> float:
    """제목 두 개가 같은 사안인지 0~1로 추정."""
    ta, tb = tokens(a_title), tokens(b_title)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    jac = inter / len(ta | tb)
    small = min(len(ta), len(tb))
    contain = inter / small if small >= 3 else 0.0
    ba, bb = bigrams(a_title), bigrams(b_title)
    dice = 2 * len(ba & bb) / (len(ba) + len(bb)) if ba and bb else 0.0
    score = max(jac, contain * 0.9, dice * 0.9)
    if inter < 2:
        # 한 단어만 겹치는 것으로 확산 판정하지 않는다
        score = min(score, 0.4)
    return score


# ----------------------------------------------------------------------------
# 기사 모델
# ----------------------------------------------------------------------------
@dataclass
class Article:
    title: str
    link: str
    publisher: str
    source_name: str
    published: datetime | None
    summary: str = ""
    image: str = ""
    is_breaking: bool = False
    category: str = "이슈"
    score: int = 0
    metrics: dict = field(default_factory=dict)
    outlets: list[str] = field(default_factory=list)
    cluster: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    viral_point: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        return norm_key(self.title)


# ----------------------------------------------------------------------------
# RSS 파싱
# ----------------------------------------------------------------------------
NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom",
}


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    # RFC 822
    try:
        dt = parsedate_to_datetime(v)
        if dt:
            return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO 8601
    iso = v.replace("Z", "+00:00")
    iso = re.sub(r"\.\d+", "", iso)
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
    except ValueError:
        pass
    # 2026.11.22 21:05 / 2026-11-22 21:05
    m = re.search(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})(?:[ T](\d{2}):(\d{2}))?", v)
    if m:
        y, mo, d, hh, mi = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), tzinfo=KST)
        except ValueError:
            return None
    return None


def first_nonempty(el: ET.Element, paths: list[str]) -> str:
    for p in paths:
        found = el.find(p, NS) if ":" in p else el.find(p)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def extract_image(el: ET.Element, desc_html: str) -> str:
    for p in ["media:content", "media:thumbnail", "enclosure"]:
        node = el.find(p, NS)
        if node is not None:
            url = node.get("url") or ""
            if url.startswith("http") and re.search(r"\.(jpg|jpeg|png|webp)", url, re.I):
                return url
    m = re.search(r'<img[^>]+src=["\']?(https?://[^"\'>\s]+)', desc_html or "", re.I)
    if m:
        return m.group(1)
    return ""


BREAKING_RE = re.compile(r"(\[속보\]|\[긴급\]|\[1보\]|\[2보\]|속보\s*[:：]|긴급\s*속보|breaking)", re.I)


def parse_rss(source: dict, raw: str) -> list[Article]:
    out: list[Article] = []
    try:
        root = ET.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except ET.ParseError:
        # 일부 피드의 잘못된 엔티티 보정
        fixed = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", raw)
        try:
            root = ET.fromstring(fixed.encode("utf-8"))
        except ET.ParseError:
            return out
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    is_gnews = source["kind"] == "gnews"
    for it in items:
        title = strip_html(first_nonempty(it, ["title", "atom:title"]))
        if not title:
            continue
        link = first_nonempty(it, ["link", "atom:link"])
        if not link:
            ln = it.find("atom:link", NS)
            link = (ln.get("href") if ln is not None else "") or ""
        guid = first_nonempty(it, ["guid"])
        if not link and guid.startswith("http"):
            link = guid
        if not link:
            continue
        desc_html = first_nonempty(it, ["description", "content:encoded", "atom:summary", "atom:content"])
        date_txt = first_nonempty(it, ["pubDate", "dc:date", "published", "updated", "atom:published", "atom:updated"])
        published = parse_date(date_txt)

        publisher = source.get("publisher") or ""
        if is_gnews:
            src_node = it.find("source")
            src_name = strip_html(src_node.text) if (src_node is not None and src_node.text) else ""
            # 제목 끝의 " - 언론사" 제거
            m = re.search(r"\s[-–—]\s([^-–—]{2,24})$", title)
            if m:
                title = title[: m.start()].strip()
            publisher = src_name or publisher
            # 구글뉴스 description은 관련기사 링크 목록 -> 요약으로 쓰지 않음
            desc_html = ""
        else:
            title = clean_title(title)

        is_breaking = bool(BREAKING_RE.search(title))
        art = Article(
            title=title.strip(),
            link=link.strip(),
            publisher=norm_publisher(publisher.strip() or source["name"]),
            source_name=source["name"],
            published=published,
            summary=clean_summary(desc_html),
            image=extract_image(it, desc_html),
            is_breaking=is_breaking,
        )
        out.append(art)
    return out


# ----------------------------------------------------------------------------
# 구글뉴스 링크 -> 원문 언론사 링크 해석
# ----------------------------------------------------------------------------
def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


GNEWS_ID_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([A-Za-z0-9_\-]+)")



def _unescape_url(raw: str) -> str:
    """구글 응답의 JSON 이스케이프(BS+u003d 등)를 실제 URL 문자로 되돌린다."""
    bs = chr(92)
    out = (raw.replace(bs + "u003d", "=")
              .replace(bs + "u0026", "&")
              .replace(bs + "u003f", "?")
              .replace(bs + "/", "/"))
    out = out.replace(bs, "").strip().strip('"')
    return out


def resolve_google_link(url: str, cache: dict) -> str:
    """구글뉴스 기사 링크(rss/articles/CBMi...)를 원문 언론사 주소로 바꾼다."""
    m = GNEWS_ID_RE.search(url)
    if not m:
        return url
    art_id = m.group(1)
    if art_id in cache:
        return cache[art_id]
    try:
        _throttle("https://news.google.com/", 0.3)
        page, _final = http_get(f"https://news.google.com/rss/articles/{art_id}", timeout=15, retries=1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        if not ts or not sg:
            return url
        payload = [
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "KR:ko", None, 1, None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            art_id, ts.group(1), sg.group(1),
        ]
        freq = json.dumps([[["Fbv4je", json.dumps(payload)]]])
        body = ("f.req=" + urllib.parse.quote(freq)).encode()
        req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body,
            headers={
                "User-Agent": UA_MOBILE,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Connection": "close",
            },
        )
        _throttle("https://news.google.com/", 0.3)
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = _decode(resp.read())

        target = ""
        idx = res.find("garturlres")
        if idx >= 0:
            k = res.find("http", idx)
            if k >= 0:
                e1 = res.find('"', k)
                target = res[k:e1 if e1 > k else len(res)]
        target = _unescape_url(target)
        if target.startswith("http") and "news.google.com" not in target:
            cache[art_id] = target
            return target
    except Exception:
        pass
    return url


def norm_publisher(name: str) -> str:
    """'news.sbs.co.kr' 같은 도메인형 이름을 언론사 이름으로 정리."""
    n = (name or "").strip()
    n = re.sub(r"^https?://", "", n).strip("/")
    host = n.lower()
    aliases = (
        ("news.sbs.co.kr", "SBS 뉴스"), ("sbs.co.kr", "SBS 뉴스"),
        ("news.kbs.co.kr", "KBS 뉴스"), ("kbs.co.kr", "KBS 뉴스"),
        ("imnews.imbc.com", "MBC 뉴스"), ("imbc.com", "MBC 뉴스"), ("mbc.co.kr", "MBC 뉴스"),
        ("hani.co.kr", "한겨레"), ("yna.co.kr", "연합뉴스"), ("khan.co.kr", "경향신문"),
        ("chosun.com", "조선일보"), ("nocutnews.co.kr", "노컷뉴스"),
        ("ohmynews.com", "오마이뉴스"), ("mt.co.kr", "머니투데이"),
    )
    for key, val in aliases:
        if host == key or host.endswith("." + key):
            return val
    if " - " in n or " | " in n:
        n = re.split(r"\s*[-–—|]\s*", n)[0].strip()
    return n or "언론사"


def _throttle(url: str, interval: float = 0.5) -> None:
    """같은 호스트에는 최소 interval 간격으로 요청한다."""
    host = urllib.parse.urlparse(url).netloc
    with _HOST_LOCK:
        t = _HOST_LAST.get(host, 0.0)
        now = time.time()
        slot = max(now, t + interval)
        _HOST_LAST[host] = slot
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


BOILERPLATE = (
    "대한민국 뉴스의 기준", "시청자의 눈높이", "Comprehensive up-to-date",
    "Google 뉴스에서", "무단전재", "재배포 금지", "저작권자",
)


def fetch_page_meta(url: str, cache: dict) -> tuple[str, str]:
    """언론사 기사 페이지에서 og:description / og:image 추출."""
    if url in cache:
        v = cache[url]
        if v.get("d") or v.get("i"):
            return (v.get("d", ""), v.get("i", ""))
        # 실패로 비어 있는 캐시는 하루 뒤에 다시 시도한다
        if time.time() - float(v.get("t", 0)) < 86400:
            return ("", "")
    desc, image = "", ""
    if "news.google.com" not in url:
        try:
            _throttle(url)
            page, final = http_get(url, timeout=12, retries=2, ua=UA_DESKTOP)
            for pat in (
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            ):
                m = re.search(pat, page, re.I)
                if m:
                    cand = strip_html(m.group(1))
                    if len(cand) > 20 and not any(b in cand for b in BOILERPLATE):
                        desc = cand
                        break
            for pat in (
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            ):
                m = re.search(pat, page, re.I)
                if m and m.group(1).startswith("http"):
                    image = m.group(1)
                    break
        except Exception:
            pass
    cache[url] = {"d": desc, "i": image, "t": int(time.time())}
    return desc, image


# ----------------------------------------------------------------------------
# 카테고리 분류
# ----------------------------------------------------------------------------
# (키워드, 가중치) — 3.4: 제목에 있으면 그 자체로 강한 신호 / 1.6: 보조 신호
CATEGORY_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "사건사고": [
        ("사고", 3.4), ("화재", 3.4), ("불이", 3.4), ("추락", 3.4), ("전복", 3.4), ("충돌", 3.0),
        ("폭발", 3.4), ("붕괴", 3.4), ("침몰", 3.4), ("침수", 3.0), ("실종", 3.4), ("사망", 3.4),
        ("숨져", 3.4), ("숨진", 3.4), ("변사", 3.0), ("시신", 3.0), ("살인", 3.4), ("피살", 3.4),
        ("폭행", 3.4), ("폭력", 3.0), ("성폭행", 3.4), ("성추행", 3.4), ("마약", 3.4), ("음주운전", 3.4),
        ("음주사고", 3.4), ("뺑소니", 3.4), ("흉기", 3.4), ("총기", 3.4), ("방화", 3.4), ("강도", 3.0),
        ("절도", 3.0), ("납치", 3.4), ("감금", 3.4), ("검거", 3.4), ("체포", 3.4), ("구속", 3.4),
        ("기소", 3.2), ("송치", 3.0), ("영장", 3.0), ("선고", 3.2), ("판결", 3.2), ("압수수색", 3.2),
        ("익사", 3.4), ("질식", 3.4), ("압사", 3.4), ("감전", 3.0), ("테러", 3.4), ("참사", 3.4),
        ("경찰", 1.7), ("검찰", 1.7), ("법원", 1.7), ("재판", 1.7), ("수사", 1.6), ("피의자", 1.8),
        ("피해", 1.5), ("부상", 2.0), ("중상", 1.8), ("구조대", 1.8), ("소방", 1.8), ("출동", 1.4),
        ("통제", 1.4), ("대피", 1.7), ("사건", 1.5), ("고소", 1.4), ("고발", 1.4), ("소송", 1.4),
        ("처벌", 1.5), ("징역", 2.0), ("벌금", 1.4), ("태풍", 2.4), ("지진", 2.6), ("산사태", 2.6),
        ("폭우", 2.0), ("폭설", 2.0), ("정전", 1.6), ("누출", 1.6), ("유출", 1.4),
    ],
    "얼탱없음": [
        ("황당", 3.4), ("어이없", 3.4), ("어처구니", 3.4), ("기가 차", 3.4), ("기막히", 3.2),
        ("갑질", 3.4), ("민폐", 3.4), ("진상", 3.0), ("기행", 3.4), ("만행", 3.2), ("소동", 3.0),
        ("해프닝", 3.4), ("촌극", 3.4), ("난동", 3.2), ("막말", 3.2), ("폭언", 3.2), ("횡령", 3.4),
        ("먹튀", 3.4), ("사칭", 3.4), ("무개념", 3.4), ("빌런", 3.4), ("몰상식", 3.4), ("뻔뻔", 3.4),
        ("적반하장", 3.4), ("추태", 3.0), ("구설", 2.6), ("몰카", 3.4), ("뻘짓", 3.4), ("생떼", 3.2),
        ("막무가내", 3.2), ("엽기", 3.2), ("괴담", 2.6), ("헛소리", 3.2), ("황당무계", 3.4),
        ("논란", 1.6), ("구설수", 1.8), ("충격", 1.4), ("경악", 2.4), ("아찔", 2.0), ("화들짝", 2.2),
        ("실소", 2.2), ("당황", 1.8), ("망신", 2.2), ("사기", 1.6), ("꼼수", 2.0), ("편법", 1.8),
        ("탈세", 2.0), ("무단", 1.6), ("불법", 1.5), ("위반", 1.4), ("과태료", 1.8), ("커닝", 2.4),
        ("부정행위", 2.2), ("앞치마", 2.0), ("갈취", 2.4), ("도둑", 2.0), ("발뺌", 2.4), ("짜증", 2.0),
        ("분통", 1.8), ("열불", 2.0), ("코미디", 2.2), ("웃픈", 2.6), ("현타", 2.4), ("엉뚱", 2.4),
        ("횡포", 2.2), ("허위", 1.6), ("가짜", 1.5),
    ],
    "감동": [
        ("감동", 3.4), ("훈훈", 3.4), ("미담", 3.4), ("선행", 3.4), ("기부", 3.4), ("봉사", 3.2),
        ("나눔", 3.2), ("기적", 3.4), ("생환", 3.4), ("따뜻", 3.2), ("헌신", 3.2), ("장기기증", 3.4),
        ("효행", 3.4), ("의인", 3.4), ("재회", 3.0), ("극복", 2.8), ("위로", 2.6), ("칭찬", 2.8),
        ("보은", 3.0), ("성금", 3.2), ("후원", 2.8), ("장학", 2.6), ("무사히", 3.0), ("전원 구조", 3.4),
        ("모두 구조", 3.4), ("살렸", 3.2), ("목숨", 2.6), ("구조했다", 3.2), ("구했다", 3.0),
        ("희망", 1.8), ("감사", 1.8), ("고마", 2.2), ("격려", 2.2), ("응원", 1.8), ("사랑", 1.4),
        ("가족", 1.2), ("우정", 1.8), ("용기", 1.8), ("도움", 1.6), ("치료비", 2.6), ("합격", 1.6),
        ("선물", 1.4), ("축하", 1.4), ("이웃", 1.4), ("눈물", 1.7), ("화해", 1.8), ("봉양", 2.4),
    ],
}

STRONG_W = 3.4
NORM_W = 1.7
SUMMARY_W = 0.38

BUZZ_STRONG = [
    "충격", "경악", "황당", "분노", "눈물", "감동", "기적", "소름", "논란", "파문",
    "역대급", "사상 최초", "최악", "최고", "발칵", "들썩", "화제", "인기", "폭발적", "역주행",
]
CURIOSITY = ["왜", "어떻게", "진짜", "결국", "알고보니", "알고 보니", "그 이유", "정체", "반전"]
SUPERLATIVE = ["최초", "최악", "최고", "역대", "사상", "유일", "1위", "세계 첫", "국내 첫"]


def _kw_score(title: str, summary: str, words: list[tuple[str, float]]) -> tuple[float, list[str], int]:
    sc = 0.0
    found: list[str] = []
    strong_hit = 0
    for w, base in words:
        if w in title:
            sc += base
            found.append(w)
            if base >= 3.0:
                strong_hit += 1
        elif summary and w in summary:
            sc += base * SUMMARY_W
            found.append(w)
    return sc, found, strong_hit


POLITICAL_RE = re.compile(
    r"(대통령|국회|여당|야당|민주당|국민의힘|청와대|정부|장관|차관|탄핵|법사위|원내대표|총리|국정감사|공소|검찰총장|대법원장|여\s*[\"“]|야\s*[\"“])"
)
SPORTS_RE = re.compile(
    r"(연승|연패|승리|패배|홈런|득점|결승|리그|투어|선발|구원|감독|코치|월드컵|올림픽|KBO|K리그|V리그|LPGA|메달|완승|선두|경기|선수)"
)
VIOLENT_RE = re.compile(
    r"(사망|숨져|숨진|화재|폭행|살인|성폭행|성추행|검거|체포|구속|흉기|음주|마약|실종|추락|전복|붕괴|폭발|참사|사고|부상|익사|질식)"
)


def classify(title: str, summary: str) -> tuple[str, dict]:
    # '진상규명'·'사기 진작'처럼 다른 뜻으로 쓰인 단어는 분류에서 제외
    t = title.replace("진상규명", "규명").replace("진상 규명", "규명")
    t = t.replace("사기 진작", "고무").replace("사기진작", "고무")
    s = (summary or "").replace("진상규명", "규명").replace("진상 규명", "규명")
    s = s.replace("사기 진작", "고무").replace("사기진작", "고무")

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    strong: dict[str, int] = {}
    for cat, words in CATEGORY_KEYWORDS.items():
        sc, found, sh = _kw_score(t, s, words)
        scores[cat] = sc
        hits[cat] = found
        strong[cat] = sh

    # 후보 적격성
    eligible: list[tuple[float, str]] = []
    if scores["사건사고"] >= 2.4:
        eligible.append((scores["사건사고"], "사건사고"))
    if scores["얼탱없음"] >= 2.4 and (strong["얼탱없음"] >= 1 or scores["얼탱없음"] >= 3.2):
        eligible.append((scores["얼탱없음"] + 0.3, "얼탱없음"))
    if scores["감동"] >= 2.4 and (strong["감동"] >= 1 or scores["감동"] >= 3.4):
        eligible.append((scores["감동"] + 0.45, "감동"))

    cat = max(eligible, key=lambda x: x[0])[1] if eligible else "이슈"

    # 살인·성범죄 등 무거운 사건은 감동/얼탱없음으로 보내지 않는다
    if cat in ("감동", "얼탱없음") and re.search(r"(살인|피살|성폭행|성추행|시신|사망|숨져|숨진|참사)", title):
        cat = "사건사고"
    # 수사·재판·위법 맥락이면 '감동'이 아니다 (예: 기부 혐의로 송치)
    if cat == "감동" and re.search(
        r"(위반|송치|기소|혐의|구속|체포|영장|재판|판결|선고|수사|고발|고소|처벌|징역|음주|마약|횡령|사기|폭행|논란)",
        title,
    ):
        cat = "사건사고" if scores["사건사고"] >= 2.4 else "이슈"
    # 정치 절차·공방은 '사건사고'가 아니라 '이슈'
    if cat == "사건사고" and POLITICAL_RE.search(title) and not VIOLENT_RE.search(title):
        cat = "이슈"
    # 스포츠 경기 결과는 감동/사건사고로 분류하지 않는다
    if cat in ("감동", "사건사고") and SPORTS_RE.search(title) and not re.search(
        r"(사망|숨져|숨진|화재|폭행|살인|검거|체포|구속|음주|마약|성폭행|성추행)", title
    ):
        cat = "이슈"

    return cat, {"scores": scores, "hits": hits, "strong": strong}


# ----------------------------------------------------------------------------
# 바이럴 점수
# ----------------------------------------------------------------------------
def spread_metric(outlet_count: int, cluster_size: int) -> int:
    table = {1: 34, 2: 58, 3: 72, 4: 82, 5: 88, 6: 92, 7: 96}
    base = table.get(outlet_count, 99 if outlet_count >= 8 else 34)
    base = min(100, base + max(0, cluster_size - outlet_count))
    return int(round(base))


def breaking_metric(is_breaking: bool, minutes: float) -> int:
    base = 78 if is_breaking else 26
    if minutes <= 20:
        add = 22
    elif minutes <= 30:
        add = 21
    elif minutes <= 60:
        add = 19
    elif minutes <= 120:
        add = 15
    elif minutes <= 180:
        add = 11
    elif minutes <= 360:
        add = 7
    elif minutes <= 720:
        add = 3
    elif minutes <= 1440:
        add = -1
    else:
        add = -6
    return max(0, min(100, base + add))


IMPACT = [
    "대통령", "총리", "장관", "국회", "여당", "야당", "민주당", "국민의힘", "검찰", "법원",
    "탄핵", "사퇴", "면직", "파면", "구속", "영장", "참사", "사망", "숨져", "실종", "파업",
    "전쟁", "금리", "환율", "집값", "반도체", "AI", "수능", "의대", "연금",
]


def buzz_metric(title: str, summary: str, category: str) -> tuple[int, list[str]]:
    text = f"{title} {summary}"
    sc = 34.0
    found: list[str] = []
    hits = 0
    for w in BUZZ_STRONG:
        if w in text:
            sc += 8
            hits += 1
            found.append(w)
    imp = 0
    for w in IMPACT:
        if w in title:
            imp += 1
            if w not in found:
                found.append(w)
    sc += min(3, imp) * 6
    cur = sum(1 for w in CURIOSITY if w in text)
    sc += min(2, cur) * 5
    for w in SUPERLATIVE:
        if w in title:
            sc += 5
            found.append(w)
            break
    if re.search(r"\d", title):
        sc += 4
    if re.search(r"[\"\'“”‘’]", title):
        sc += 5
    if "?" in title:
        sc += 4
    if "!" in title:
        sc += 3
    if 18 <= len(title) <= 45:
        sc += 3
    if category == "이슈" and re.search(
        r"(연승|연패|승리|패배|홈런|득점|결승|리그|투어|선발|구원|감독|코치|월드컵|올림픽|KBO|K리그|V리그|LPGA)",
        title,
    ):
        sc -= 13
    if category == "얼탱없음":
        sc += 9
    elif category == "감동":
        sc += 7
    elif category == "사건사고":
        sc += 4
    return max(0, min(100, int(round(sc)))), found


def viral_score(spread: int, breaking: int, buzz: int, outlets: int) -> int:
    raw = 0.42 * spread + 0.27 * breaking + 0.31 * buzz
    if outlets >= 3:
        raw += 3
    if outlets >= 5:
        raw += 2
    if outlets >= 8:
        raw += 1
    return int(max(5, min(99, round(raw))))


def human_outlets(outlets: list[str]) -> str:
    names = []
    for o in outlets:
        o = o.strip()
        if not o:
            continue
        o = re.sub(r"\s*(뉴스|신문|일보|닷컴|방송)$", "", o).strip() or o
        if o not in names:
            names.append(o)
    return ", ".join(names)


def make_viral_point(category: str, outlets: int, minutes: float, is_breaking: bool,
                     buzz_words: list[str], hits: dict, title: str = "") -> str:
    parts: list[str] = []
    if outlets >= 6:
        parts.append(f"{outlets}개 매체가 동시에 받아쓴 확산형 이슈")
    elif outlets >= 4:
        parts.append(f"{outlets}개 매체가 함께 다룬 화제")
    elif outlets == 3:
        parts.append("3개 매체가 나란히 보도")
    elif outlets == 2:
        parts.append("2개 매체가 함께 다룬 사안")
    else:
        parts.append("단독 보도로 시작된 화제")

    if is_breaking and minutes <= 90:
        parts.append("속보 직후 반응이 몰리는 구간")
    elif is_breaking:
        parts.append("속보로 번지는 중")
    elif minutes <= 120:
        parts.append("게시 2시간 이내 신선한 이슈")
    elif minutes <= 360:
        parts.append("오늘 오후부터 회자된 이야기")
    elif minutes <= 720:
        parts.append("오늘 낮부터 이어진 관심")
    else:
        parts.append("하루 종일 회자된 사안")

    words = [w for w in buzz_words if 1 < len(w) <= 7][:2]
    kw = "·".join(words) if words else ""

    if category == "얼탱없음":
        parts.append(f"‘{kw}’ 장면에 어이없다는 반응" if kw else "어이없음 반응을 부르는 장면")
    elif category == "감동":
        parts.append(f"‘{kw}’ 사연이 훈훈하게 공유" if kw else "훈훈함으로 공유되는 사연")
    elif category == "사건사고":
        parts.append("피해·수사 진행 상황에 관심 집중")
    elif kw:
        parts.append(f"‘{kw}’ 키워드로 화제성 상승")
    elif re.search(r"(로또|당첨번호|연금복권|날씨|기온|주가|환율|금리|운세|부동산 시세)", title):
        parts.append("확인·검색 수요가 몰리는 생활 정보")
    else:
        parts.append("정치·사회 쟁점으로 논의 확대")

    return " · ".join(parts[:3])


def build_tags(category: str, outlets: int, is_breaking: bool, minutes: float,
               buzz_words: list[str], hits: dict) -> list[str]:
    tags: list[str] = []
    if is_breaking:
        tags.append("속보")
    if outlets >= 5:
        tags.append("확산")
    elif outlets >= 3:
        tags.append("동시보도")
    if minutes <= 60:
        tags.append("방금")
    elif minutes <= 180:
        tags.append("3시간")
    for w in hits.get(category, [])[:2]:
        tags.append(w)
    for w in buzz_words[:1]:
        if w not in tags:
            tags.append(w)
    seen, out = set(), []
    for t in tags:
        if t not in seen and len(t) <= 8:
            seen.add(t)
            out.append(t)
    if not out:
        out = [category if category != "이슈" else "화제"]
    elif len(out) < 2:
        out.append(category if category not in out else "화제")
    return out[:5]


# ----------------------------------------------------------------------------
# 클러스터링 (확산도: 같은 사안을 다룬 매체 수)
# ----------------------------------------------------------------------------
def compute_spreads(articles: list[Article], hi: float = 0.42, lo: float = 0.33) -> None:
    """같은 사안을 다룬 기사 묶음과 '동시 보도 매체 수'를 계산한다.

    - hi(0.42) 이상: 사실상 같은 헤드라인 → 같은 사안
    - lo(0.33) 이상이면서 고유명사(df가 낮은 토큰)를 공유: 같은 사안의 다른 각도
    토큰 IDF로 '사고·경찰' 같은 흔한 단어의 영향력을 줄인다.
    """
    n = len(articles)
    toks = [tokens(a.title) for a in articles]
    bags = [bigrams(a.title) for a in articles]
    df: dict[str, int] = {}
    for ts in toks:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    w = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
    wsum = [sum(w[t] for t in ts) for ts in toks]
    distinctive_df = max(12, int(n * 0.02))

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        ti, bi = toks[i], bags[i]
        if not ti:
            continue
        for j in range(i + 1, n):
            tj, bj = toks[j], bags[j]
            if not tj:
                continue
            shared = ti & tj
            if not shared:
                continue
            sw = sum(w[t] for t in shared)
            wi, wj = wsum[i], wsum[j]
            cont = sw / min(wi, wj) if min(wi, wj) > 0 else 0.0
            jac = sw / (wi + wj - sw) if (wi + wj - sw) > 0 else 0.0
            dice = 2 * len(bi & bj) / (len(bi) + len(bj)) if bi and bj else 0.0
            sim = max(jac, cont * 0.9, dice * 0.85)
            if len(shared) < 2:
                sim = min(sim, 0.4)
            if sim >= hi:
                union(i, j)
            elif sim >= lo and any(df[t] <= distinctive_df for t in shared):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    for members in groups.values():
        outlets: list[str] = []
        for idx in members:
            p = articles[idx].publisher
            if p and p not in outlets:
                outlets.append(p)
        for idx in members:
            articles[idx].outlets = outlets
            articles[idx].cluster = members


# ----------------------------------------------------------------------------
# 파이프라인
# ----------------------------------------------------------------------------
def fetch_source(source: dict) -> tuple[dict, list[Article], str]:
    url = source.get("url") or gnews_search_url(source["q"])
    try:
        raw, _final = http_get(url)
    except Exception as exc:  # noqa: BLE001
        return ({"name": source["name"], "status": "error", "error": str(exc)[:120], "count": 0}, [], str(exc))
    arts = parse_rss(source, raw)
    return ({"name": source["name"], "status": "ok", "count": len(arts)}, arts, "")


def dedupe(articles: list[Article]) -> list[Article]:
    """같은 언론사 + 같은 제목(유사) 기사 제거. 정보가 많은 쪽 유지."""
    kept: list[Article] = []
    by_pub: dict[str, list[Article]] = {}
    for a in articles:
        pub = a.publisher
        dup = None
        for b in by_pub.get(pub, []):
            if a.key == b.key or similarity(a.title, b.title) >= 0.82:
                dup = b
                break
        if dup is None:
            by_pub.setdefault(pub, []).append(a)
            kept.append(a)
        else:
            # 요약/이미지/시각이 더 좋은 쪽으로 교체
            score_new = (bool(a.summary) * 2 + bool(a.image) + (a.published is not None))
            score_old = (bool(dup.summary) * 2 + bool(dup.image) + (dup.published is not None))
            if score_new > score_old:
                dup.summary = a.summary or dup.summary
                dup.image = a.image or dup.image
                dup.published = a.published or dup.published
                dup.link = a.link if "news.google.com" not in a.link else dup.link
    return kept


def enrich(articles: list[Article], workers: int = 4) -> None:
    """구글뉴스 링크 해석 + 부족한 요약/이미지 보강."""
    gcache = _load_json(GOOGLE_CACHE)
    mcache = _load_json(META_CACHE)
    targets = [a for a in articles if "news.google.com" in a.link]
    if targets:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(resolve_google_link, a.link, gcache): a for a in targets}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    a.link = fut.result()
                except Exception:
                    pass
        # 동시 요청에서 구글이 거절한 건은 순서대로 한 번 더 시도
        for a in [x for x in targets if "news.google.com" in x.link]:
            a.link = resolve_google_link(a.link, gcache)
        _save_json(GOOGLE_CACHE, gcache)

    need = [a for a in articles if (not a.summary) or (not a.image)]
    if need:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_page_meta, a.link, mcache): a for a in need}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    desc, image = fut.result()
                except Exception:
                    continue
                if desc and not a.summary:
                    a.summary = clean_summary(desc)
                if image and not a.image:
                    a.image = image
        _save_json(META_CACHE, mcache)


def summarize_fallback(a: Article) -> str:
    if a.summary:
        return a.summary
    t = re.sub(r"\[[^\]]+\]", "", a.title).strip(" -–—")
    cat_hint = {
        "사건사고": "사고 경위와 피해 상황이 확인되고 있다",
        "얼탱없음": "황당한 정황이 알려지며 누리꾼 반응이 이어지고 있다",
        "감동": "잔잔한 감동을 주는 사연이 전해졌다",
        "이슈": "관련 보도가 이어지고 있다",
    }[a.category]
    if len(t) > 82:
        t = t[:82].rstrip(" ,·…") + "…"
    return f"{t} — {cat_hint}."


def collapse_stories(scored: list[Article]) -> list[Article]:
    """같은 사안을 다룬 여러 매체 기사는 대표 1건만 남긴다(피드 중복 방지).

    대표는 점수·확산도·요약 품질이 가장 좋은 기사를 고른다.
    """
    for i, a in enumerate(scored):
        a._idx = i  # type: ignore[attr-defined]
    covered: set[int] = set()
    reps: list[Article] = []
    for a in sorted(scored, key=lambda x: (-(x.score + (4 if x.summary else 0)), -len(x.outlets),
                                           -(x.published or datetime.now(KST)).timestamp())):
        members = a.cluster or [a._idx]  # type: ignore[attr-defined]
        if any(m in covered for m in members):
            continue
        covered.update(members)
        reps.append(a)
    # 원래 정렬(점수순) 유지
    order = {id(a): i for i, a in enumerate(scored)}
    reps.sort(key=lambda x: order[id(x)])
    return reps


def select_balanced(scored: list[Article], limit: int) -> list[Article]:
    """점수 상위 기사를 기본으로 하되, 감정 카테고리가 비지 않도록 최소 몫을 보장."""
    quotas = {"사건사고": 40, "얼탱없음": 18, "감동": 14}
    chosen: list[Article] = scored[:limit]
    chosen_ids = {id(a) for a in chosen}
    extra: list[Article] = []
    for cat, q in quotas.items():
        have = sum(1 for a in chosen if a.category == cat)
        if have >= q:
            continue
        for a in scored:
            if have >= q:
                break
            if a.category == cat and id(a) not in chosen_ids:
                extra.append(a)
                chosen_ids.add(id(a))
                have += 1
    return chosen + extra


def apply_category(a: Article) -> None:
    cat, detail = classify(a.title, a.summary)
    a.category = cat
    a._detail = detail  # type: ignore[attr-defined]


def score_article(a: Article, now: datetime) -> None:
    detail = getattr(a, "_detail", None) or classify(a.title, a.summary)[1]
    minutes = max(0.0, (now - a.published).total_seconds() / 60.0) if a.published else 9999
    n_out = len(a.outlets) or 1
    sm = spread_metric(n_out, len(a.cluster))
    bm = breaking_metric(a.is_breaking, minutes)
    buzz, buzz_words = buzz_metric(a.title, a.summary, a.category)
    a.metrics = {"spread": sm, "breaking": bm, "buzz": buzz}
    a.score = viral_score(sm, bm, buzz, n_out)
    a.viral_point = make_viral_point(a.category, n_out, minutes, a.is_breaking,
                                     buzz_words, detail.get("hits", {}), a.title)
    a.tags = build_tags(a.category, n_out, a.is_breaking, minutes,
                        buzz_words, detail.get("hits", {}))
    a.reason = (
        f"확산도 {sm} (매체 {n_out}곳) · 속보성 {bm} (경과 {int(minutes)}분"
        f"{', 속보' if a.is_breaking else ''}) · 화제성 {buzz}"
    )


def enrichment_candidates(valid: list[Article], limit: int) -> list[Article]:
    """원문 페이지를 열어 요약·이미지를 보강할 후보(상위 점수 + 카테고리 대표)."""
    ranked = sorted(valid, key=lambda a: -a.score)
    pick: dict[int, Article] = {id(a): a for a in ranked[: max(60, limit * 2)]}
    for cat in ("감동", "얼탱없음", "사건사고"):
        for a in [x for x in ranked if x.category == cat][:30]:
            pick[id(a)] = a
    return [a for a in pick.values() if (not a.summary) or (not a.image) or "news.google.com" in a.link]

def build(hours: int = 48, limit: int = 140, verbose: bool = True) -> dict:
    now = datetime.now(KST)
    started = time.time()
    results: list[tuple[dict, list[Article]]] = []
    source_report: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_source, s): s for s in SOURCES}
        for fut in as_completed(futs):
            report, arts, err = fut.result()
            source_report.append(report)
            results.append((report, arts))
            if verbose:
                mark = "OK " if report["status"] == "ok" else "ERR"
                print(f"  [{mark}] {report['name']:<14} {report['count']:>3}건 {err[:60] if err else ''}")

    all_arts: list[Article] = []
    for _report, arts in results:
        all_arts.extend(arts)

    # 시각 파싱 실패/오래된 기사 제외
    window_start = now - timedelta(hours=hours)
    valid = [a for a in all_arts if a.published and window_start <= a.published <= now + timedelta(minutes=10)]
    if verbose:
        print(f"  수집 원본 {len(all_arts)}건 → 시간창({hours}h) 통과 {len(valid)}건")

    # 중복 제거 (언론사 내)
    valid = dedupe(valid)

    # 제목/링크 완전 중복 제거
    seen_links, seen_keys, uniq = set(), set(), []
    for a in valid:
        lk = a.link.split("?")[0]
        if lk in seen_links or a.key in seen_keys:
            continue
        seen_links.add(lk)
        seen_keys.add(a.key)
        uniq.append(a)
    valid = uniq

    # 1차 분류(원문 피드에 요약이 있으면 반영) → 확산도 → 1차 점수
    for a in valid:
        apply_category(a)
    compute_spreads(valid)
    for a in valid:
        score_article(a, now)

    # 요약·이미지가 비어 있는 상위 후보만 원문 페이지에서 보강
    cand = enrichment_candidates(valid, limit)
    if verbose:
        print(f"  원문 보강 대상 {len(cand)}건")
    enrich(cand)

    # 보강된 요약으로 재분류·재점수
    for a in valid:
        apply_category(a)
        score_article(a, now)

    scored = sorted(valid, key=lambda a: (-a.score, -(a.published or now).timestamp()))
    scored = collapse_stories(scored)
    top = select_balanced(scored, limit)

    # 최종 출력에 남은 기사는 링크·요약을 끝까지 보강
    rest = [a for a in top if "news.google.com" in a.link or not a.summary]
    if rest and verbose:
        print(f"  최종 보강 {len(rest)}건")
    enrich(rest)
    for a in rest:
        if a.summary:
            apply_category(a)
        score_article(a, now)
    top.sort(key=lambda a: (-a.score, -(a.published or now).timestamp()))
    for rank, a in enumerate(top, start=1):
        a.rank = rank  # type: ignore[attr-defined]

    # JSON 직렬화
    articles_json = []
    for a in top:
        articles_json.append({
            "rank": getattr(a, "rank", 0),
            "score": a.score,
            "category": a.category,
            "title": a.title,
            "summary": summarize_fallback(a),
            "viral_point": a.viral_point,
            "reason": a.reason,
            "tags": a.tags,
            "publisher": a.publisher,
            "outlets": a.outlets,
            "link": a.link,
            "published_at": a.published.astimezone(KST).isoformat() if a.published else None,
            "published_ts": int(a.published.timestamp() * 1000) if a.published else None,
            "is_breaking": a.is_breaking,
            "metrics": a.metrics,
            "image": a.image,
        })

    counts = {"전체": len(articles_json), "속보": sum(1 for a in articles_json if a["is_breaking"])}
    for cat in ["사건사고", "얼탱없음", "감동", "이슈"]:
        counts[cat] = sum(1 for a in articles_json if a["category"] == cat)
    counts["1시간이내"] = sum(1 for a in articles_json
                          if a["published_ts"] and (now.timestamp() * 1000 - a["published_ts"]) <= 3600_000)
    counts["오늘"] = sum(1 for a in articles_json
                       if a["published_ts"] and
                       datetime.fromtimestamp(a["published_ts"] / 1000, KST).date() == now.date())

    data = {
        "generated_at": now.isoformat(),
        "generated_at_label": f"{now.month}월 {now.day}일 ({'월화수목금토일'[now.weekday()]}) {now.hour:02d}:{now.minute:02d}" if False else f"{now.month}월 {now.day}일 ({'월화수목금토일'[now.weekday()]}) {now.hour:02d}:{now.minute:02d}",
        "window_hours": hours,
        "elapsed_sec": round(time.time() - started, 1),
        "counts": counts,
        "sources": sorted(source_report, key=lambda r: -r["count"]),
        "articles": articles_json,
    }
    return data


# ----------------------------------------------------------------------------
# HTML 빌드
# ----------------------------------------------------------------------------
def build_html(data: dict) -> None:
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"템플릿이 없습니다: {TEMPLATE_PATH}")
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if "/*__NEWS_DATA__*/" not in tpl:
        raise SystemExit("템플릿에 /*__NEWS_DATA__*/ 자리표시자가 없습니다.")
    out = tpl.replace("/*__NEWS_DATA__*/", payload)
    HTML_PATH.write_text(out, encoding="utf-8")
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def print_report(data: dict) -> None:
    c = data["counts"]
    print("\n=== 수집 결과 ===")
    print(f"생성 시각: {data['generated_at_label']} (KST)  소요 {data['elapsed_sec']}s")
    print(f"기사 {c['전체']}건 | 사건사고 {c['사건사고']} · 얼탱없음 {c['얼탱없음']} · 감동 {c['감동']} · 이슈 {c['이슈']}")
    print(f"속보 {c['속보']}건 · 1시간 이내 {c['1시간이내']}건 · 오늘 {c['오늘']}건")
    print("\n--- TOP 5 ---")
    for a in data["articles"][:5]:
        print(f"{a['rank']}. [{a['score']}] {a['category']} | {a['publisher']} | {a['title'][:52]}")
        print(f"     {a['viral_point']}")
    print(f"\n저장: {DATA_PATH}\n      {HTML_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description="바이럴 국내뉴스 수집/빌드")
    ap.add_argument("--hours", type=int, default=48, help="수집 시간창(시간)")
    ap.add_argument("--limit", type=int, default=140, help="최대 기사 수")
    ap.add_argument("--rebuild", action="store_true", help="data.json으로 index.html만 재빌드")
    args = ap.parse_args()

    if args.rebuild:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        build_html(data)
        print(f"재빌드 완료: {HTML_PATH}")
        return

    print(f"뉴스 수집 시작 ({datetime.now(KST):%Y-%m-%d %H:%M} KST)")
    data = build(hours=args.hours, limit=args.limit)
    if not data["articles"]:
        raise SystemExit("수집된 기사가 없습니다. 네트워크/피드를 확인하세요.")
    build_html(data)
    print_report(data)


if __name__ == "__main__":
    main()
