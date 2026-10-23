# -*- coding: utf-8 -*-
"""
공제조합 키워드 감시 → 카카오톡(나에게 보내기) 알림
  1) 대법원 회생·파산 자산매각 공고 (main.py)
  2) 법원경매정보 부동산 매각공고 (court_auction.py)
  3) 온비드 공매 물건 (onbid.py)

- 게시판: https://www.scourt.go.kr/portal/notice/realestate/RealNoticeList.work
- 새 공고(이전 실행 이후 올라온 글)만 검사
- 검색 범위: 제목 · 매각기관 · 상세 본문 · 첨부파일(PDF/HWP/HWPX/DOCX/XLSX/ZIP) 내용
- 결과: 있으면 공고별 링크 전송, 없으면 '해당 없음' 결과 통지
"""
import io
import json
import os
import re
import sys
import time
import zipfile
import zlib
import struct
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ───────────────────────── 설정 ─────────────────────────
KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "공제조합").split(",") if k.strip()]
BASE = "https://www.scourt.go.kr"
LIST_URL = BASE + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL = BASE + "/portal/notice/realestate/RealNoticeView.work?pageIndex=1&seq_id={seq}"
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))          # 한 번에 최대 몇 페이지(페이지당 10건)까지 거슬러 볼지
INITIAL_DAYS = int(os.getenv("INITIAL_DAYS", "1"))      # 최초 실행 시 며칠치 공고를 볼지
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"              # 1이면 카톡 안 보내고 화면 출력만

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


def log(*a):
    print(datetime.now(KST).strftime("[%H:%M:%S]"), *a, flush=True)


def get_html(url, **kw):
    for i in range(3):
        try:
            r = session.get(url, timeout=30, **kw)
            r.raise_for_status()
            r.encoding = "cp949"   # 사이트가 euc-kr
            return r.text
        except Exception as e:
            log(f"요청 실패({i+1}/3): {url} - {e}")
            time.sleep(15 * (i + 1))   # 사이트가 연결을 끊으면 잠시 쉬었다가 재시도
    raise RuntimeError(f"페이지를 가져오지 못했습니다: {url}")


def norm(s):
    """공백/줄바꿈 제거 - '공 제 조 합'처럼 띄어 쓴 경우도 잡기 위함"""
    return re.sub(r"\s+", "", s or "")


def find_keywords(text):
    t = norm(text)
    return [k for k in KEYWORDS if norm(k) in t]


def snippet(text, kw, width=40):
    """키워드 주변 문장 일부 (알림 메시지용)"""
    flat = re.sub(r"\s+", " ", text or "")
    idx = flat.find(kw)
    if idx < 0:
        # 띄어쓰기 섞인 경우
        pat = r"\s*".join(map(re.escape, kw))
        m = re.search(pat, flat)
        if not m:
            return ""
        idx = m.start()
    s = flat[max(0, idx - width): idx + len(kw) + width].strip()
    return "…" + s + "…"


# ───────────────────────── 상태 저장 ─────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"max_seq": 0, "history": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = state.get("history", [])[-300:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────────────────── 게시판 파싱 ─────────────────────────
def parse_list(html):
    """목록 페이지 → [{seq, court, org, title}] (최신순)"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"RealNoticeView\.work.*?seq_id=(\d+)", a["href"])
        if not m:
            continue
        tr = a.find_parent("tr")
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")] if tr else []
        items.append({
            "seq": int(m.group(1)),
            "title": a.get_text(" ", strip=True),
            "court": tds[1] if len(tds) > 1 else "",
            "org": tds[2] if len(tds) > 2 else "",
        })
    # 중복 제거
    seen, out = set(), []
    for it in items:
        if it["seq"] not in seen:
            seen.add(it["seq"])
            out.append(it)
    return out


def parse_view(html, page_url):
    """상세 페이지 → 항목 dict + 본문 텍스트 + 첨부파일 후보 링크"""
    soup = BeautifulSoup(html, "lxml")
    info = {}
    attach_links = []
    for th in soup.find_all("th"):
        key = norm(th.get_text())
        td = th.find_next_sibling("td")
        if not td:
            continue
        info[key] = td.get_text(" ", strip=True)
        if key == "첨부파일":
            for a in td.find_all("a"):
                attach_links.append({
                    "name": a.get_text(" ", strip=True),
                    "href": a.get("href", ""),
                    "onclick": a.get("onclick", ""),
                })
    # 본문: 공고 표 영역 전체 텍스트 (메뉴 제외 위해 content 영역 우선)
    content = soup.find(id="content") or soup.find(class_=re.compile("board|view|content")) or soup
    body_text = content.get_text("\n", strip=True)
    return info, body_text, attach_links


def written_date(info):
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", info.get("작성일", ""))
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST).date()


# ───────────────────────── 첨부파일 다운로드 ─────────────────────────
def download_attachments(page_url, attach_links, failed=None):
    """
    첨부파일 링크는 자바스크립트 함수로 되어 있는 경우가 많아
    실제 브라우저(Playwright)로 클릭해서 다운로드를 받는다.
    반환: [(파일명, bytes)]
    """
    files = []
    if failed is None:
        failed = []
    if not attach_links:
        return files

    # 1) 일반 URL 링크면 requests로 바로 받기
    remaining = []
    for link in attach_links:
        href = link["href"]
        if href and not href.startswith("javascript") and href != "#":
            try:
                r = session.get(urljoin(page_url, href), timeout=60, headers={"Referer": page_url})
                if r.ok and r.content and b"<html" not in r.content[:500].lower():
                    files.append((link["name"], r.content))
                    continue
            except Exception as e:
                log("직접 다운로드 실패:", e)
        remaining.append(link)

    if not remaining:
        return files

    # 2) 자바스크립트 링크 → 브라우저로 클릭
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright 미설치 - 첨부파일 검색 생략")
        return files

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(accept_downloads=True, user_agent=HEADERS["User-Agent"], locale="ko-KR")
            # 다운로드 이벤트 대신 파일 응답(PDF 등)이 바로 오는 경우도 잡기
            captured = []

            def on_response(resp):
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    cd = (resp.headers.get("content-disposition") or "").lower()
                    if "attachment" in cd or ("text/html" not in ct and ct and
                                              any(x in ct for x in ("pdf", "octet", "hwp", "zip", "msword", "officedocument"))):
                        captured.append(resp)
                except Exception:
                    pass
            ctx.on("response", on_response)

            page = ctx.new_page()
            for i in range(3):
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                    break
                except Exception as e:
                    log(f"  브라우저 접속 실패({i+1}/3): {str(e)[:80]}")
                    if i == 2:
                        raise
                    time.sleep(15 * (i + 1))
            anchors = page.locator("xpath=//th[contains(normalize-space(.),'첨부')]/following-sibling::td[1]//a")
            n = anchors.count()
            for i in range(n):
                a = anchors.nth(i)
                name = (a.inner_text() or f"첨부{i+1}").strip()
                if any(name == f[0] for f in files):
                    continue
                captured.clear()
                try:
                    with page.expect_download(timeout=90000) as dl_info:
                        a.click()
                    dl = dl_info.value
                    data = Path(dl.path()).read_bytes()
                    files.append((name or dl.suggested_filename, data))
                    log(f"  첨부 다운로드: {name} ({len(data):,} bytes)")
                except Exception as e:
                    got = None
                    for resp in captured:
                        try:
                            body = resp.body()
                            if body and b"<html" not in body[:500].lower():
                                got = body
                                break
                        except Exception:
                            pass
                    if got:
                        files.append((name, got))
                        log(f"  첨부 다운로드(응답 캡처): {name} ({len(got):,} bytes)")
                    else:
                        log(f"  첨부 다운로드 실패: {name} - {str(e)[:80]}")
                        failed.append(name)
                    # 열린 팝업 창 정리
                    for pg in ctx.pages[1:]:
                        try:
                            pg.close()
                        except Exception:
                            pass
                time.sleep(2)
            browser.close()
    except Exception as e:
        log(f"  첨부파일 확인 오류: {str(e)[:120]}")
        failed.extend(l["name"] for l in remaining if not any(l["name"] == f[0] for f in files))
    return files


# ───────────────────────── 첨부파일 텍스트 추출 ─────────────────────────
def text_from_pdf(data):
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pg in pdf.pages:
                out.append(pg.extract_text() or "")
        return "\n".join(out)
    except Exception as e:
        log("   PDF 추출 오류:", e)
        return ""


def text_from_hwp(data):
    """HWP 5.0 (OLE) 본문 텍스트 추출"""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    header = ole.openstream("FileHeader").read()
    flags = struct.unpack_from("<I", header, 36)[0]
    compressed = bool(flags & 1)
    if flags & 4:   # 배포용 문서(암호화)
        log("   배포용 HWP - 본문 추출 불가, 미리보기 텍스트만 확인")
        if ole.exists("PrvText"):
            return ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
        return ""
    sections = sorted(
        [e for e in ole.listdir() if e[0] == "BodyText"],
        key=lambda e: int(re.sub(r"\D", "", e[1]) or 0),
    )
    texts = []
    for sec in sections:
        raw = ole.openstream("/".join(sec)).read()
        if compressed:
            raw = zlib.decompress(raw, -15)
        i = 0
        while i + 4 <= len(raw):
            h = struct.unpack_from("<I", raw, i)[0]
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = struct.unpack_from("<I", raw, i)[0]
                i += 4
            if tag == 67:  # HWPTAG_PARA_TEXT
                chunk = raw[i:i + size]
                buf, j = [], 0
                while j + 1 < len(chunk):
                    c = struct.unpack_from("<H", chunk, j)[0]
                    if c in (1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23):
                        j += 16      # 확장 컨트롤(8 wchar)
                        continue
                    if c in (4, 5, 6, 7, 8, 9, 19, 20):
                        j += 16      # 인라인 컨트롤
                        buf.append(" ")
                        continue
                    if c in (10, 13):
                        buf.append("\n")
                    elif c >= 32:
                        buf.append(chr(c))
                    j += 2
                texts.append("".join(buf))
            i += size
    return "\n".join(texts)


def text_from_xml_zip(data, prefixes):
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.endswith(".xml") and any(n.startswith(p) for p in prefixes):
                xml = z.read(n).decode("utf-8", "ignore")
                out.append(re.sub(r"<[^>]+>", " ", xml))
    return "\n".join(out)


def extract_text(name, data, depth=0):
    """파일 종류 판별 후 텍스트 추출. 반환 (text, 메모)"""
    low = name.lower()
    head = data[:8]
    try:
        if head.startswith(b"%PDF") or low.endswith(".pdf"):
            t = text_from_pdf(data)
            return t, ("" if norm(t) else "스캔본(텍스트 없음)")
        if head.startswith(b"\xd0\xcf\x11\xe0") or low.endswith(".hwp"):
            return text_from_hwp(data), ""
        if head.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
            if any(n.startswith("Contents/") for n in names) or low.endswith(".hwpx"):
                return text_from_xml_zip(data, ["Contents/", "Preview/"]), ""
            if any(n.startswith("word/") for n in names):
                return text_from_xml_zip(data, ["word/"]), ""
            if any(n.startswith("xl/") for n in names):
                return text_from_xml_zip(data, ["xl/sharedStrings", "xl/worksheets/"]), ""
            if depth < 2:   # 일반 압축파일 → 안의 파일들 검사
                parts = []
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        fname = info.filename
                        try:
                            fname = fname.encode("cp437").decode("cp949")
                        except Exception:
                            pass
                        t, _ = extract_text(fname, z.read(info), depth + 1)
                        parts.append(fname + "\n" + t)
                return "\n".join(parts), ""
        if low.endswith((".txt", ".csv")):
            return data.decode("cp949", "ignore"), ""
    except Exception as e:
        log(f"   텍스트 추출 실패 {name}: {e}")
        return "", "읽기 실패"
    return "", "지원 안 되는 형식"


# ───────────────────────── 카카오톡 (나에게 보내기) ─────────────────────────
class Kakao:
    TOKEN_URL = "https://kauth.kakao.com/oauth/token"
    SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

    def __init__(self):
        self.rest_key = os.getenv("KAKAO_REST_API_KEY", "")
        self.refresh_token = os.getenv("KAKAO_REFRESH_TOKEN", "")
        if not DRY_RUN and not (self.rest_key and self.refresh_token):
            raise RuntimeError("KAKAO_REST_API_KEY / KAKAO_REFRESH_TOKEN Secret 이 설정되지 않았습니다")
        self.client_secret = os.getenv("KAKAO_CLIENT_SECRET", "")
        self.access_token = None

    def refresh(self):
        data = {"grant_type": "refresh_token", "client_id": self.rest_key,
                "refresh_token": self.refresh_token}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        r = requests.post(self.TOKEN_URL, data=data, timeout=30)
        if not r.ok:
            raise RuntimeError(f"카카오 토큰 갱신 실패: {r.status_code} {r.text}")
        j = r.json()
        self.access_token = j["access_token"]
        if j.get("refresh_token"):         # 만료 1개월 전부터 새 리프레시 토큰이 내려옴
            log("새 카카오 리프레시 토큰 발급됨 → GitHub Secret 자동 갱신 시도")
            update_github_secret("KAKAO_REFRESH_TOKEN", j["refresh_token"])

    def send(self, text, url=None, button="공고 보기"):
        if DRY_RUN:
            print("\n──── [DRY RUN 카톡] ────\n" + text + (f"\n▶ {url}" if url else "") + "\n")
            return
        if not self.access_token:
            self.refresh()
        link = {"web_url": url or LIST_URL, "mobile_web_url": url or LIST_URL}
        tpl = {"object_type": "text", "text": text[:200], "link": link, "button_title": button}
        r = requests.post(self.SEND_URL,
                          headers={"Authorization": f"Bearer {self.access_token}"},
                          data={"template_object": json.dumps(tpl, ensure_ascii=False)},
                          timeout=30)
        if not r.ok or r.json().get("result_code") != 0:
            raise RuntimeError(f"카톡 전송 실패: {r.status_code} {r.text}")
        log("카톡 전송 완료")


def update_github_secret(name, value):
    """GH_PAT 이 있으면 저장소 Secret 을 새 값으로 교체 (리프레시 토큰 자동 연장)"""
    pat, repo = os.getenv("GH_PAT"), os.getenv("GITHUB_REPOSITORY")
    if not (pat and repo):
        log("⚠ GH_PAT 미설정 - 새 리프레시 토큰을 수동으로 Secret 에 넣어야 합니다:")
        log(value)
        return
    from base64 import b64encode
    from nacl import encoding, public
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    k = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=h, timeout=30).json()
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    enc = b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}", headers=h,
                     json={"encrypted_value": enc, "key_id": k["key_id"]}, timeout=30)
    log("Secret 갱신", "성공" if r.status_code in (201, 204) else f"실패 {r.status_code} {r.text}")


# ───────────────────────── 메인 ─────────────────────────
def collect_new_posts(state):
    """이전에 확인한 마지막 글 이후의 새 공고 목록"""
    max_seq = state.get("max_seq", 0)
    new = []
    for page in range(1, MAX_PAGES + 1):
        items = parse_list(get_html(LIST_URL, params={"pageIndex": page}))
        if not items:
            break
        fresh = [it for it in items if it["seq"] > max_seq]
        new.extend(fresh)
        if len(fresh) < len(items):     # 이미 본 글에 도달
            break
        if max_seq == 0 and page >= 3:  # 최초 실행은 날짜로 거르므로 3페이지면 충분
            break
        time.sleep(1)
    return new


def check_scourt(state):
    """대법원 회생·파산 자산매각 공고 → (hits, checked, notes)"""
    today = datetime.now(KST)
    first_run = state.get("max_seq", 0) == 0
    posts = collect_new_posts(state)
    log(f"[회생파산] 새 공고 {len(posts)}건")
    hits, checked, unreadable, fail_urls, page_fail = [], 0, [], [], []
    cutoff = (today - timedelta(days=INITIAL_DAYS)).date()

    for it in sorted(posts, key=lambda x: x["seq"]):
        url = VIEW_URL.format(seq=it["seq"])
        try:
            info, body, links = parse_view(get_html(url), url)
        except Exception as e:
            log("상세 페이지 오류:", url, e)
            page_fail.append(it["seq"])
            continue
        wdate = written_date(info)
        if first_run and wdate and wdate < cutoff:
            continue
        checked += 1
        title = info.get("제목", it["title"])
        org = info.get("매각기관", it["org"])
        court = info.get("관할법원", it["court"])
        log(f"[{it['seq']}] {court} | {org} | {title}")

        where, snip = [], ""
        kw = find_keywords(" ".join([title, org, body]))
        if kw:
            where.append("공고 본문")
            snip = snippet(" ".join([title, org, body]), kw[0])

        failed = []
        for fname, data in download_attachments(url, links, failed):
            text, memo = extract_text(fname, data)
            if memo:
                unreadable.append(f"{title[:15]}…/{fname}({memo})")
            k2 = find_keywords(text)
            if k2:
                where.append(f"첨부:{fname}")
                snip = snip or snippet(text, k2[0])

        for fname in failed:
            unreadable.append(f"{title[:15]}…/{fname}(다운로드 실패)")
            fail_urls.append(url)

        if where:
            hits.append({"source": "회생파산 자산매각", "url": url, "button": "공고 보기", "lines": [
                f"{court} / {info.get('작성일', '')}", org, f"제목: {title}",
                f"위치: {', '.join(where)}", snip]})
            log("  ★ 키워드 발견:", where)
        time.sleep(3)

    if posts:
        done = [p["seq"] for p in posts if p["seq"] not in page_fail]
        if page_fail:   # 못 연 공고는 다음 실행에서 다시 보도록 그 앞까지만 기록
            new_max = min(page_fail) - 1
        else:
            new_max = max(done)
        state["max_seq"] = max(state.get("max_seq", 0), new_max)
    notes = []
    if unreadable:
        notes.append(f"회생파산 첨부 {len(unreadable)}건 확인 불가(스캔본·다운로드 실패) - 직접 확인 필요")
        state["scourt_unchecked"] = (state.get("scourt_unchecked", []) + sorted(set(fail_urls)))[-50:]
    return hits, checked, notes


SOURCES = [s.strip() for s in os.getenv("SOURCES", "scourt,court_auction,onbid").split(",") if s.strip()]


def main():
    today = datetime.now(KST)
    log(f"검사 시작 - 키워드: {KEYWORDS} / 대상: {SOURCES}")
    state = load_state()
    kakao = Kakao()
    all_hits, summary, notes = [], [], []

    jobs = []
    if "scourt" in SOURCES:
        jobs.append(("회생파산 자산매각", "공고", lambda: check_scourt(state)))
    if "court_auction" in SOURCES:
        import court_auction
        jobs.append(("법원경매", "공고",
                     lambda: court_auction.check(state, find_keywords, snippet, log,
                                                 int(os.getenv("AUCTION_MAX_DETAILS", "60")),
                                                 int(os.getenv("AUCTION_MIN_ROUND", "2")))))
    if "onbid" in SOURCES:
        import onbid
        jobs.append(("온비드 공매", "물건", lambda: onbid.check(state, find_keywords, snippet, log)))

    for label, unit, fn in jobs:
        try:
            h, c, n = fn()
        except Exception as e:
            traceback.print_exc()
            h, c, n = [], 0, [f"{label} 확인 실패: {str(e)[:60]}"]
        all_hits += h
        notes += n
        summary.append(f"· {label}: {unit} {c:,}건 확인 / 발견 {len(h)}건")

    # ── 알림 전송 ──
    kw_label = "·".join(KEYWORDS)
    for h in all_hits:
        body = "\n".join(x for x in h["lines"] if x)
        kakao.send(f"[{h['source']} {kw_label} 발견]\n{body}", h["url"], h.get("button", "보기"))
        time.sleep(1)

    head = f"[{today.strftime('%m/%d')} {kw_label} 점검 결과]"
    head += f"\n총 {len(all_hits)}건 발견(위 메시지 참고)" if all_hits else f"\n'{kw_label}' 해당 없음"
    msg = head + "\n" + "\n".join(summary)
    if notes:
        msg += "\n※ " + "\n※ ".join(notes)
    # 카톡 텍스트는 200자 제한 → 길면 나눠서 전송
    while msg:
        kakao.send(msg[:200], LIST_URL, "게시판 보기")
        msg = msg[200:]

    state.setdefault("history", []).append({
        "run": today.strftime("%Y-%m-%d %H:%M"), "summary": summary,
        "hits": [h["lines"][:3] for h in all_hits], "notes": notes,
    })
    save_state(state)
    log("완료")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test-kakao":
        Kakao().send("[테스트] 공제조합 알림(회생파산·법원경매·온비드) 연결 성공", LIST_URL, "게시판 보기")
        sys.exit(0)
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        try:
            Kakao().send(f"[공제조합 점검 오류]\n{str(e)[:150]}", LIST_URL, "게시판 보기")
        except Exception:
            pass
        sys.exit(1)
