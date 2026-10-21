# -*- coding: utf-8 -*-
"""
법원경매정보(courtauction.go.kr) 부동산 매각공고 검사

- 매각공고 목록(이번 달·다음 달, 전국 법원) → 처음 보는 공고만 상세 조회
- 상세의 사건번호·용도·소재지·비고 등 전체 내용에서 키워드 검색
- 사이트가 과도한 호출 시 IP를 차단하므로 호출 간격 2~3초, 1회 실행당 상세 조회 상한을 둔다
"""
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://www.courtauction.go.kr"
NOTICE_PAGE = BASE + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ143M01.xml&pgjId=143M01"
LIST_API = BASE + "/pgj/pgj143/selectRletDspslPbanc.on"
DETAIL_API = BASE + "/pgj/pgj143/selectRletDspslPbancDtl.on"
KST = timezone(timedelta(hours=9))


class Blocked(Exception):
    pass


class CourtAuction:
    def __init__(self, log, max_details=60):
        self.log = log
        self.max_details = max_details
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Origin": BASE,
            "Referer": NOTICE_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        })
        self.last = 0

    def _wait(self):
        gap = 2.0 + random.random() * 1.5 - (time.time() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.time()

    def _post(self, url, body):
        self._wait()
        r = self.s.post(url, data=json.dumps(body), timeout=30,
                        headers={"Content-Type": "application/json; charset=UTF-8"})
        r.raise_for_status()
        j = r.json()
        data = j.get("data") if isinstance(j, dict) else None
        if isinstance(data, dict) and data.get("ipcheck") is False:
            raise Blocked("법원경매 사이트가 접속을 일시 차단했습니다(ipcheck)")
        if isinstance(j, dict) and isinstance(j.get("errors"), dict) and j["errors"].get("errorMessage"):
            raise RuntimeError(j["errors"]["errorMessage"])
        return j

    def warmup(self):
        self.s.get(NOTICE_PAGE, timeout=30)

    def list_notices(self, yyyymm):
        j = self._post(LIST_API, {"dma_srchDspslPbanc": {
            "srchYmd": yyyymm, "cortOfcCd": "", "bidDvsCd": "", "srchBtnYn": "Y"}})
        return (j.get("data") or {}).get("dlt_rletDspslPbancLst") or []

    def notice_detail(self, row):
        body = {"dma_srchGnrlPbanc": {
            "cortOfcCd": row.get("cortOfcCd", ""),
            "dspslDxdyYmd": row.get("dspslDxdyYmd", ""),
            "bidBgngYmd": row.get("bidBgngYmd", "") or "",
            "bidEndYmd": row.get("bidEndYmd", "") or "",
            "jdbnCd": row.get("jdbnCd", ""),
            "cortAuctnJdbnNm": row.get("cortAuctnJdbnNm", "") or "",
            "jdbnTelno": row.get("jdbnTelno", "") or "",
            "dspslPlcNm": row.get("dspslPlcNm", "") or "",
            "fstDspslHm": row.get("fstDspslHm", "") or "",
            "scndDspslHm": row.get("scndDspslHm", "") or "",
            "thrdDspslHm": row.get("thrdDspslHm", "") or "",
            "fothDspslHm": row.get("fothDspslHm", "") or "",
            "bidDvsCd": row.get("bidDvsCd", "") or row.get("intgCd", "") or "",
        }}
        j = self._post(DETAIL_API, body)
        data = j.get("data") or {}
        res = data.get("result") if isinstance(data.get("result"), dict) else {}
        pb = ((res.get("dspslPbanc") or {}).get("pbancInfo") or {}) if res else {}
        return pb.get("lst") or data.get("dlt_gnrlPbancLst") or []


def notice_key(row):
    return "|".join(str(row.get(k, "")) for k in ("cortOfcCd", "dspslDxdyYmd", "cortAuctnJdbnNm", "bidBgngYmd"))


def _num(v):
    try:
        return int(re.sub(r"[^0-9]", "", str(v)) or 0)
    except Exception:
        return 0


def bid_round(it):
    """
    몇 번째 입찰인지 추정 (1 = 최초 입찰)
    - 유찰횟수 항목이 있으면 그 값 + 1
    - 없으면 최저매각가격 < 감정평가액 이면 유찰된 물건(2회차 이상)으로 판단
      (법원경매 최초 입찰의 최저가는 감정가와 같고, 유찰될 때마다 20~30% 낮아짐)
    """
    for k in ("yuchalCnt", "flbdNcnt", "flbdCnt", "usbdNft"):
        if str(it.get(k, "")).strip() != "":
            return _num(it[k]) + 1
    aee, lws = _num(it.get("aeeEvlAmt")), _num(it.get("lwsDspslPrc"))
    if aee and lws:
        return 2 if lws < aee else 1
    return None   # 판단 불가 → 놓치지 않도록 알림 대상에 포함


def strip(s):
    return re.sub(r"<[^>]+>", " ", str(s or "")).strip()


def check(state, find_keywords, snippet, log, max_details=60, min_round=2):
    """반환: hits(list), checked(int), notes(list)"""
    st = state.setdefault("court_auction", {"seen": [], "alerted": []})
    seen, alerted = set(st["seen"]), set(st["alerted"])
    hits, notes, checked, skipped_first = [], [], 0, 0
    ca = CourtAuction(log, max_details)
    try:
        ca.warmup()
        today = datetime.now(KST).date()
        months = sorted({today.strftime("%Y%m"), (today.replace(day=1) + timedelta(days=32)).strftime("%Y%m")})
        rows = []
        for m in months:
            rows += ca.list_notices(m)
        # 이미 지난 매각기일 제외, 새 공고만
        rows = [r for r in rows if str(r.get("dspslDxdyYmd", "")) >= today.strftime("%Y%m%d")]
        rows = list({notice_key(r): r for r in rows}.values())   # 중복 제거
        new = [r for r in rows if notice_key(r) not in seen]
        new.sort(key=lambda r: str(r.get("dspslDxdyYmd", "")))
        log(f"[법원경매] 매각공고 {len(rows)}건 중 새 공고 {len(new)}건")
        if len(new) > max_details:
            notes.append(f"법원경매 새 공고 {len(new)}건 중 {max_details}건만 확인(나머지는 다음 실행)")
        for r in new[:max_details]:
            items = ca.notice_detail(r)
            checked += 1
            seen.add(notice_key(r))
            court = r.get("cortOfcNm") or r.get("cortSptNm") or ""
            sale = str(r.get("dspslDxdyYmd", ""))
            for it in items:
                text = " ".join(strip(v) for v in it.values() if isinstance(v, (str, int, float)))
                kw = find_keywords(text)
                cs = strip(it.get("csNo"))
                key = f"{court}|{cs}|{it.get('dspslSeq', '')}"
                rnd = bid_round(it)
                if kw and rnd == 1 and min_round > 1:
                    skipped_first += 1
                    log("  - 공제조합 있으나 최초 입찰이라 제외:", court, cs)
                    continue
                if kw and key not in alerted:
                    alerted.add(key)
                    aee, lws = _num(it.get("aeeEvlAmt")), _num(it.get("lwsDspslPrc"))
                    price = f"감정 {aee:,} → 최저 {lws:,}원 ({lws * 100 // aee}%)" if aee and lws else ""
                    hits.append({
                        "source": "법원경매",
                        "url": NOTICE_PAGE,
                        "button": "법원경매정보",
                        "lines": [
                            f"{court} {strip(r.get('cortAuctnJdbnNm'))}",
                            f"사건: {cs} / 매각기일 {sale[:4]}.{sale[4:6]}.{sale[6:8]}",
                            f"{strip(it.get('usgNm'))} {strip(it.get('st'))[:50]}",
                            (f"{rnd}회차 이상 입찰 / " if rnd else "") + price,
                            snippet(text, kw[0]),
                        ],
                    })
                    log("  ★ 법원경매 키워드 발견:", court, cs)
    except Blocked as e:
        notes.append(str(e))
        log(e)
    except Exception as e:
        notes.append(f"법원경매 확인 오류: {str(e)[:60]}")
        log("[법원경매] 오류:", e)
    if skipped_first:
        notes.append(f"법원경매 최초 입찰 {skipped_first}건 제외(2회차부터 알림)")
    today_s = datetime.now(KST).strftime("%Y%m%d")
    st["seen"] = [k for k in seen if k.split("|")[1] >= today_s]   # 매각기일 지난 공고는 기록에서 정리
    st["alerted"] = list(alerted)[-2000:]
    return hits, checked, notes
