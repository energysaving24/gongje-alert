# -*- coding: utf-8 -*-
"""
온비드(한국자산관리공사) 공매 물건 검사 - 공공데이터포털 '차세대 온비드' OpenAPI v2.0
(활용가이드 01·02, 배포일 2026/04/06 기준)

- 부동산 물건목록 : https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2
- 동산 물건목록   : https://apis.data.go.kr/B010003/OnbidMvastListSrvc2/getMvastCltrList2
  (출자증권·회원권 등 '권리/증권'은 동산에 속함)

검색 방식
  1) 물건명 검색  : onbidCltrNm=키워드 로 서버에서 바로 검색 (부동산·동산 모두, 호출 수 적음)
  2) 동산 전체확인: 동산 목록 전체를 받아 물건명·권리증서명·재산명 등 모든 항목에서 키워드 확인
                   (물건명엔 없고 '권리증서명' 등에만 공제조합이 적힌 경우 대비)
- 필수값: serviceKey, pageNo, numOfRows, resultType, prptDivCd(쉼표로 여러 개), pvctTrgtYn(Y/N)
- 이미 알린 물건(물건관리번호+공매조건번호)은 다시 알리지 않음
"""
import os
import re
import time

import requests

BASE = "https://apis.data.go.kr/B010003"
APIS = {
    "부동산": os.getenv("ONBID_REALTY_PATH") or "/OnbidRlstListSrvc2/getRlstCltrList2",
    "동산": os.getenv("ONBID_MOVABLE_PATH") or "/OnbidMvastListSrvc2/getMvastCltrList2",
}
# 동산은 목록 전체를 받아 모든 항목에서 키워드 확인 (부동산은 물건명 검색만)
FULL_SCAN = [s.strip() for s in os.getenv("ONBID_FULL_SCAN", "동산").split(",") if s.strip()]

# 재산유형코드 (prptDivCd) - 쉼표로 묶어 한 번에 요청
PRPT_CODES = {
    "0002": "공유재산", "0003": "금융권담보재산", "0004": "불용품", "0005": "기타일반재산",
    "0006": "유입재산", "0007": "압류재산", "0008": "수탁재산", "0010": "국유재산",
    "0011": "공공개발재산", "0013": "파산자산",
}
ALL_PRPT = ",".join(PRPT_CODES)
ONBID_HOME = "https://www.onbid.co.kr"
DETAIL_URL = ("https://www.onbid.co.kr/op/cltrpbancinf/cltrdtl/CltrDtlController/mvmnCltrDtl.do"
              "?cltrScrnGrpCd={grp}&cltrPrptDivCd={prpt}&onbidCltrno={cltr}&onbidPbancNo={pbanc}"
              "&pbctNo={pbct}&pbctCdtnNo={cdtn}")
SCREEN_GROUP = {"부동산": "0001", "동산": "0003"}   # 물건유형코드


def detail_url(label, x):
    """온비드 물건 상세화면 주소 (필요한 번호가 없으면 첫 화면)"""
    vals = dict(cltr=x.get("onbidCltrno"), pbanc=x.get("onbidPbancNo"), pbct=x.get("pbctNo"),
                cdtn=x.get("pbctCdtnNo"), prpt=x.get("prptDivCd"))
    if not all(vals.values()):
        return ONBID_HOME
    return DETAIL_URL.format(grp=SCREEN_GROUP.get(label, "0001"), **vals)
PAGE = int(os.getenv("ONBID_PAGE_SIZE", "500"))
MAX_PAGES = 60          # 안전장치 (500건 × 60페이지 = 3만건)


class OnbidError(Exception):
    pass


def _items(obj):
    """응답 JSON 어디에 있든 물건 목록(cltrMngNo 가진 dict들)을 찾아낸다"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and any("cltrMngNo" in x for x in obj):
            return obj
        for x in obj:
            r = _items(x)
            if r:
                return r
    elif isinstance(obj, dict):
        if "cltrMngNo" in obj:
            return [obj]
        for v in obj.values():
            r = _items(v)
            if r:
                return r
    return []


def _find(j, key):
    """중첩 dict 안에서 key 값 찾기 (header.resultCode, body.totalCount 등)"""
    if isinstance(j, dict):
        if key in j and not isinstance(j[key], (dict, list)):
            return j[key]
        for v in j.values():
            r = _find(v, key)
            if r is not None:
                return r
    return None


def _call(key, path, params, log):
    q = {"serviceKey": key, "resultType": "json", **params}
    for t in range(3):
        try:
            r = requests.get(BASE + path, params=q, timeout=90)
            break
        except requests.exceptions.RequestException as e:
            if t == 2:
                raise OnbidError(f"접속 실패: {str(e)[:60]}")
            log(f"   재시도({t+1}/3): {str(e)[:60]}")
            time.sleep(10)
    time.sleep(0.3)   # 초당 10건 제한
    text = r.text.strip()
    if text.startswith("<"):
        # 인증키 오류 등은 XML로 오는 경우가 있음
        m = re.search(r"<(?:returnReasonCode|resultCode)>(\d+)<", text)
        msg = re.search(r"<(?:returnAuthMsg|resultMsg)>([^<]+)<", text)
        raise OnbidError(f"오류 응답 코드 {m.group(1) if m else '?'} {msg.group(1) if msg else text[:100]}")
    try:
        j = r.json()
    except Exception:
        raise OnbidError(f"응답 해석 실패(HTTP {r.status_code}): {text[:120]}")
    code = _find(j, "resultCode")
    code = None if code is None else str(code).zfill(2)
    if code == "03":
        return [], 0
    if code not in (None, "00"):
        raise OnbidError(f"API 오류 코드 {code} {_find(j, 'resultMsg') or ''}")
    rows = _items(j)
    try:
        total = int(_find(j, "totalCount") or 0)
    except Exception:
        total = 0
    return rows, total


def fetch(key, path, log, extra=None):
    """수의계약여부 N/Y 각각 전체 페이지 수집"""
    out, calls = [], 0
    for pvct in ("N", "Y"):
        for page in range(1, MAX_PAGES + 1):
            params = {"pageNo": page, "numOfRows": PAGE, "prptDivCd": ALL_PRPT, "pvctTrgtYn": pvct}
            if extra:
                params.update(extra)
            rows, total = _call(key, path, params, log)
            calls += 1
            out += rows
            if len(rows) < PAGE or page * PAGE >= total:
                break
    return out, calls


def _bid_period(x):
    def fmt(s):
        s = str(s or "")
        return f"{s[:4]}.{s[4:6]}.{s[6:8]} {s[8:10]}:{s[10:12]}" if len(s) >= 12 else s
    b, e = x.get("cltrBidBgngDt"), x.get("cltrBidEndDt")
    return f"📅 {fmt(b)[2:]}\n     ~ {fmt(e)[2:]}" if (b or e) else ""


def _short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _man(v):
    try:
        v = int(float(re.sub(r"[^0-9.]", "", str(v)) or 0))
    except Exception:
        return str(v or "-")
    if v <= 0:
        return "비공개" if v == 0 else "-"
    eok, rest = divmod(v, 100_000_000)
    m = rest // 10_000
    return (f"{eok}억 " if eok else "") + (f"{m:,}만원" if m else ("원" if eok else f"{v:,}원"))


def _rate(x):
    r = x.get("apslPrcCtrsLowstBidRto")
    try:
        return f" ({float(r):.0f}%)" if r not in (None, "") else ""
    except Exception:
        return ""


def _won(v):
    try:
        return f"{int(float(str(v).replace(',', ''))):,}원"
    except Exception:
        return str(v or "")


def check(state, find_keywords, snippet, log, keywords=("공제조합",)):
    st = state.setdefault("onbid", {"alerted": []})
    alerted = set(st["alerted"])
    hits, notes, checked = [], [], 0
    key = os.getenv("DATA_GO_KR_KEY", "")
    if not key:
        notes.append("온비드: DATA_GO_KR_KEY 미설정으로 생략")
        return hits, checked, notes

    for label, path in APIS.items():
        found = {}
        try:
            # 1) 물건명 검색 (키워드별)
            for kw in keywords:
                rows, calls = fetch(key, path, log, {"onbidCltrNm": kw.strip()})
                log(f"[온비드 {label}] 물건명 '{kw.strip()}' 검색: {len(rows)}건 ({calls}회 호출)")
                for x in rows:
                    found[(x.get("cltrMngNo"), x.get("pbctCdtnNo"))] = x
            # 2) 전체 목록 확인
            if label in FULL_SCAN:
                rows, calls = fetch(key, path, log)
                log(f"[온비드 {label}] 전체 목록: {len(rows):,}건 ({calls}회 호출)")
                checked += len(rows)
                for x in rows:
                    found.setdefault((x.get("cltrMngNo"), x.get("pbctCdtnNo")), x)
        except OnbidError as e:
            notes.append(f"온비드 {label} 조회 오류: {str(e)[:60]}")
            log(f"[온비드 {label}] 오류:", e)
            if not found:
                continue
        if label not in FULL_SCAN:
            checked += len(found)

        for (mng, cdtn), x in found.items():
            text = " ".join(str(v) for v in x.values() if isinstance(v, (str, int, float)))
            kw = find_keywords(text)
            if not kw:
                continue
            k = f"{mng}|{cdtn}"
            if k in alerted:
                continue
            alerted.add(k)
            nm = str(x.get("onbidCltrNm") or "")
            usbd = x.get("usbdNft")
            hits.append({
                "source": f"온비드 {label}",
                "url": detail_url(label, x),
                "button": "물건 상세보기",
                "kws": kw,
                "lines": [
                    f"📌 {_short(nm, 40)}" if nm else "",
                    f"💰 감정 {_man(x.get('apslEvlAmt'))}",
                    f"     → 최저 {_man(x.get('lowstBidPrcIndctCont'))}" + _rate(x),
                    f"🔁 유찰 {usbd}회" if usbd not in (None, "", 0, "0") else "",
                    _bid_period(x),
                    f"🏷 {x.get('prptDivNm', '')} · {_short(x.get('orgNm', ''), 20)}",
                    f"🔢 물건번호 {x.get('onbidCltrno', '')}",
                    f"💬 {_short(snippet(text, kw[0]), 60)}" if kw[0] not in re.sub(r"\s", "", nm) else "",
                ],
            })
            log("  ★ 온비드 키워드 발견:", mng, nm)
    st["alerted"] = list(alerted)[-3000:]
    return hits, checked, notes
