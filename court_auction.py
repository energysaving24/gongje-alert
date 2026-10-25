# -*- coding: utf-8 -*-
"""
법원경매정보(courtauction.go.kr) 물건 검사  [2026-10-25 개편]

예전 방식(매각공고 목록 → 공고 상세)은 전국 공고가 몇 건만 조회되는 문제가 있어
사이트의 '물건상세검색'(PGJ151) 기능으로 바꿈.

- 조건: 전국 · 부동산 · 매각기일 오늘~N일 후(기본 14일) · 유찰 1회 이상(=2회차 입찰부터)
- 결과 목록의 비고(mulBigo)·물건내역·소재지 등에서 키워드 검색
- 이미 알린 물건(docid)은 다시 알리지 않음
- 사이트가 과도한 호출 시 IP를 차단하므로 호출 간격 2.5~4초, 실행당 최대 페이지 수 제한
"""
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://www.courtauction.go.kr"
SEARCH_PAGE = BASE + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml&pgjId=151F00"
SEARCH_API = BASE + "/pgj/pgjsearch/searchControllerMain.on"
CASE_PAGE = BASE + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ159M00.xml&pgjId=159M00"   # 경매사건검색
KST = timezone(timedelta(hours=9))
PAGE_SIZE = 100          # 사이트가 허용하는 값: 10/20/50/100


class Blocked(Exception):
    pass


SIDO = {"11": "서울", "26": "부산", "27": "대구", "28": "인천", "29": "광주", "30": "대전", "31": "울산",
        "36": "세종", "41": "경기", "42": "강원", "51": "강원특별", "43": "충북", "44": "충남",
        "45": "전북", "52": "전북특별", "46": "전남", "47": "경북", "48": "경남", "50": "제주"}


class BadRequest(Exception):
    pass


def _body(page, ymd_from, ymd_to, min_flbd, sido="", bid=""):
    return {
        "dma_pageInfo": {"pageNo": page, "pageSize": PAGE_SIZE, "bfPageNo": "", "startRowNo": "",
                         "totalCnt": "", "totalYn": "Y", "groupTotalCount": ""},
        "dma_srchGdsDtlSrchInfo": {
            "rletDspslSpcCondCd": "", "bidDvsCd": bid, "mvprpRletDvsCd": "00031R",
            "cortAuctnSrchCondCd": "0004601",
            "rprsAdongSdCd": sido, "rprsAdongSggCd": "", "rprsAdongEmdCd": "",
            "rdnmSdCd": "", "rdnmSggCd": "", "rdnmNo": "",
            "mvprpDspslPlcAdongSdCd": "", "mvprpDspslPlcAdongSggCd": "", "mvprpDspslPlcAdongEmdCd": "",
            "rdDspslPlcAdongSdCd": "", "rdDspslPlcAdongSggCd": "", "rdDspslPlcAdongEmdCd": "",
            "cortOfcCd": "", "jdbnCd": "", "execrOfcDvsCd": "",
            "lclDspslGdsLstUsgCd": "", "mclDspslGdsLstUsgCd": "", "sclDspslGdsLstUsgCd": "",
            "cortAuctnMbrsId": "", "aeeEvlAmtMin": "", "aeeEvlAmtMax": "",
            "lwsDspslPrcRateMin": "", "lwsDspslPrcRateMax": "",
            "flbdNcntMin": str(min_flbd) if min_flbd else "", "flbdNcntMax": "",
            "objctArDtsMin": "", "objctArDtsMax": "",
            "mvprpArtclKndCd": "", "mvprpArtclNm": "", "mvprpAtchmPlcTypCd": "",
            "notifyLoc": "off", "lafjOrderBy": "", "pgmId": "PGJ151F01", "csNo": "",
            "cortStDvs": "2" if sido else "1", "statNum": 1,
            "bidBgngYmd": ymd_from, "bidEndYmd": ymd_to, "dspslDxdyYmd": "",
            "fstDspslHm": "", "scndDspslHm": "", "thrdDspslHm": "", "fothDspslHm": "",
            "dspslPlcNm": "", "lwsDspslPrcMin": "", "lwsDspslPrcMax": "",
            "grbxTypCd": "", "gdsVendNm": "", "fuelKndCd": "",
            "carMdyrMax": "", "carMdyrMin": "", "carMdlNm": "", "sideDvsCd": "",
        },
    }


class CourtAuction:
    def __init__(self, log):
        self.log = log
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Origin": BASE,
            "Referer": SEARCH_PAGE,
            "X-Requested-With": "XMLHttpRequest",
            "submissionid": "mf_wfm_mainFrame_sbm_selectGdsDtlSrch",
            "sc-userid": "SYSTEM",
        })
        self.last = 0
        self.page = None          # 브라우저 모드일 때 사용
        self._pw = self._br = None

    def warmup(self):
        self.s.get(SEARCH_PAGE, timeout=30)

    def use_browser(self):
        """requests 요청이 거절될 때: 실제 브라우저로 검색화면을 열고, 그 화면 안에서 검색 요청"""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._br = self._pw.chromium.launch()
        ctx = self._br.new_context(locale="ko-KR", user_agent=self.s.headers["User-Agent"])
        self.page = ctx.new_page()
        self.page.goto(SEARCH_PAGE, wait_until="networkidle", timeout=90000)
        time.sleep(3)

    def close(self):
        try:
            if self._br:
                self._br.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _post_browser(self, body):
        res = self.page.evaluate("""async ([url, body]) => {
            const r = await fetch(url, {method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json; charset=UTF-8',
                          'Accept': 'application/json, text/javascript, */*; q=0.01',
                          'submissionid': 'mf_wfm_mainFrame_sbm_selectGdsDtlSrch',
                          'sc-userid': 'SYSTEM', 'X-Requested-With': 'XMLHttpRequest'},
                body: JSON.stringify(body)});
            return {status: r.status, text: await r.text()};
        }""", [SEARCH_API, body])
        return res["status"], res["text"]

    def search(self, page, ymd_from, ymd_to, min_flbd, sido="", bid=""):
        gap = 2.5 + random.random() * 1.5 - (time.time() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.time()
        body = _body(page, ymd_from, ymd_to, min_flbd, sido, bid)
        if self.page:
            status, text = self._post_browser(body)
        else:
            r = self.s.post(SEARCH_API, data=json.dumps(body), timeout=40,
                            headers={"Content-Type": "application/json; charset=UTF-8"})
            status, text = r.status_code, r.text
        if status == 400:
            raise BadRequest(re.sub(r'.*"errorMessage":"([^"]{0,40}).*', r"\1", text[:300]))
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        j = json.loads(text)
        data = j.get("data") if isinstance(j, dict) else None
        if isinstance(data, dict) and data.get("ipcheck") is False:
            raise Blocked("법원경매 사이트가 접속을 일시 차단했습니다(ipcheck)")
        if isinstance(j, dict) and isinstance(j.get("errors"), dict) and j["errors"].get("errorMessage"):
            raise RuntimeError(j["errors"]["errorMessage"])
        data = data or {}
        rows = data.get("dlt_srchResult") or []
        info = data.get("dma_pageInfo") or {}
        try:
            total = int(str(info.get("totalCnt") or info.get("groupTotalCount") or 0))
        except Exception:
            total = 0
        return rows, total


def _num(v):
    try:
        return int(re.sub(r"[^0-9]", "", str(v)) or 0)
    except Exception:
        return 0


def _man(v):
    v = int(v or 0)
    eok, rest = divmod(v, 100_000_000)
    m = rest // 10_000
    return (f"{eok}억 " if eok else "") + (f"{m:,}만원" if m else ("원" if eok else f"{v:,}원"))


def strip(s):
    return re.sub(r"<[^>]+>", " ", str(s or "")).strip()


def _addr(r):
    a = " ".join(strip(r.get(k)) for k in ("hjguSido", "hjguSigu", "hjguDong", "daepyoLotno") if r.get(k))
    return a or strip(r.get("printSt")) or strip(r.get("realSt"))


def check(state, find_keywords, snippet, log, max_details=60, min_round=2, days=14, max_pages=50):
    """반환: hits, checked, notes  (max_details 는 예전 설정과의 호환용, 사용 안 함)"""
    st = state.setdefault("court_auction", {})
    st.pop("seen", None)                                     # 예전 방식 기록 정리
    alerted = set(st.get("alerted", []))
    hits, notes, checked = [], [], 0
    today = datetime.now(KST).date()
    ymd_from = today.strftime("%Y%m%d")
    ymd_to = (today + timedelta(days=days)).strftime("%Y%m%d")
    min_flbd = max(0, min_round - 1)                         # 2회차부터 = 유찰 1회 이상

    ca = CourtAuction(log)

    def handle(rows):
        nonlocal checked
        checked += len(rows)
        for r in rows:
            if min_flbd and _num(r.get("yuchalCnt")) < min_flbd:
                continue
            text = " ".join(strip(r.get(k)) for k in
                            ("mulBigo", "pjbBuldList", "buldList", "buldNm", "jimokList", "dspslUsgNm")
                            if r.get(k))
            kw = find_keywords(text)
            if not kw:
                continue
            key = r.get("docid") or f"{r.get('boCd')}|{r.get('saNo')}|{r.get('maemulSer')}"
            if key in alerted:
                continue
            alerted.add(key)
            aee, lws = _num(r.get("gamevalAmt")), _num(r.get("minmaePrice"))
            sale = str(r.get("maeGiil") or "")
            cs = strip(r.get("srnSaNo")) or strip(r.get("saNo"))
            hits.append({
                "source": "법원경매",
                "kws": kw,
                "title": f"{cs} {_addr(r)[:20]}",
                "rate": (lws * 100 // aee) if aee and lws else None,
                "short": f"{cs} · {_man(lws).replace('만원', '만')} 유찰{_num(r.get('yuchalCnt'))}",
                "period": f"{sale[4:6]}/{sale[6:8]}", "place": _addr(r)[:20],
                "price": f"{_man(lws)}" + (f" ({lws * 100 // aee}%)" if aee and lws else ""),
                "url": CASE_PAGE,
                "button": "경매사건검색",
                "lines": [
                    f"🔢 사건번호 {cs}",
                    "     (사건검색에 입력)",
                    f"⚖ {strip(r.get('jiwonNm'))} {strip(r.get('jpDeptNm'))}".rstrip(),
                    f"📅 매각기일 {sale[2:4]}.{sale[4:6]}.{sale[6:8]}" if len(sale) == 8 else "",
                    f"🏠 {_addr(r)[:34]}",
                    f"💰 감정 {_man(aee)}" if aee else "",
                    f"     → 최저 {_man(lws)} ({lws * 100 // aee}%)" if aee and lws else "",
                    f"🔁 유찰 {_num(r.get('yuchalCnt'))}회",
                    f"💬 {snippet(text, kw[0])[:60]}",
                ],
            })
            log("  ★ 법원경매 키워드 발견:", cs, kw)

    def run(label, sido="", bid=""):
        """한 조건으로 끝 페이지까지 조회. 반환: 전체 건수"""
        page, total = 1, None
        while page <= max_pages:
            rows, t = ca.search(page, ymd_from, ymd_to, min_flbd, sido, bid)
            if total is None:
                total = t
            if not rows:
                break
            handle(rows)
            if total and page * PAGE_SIZE >= total:
                break
            page += 1
        if total and total > max_pages * PAGE_SIZE:
            notes.append(f"법원경매 {label} {total:,}건 중 일부만 확인")
        return total or 0

    try:
        ca.warmup()
        log(f"[법원경매] 매각기일 {ymd_from}~{ymd_to}, 유찰 {min_flbd}회 이상 검색")

        def by_region():
            for code, name in SIDO.items():
                t = run(name, code)
                log(f"   {name}: {t:,}건")

        done = False
        for mode in ("직접요청", "브라우저"):
            if mode == "브라우저":
                log("[법원경매] 직접 요청이 거절되어 브라우저로 다시 시도")
                ca.use_browser()
            try:
                total = run("전국")
                log(f"[법원경매] ({mode}) 전국 {total:,}건")
                done = True
                break
            except BadRequest as e:
                log(f"[법원경매] ({mode}) 전국 검색 거절: {e}")
            try:
                by_region()
                done = True
                break
            except BadRequest as e:
                log(f"[법원경매] ({mode}) 시도별 검색도 거절: {e}")
        if not done:
            notes.append("법원경매 사이트가 자동 검색을 거절함 (해외 서버 접속 제한 추정)")
    except Blocked as e:
        notes.append(str(e))
        log(e)
    except Exception as e:
        notes.append(f"법원경매 확인 오류: {str(e)[:60]}")
        log("[법원경매] 오류:", e)
    finally:
        ca.close()
    st["alerted"] = list(alerted)[-3000:]
    return hits, checked, notes
