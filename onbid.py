# -*- coding: utf-8 -*-
"""
온비드(한국자산관리공사) 공매 물건 검사 - 공공데이터포털 '차세대 온비드' OpenAPI 사용

- 부동산 물건목록 : data.go.kr 15157207  (/OnbidRlstListSrvc2/getRlstCltrList2)
- 동산 물건목록   : data.go.kr 15157232  (요청주소는 활용신청 후 확인 → ONBID_MOVABLE_PATH 로 지정)
- 현재 입찰 진행 중인 전체 물건을 받아 물건명·용도·기관명 등 모든 항목에서 키워드 검색
- 이미 알린 물건(물건관리번호+공매조건번호)은 다시 알리지 않음
"""
import os
import re
import time

import requests

BASE = "https://apis.data.go.kr/B010003"
APIS = {
    "부동산": os.getenv("ONBID_REALTY_PATH") or "/OnbidRlstListSrvc2/getRlstCltrList2",
    "동산": os.getenv("ONBID_MOVABLE_PATH") or "/OnbidMvprpListSrvc2/getMvprpCltrList2",
}
# 재산유형코드 (prptDivCd)
PRPT_CODES = {
    "0002": "공유재산", "0003": "금융권담보재산", "0004": "불용품", "0005": "기타일반재산",
    "0006": "유입재산", "0007": "압류재산", "0008": "수탁재산", "0010": "국유재산",
    "0011": "공공개발재산", "0013": "파산자산",
}
ONBID_HOME = "https://www.onbid.co.kr"
PAGE = 1000


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


def _result_code(j):
    for path in (("response", "header", "resultCode"), ("header", "resultCode"), ("result", "resultCode")):
        cur = j
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is not None:
            return str(cur)
    return None


def _total(j):
    for path in (("response", "body", "totalCount"), ("body", "totalCount")):
        cur = j
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is not None:
            try:
                return int(cur)
            except Exception:
                pass
    return None


def fetch_all(key, path, log):
    """한 API의 (재산유형 × 수의계약여부) 전체 페이지 수집"""
    out, calls = [], 0
    for prpt in PRPT_CODES:
        for pvct in ("N", "Y"):
            page = 1
            while True:
                params = {"serviceKey": key, "pageNo": page, "numOfRows": PAGE, "resultType": "json",
                          "prptDivCd": prpt, "pvctTrgtYn": pvct}
                r = requests.get(BASE + path, params=params, timeout=60)
                calls += 1
                time.sleep(0.5)
                try:
                    j = r.json()
                except Exception:
                    raise RuntimeError(f"응답 해석 실패(HTTP {r.status_code}): {r.text[:120]}")
                code = _result_code(j)
                if code == "03":           # 해당 조건 물건 없음
                    break
                if code not in (None, "00"):
                    raise RuntimeError(f"API 오류 코드 {code}: {str(j)[:120]}")
                rows = _items(j)
                for x in rows:
                    x["_prpt"] = PRPT_CODES[prpt]
                out += rows
                total = _total(j)
                if len(rows) < PAGE or (total is not None and page * PAGE >= total):
                    break
                page += 1
    log(f"   {calls}회 호출, {len(out)}건")
    return out


def name_of(x):
    for k in ("cltrNm", "onbidCltrNm", "cltrName", "pbctCltrNm"):
        if x.get(k):
            return str(x[k])
    return ""


def _bid_period(x):
    b = next((str(x[k]) for k in x if re.search(r"(bid|pbct).*(Bgng|Begn|Strt)", k, re.I) and x[k]), "")
    e = next((str(x[k]) for k in x if re.search(r"(bid|pbct).*(End|Cls)", k, re.I) and x[k]), "")
    return f"입찰: {b} ~ {e}" if (b or e) else ""


def check(state, find_keywords, snippet, log):
    st = state.setdefault("onbid", {"alerted": []})
    alerted = set(st["alerted"])
    hits, notes, checked = [], [], 0
    key = os.getenv("DATA_GO_KR_KEY", "")
    if not key:
        notes.append("온비드: DATA_GO_KR_KEY 미설정으로 생략")
        return hits, checked, notes

    for label, path in APIS.items():
        if not path:
            continue
        log(f"[온비드 {label}] 물건목록 조회")
        try:
            rows = fetch_all(key, path, log)
        except Exception as e:
            notes.append(f"온비드 {label} 조회 오류: {str(e)[:70]}")
            log(f"[온비드 {label}] 오류:", e)
            continue
        checked += len(rows)
        for x in rows:
            text = " ".join(str(v) for v in x.values() if isinstance(v, (str, int, float)))
            kw = find_keywords(text)
            if not kw:
                continue
            k = f"{x.get('cltrMngNo')}|{x.get('pbctCdtnNo', '')}"
            if k in alerted:
                continue
            alerted.add(k)
            nm = name_of(x)
            hits.append({
                "source": f"온비드 {label}",
                "url": ONBID_HOME,
                "button": "온비드 열기",
                "lines": [
                    f"[{x.get('_prpt', '')}] {x.get('orgNm', '') or x.get('pbancOrgNm', '')}".strip(),
                    f"물건: {nm[:70]}" if nm else "",
                    f"물건관리번호: {x.get('cltrMngNo')}",
                    _bid_period(x),
                    snippet(text, kw[0]) if not (nm and kw[0] in re.sub(r'\s', '', nm)) else "",
                ],
            })
            log("  ★ 온비드 키워드 발견:", x.get("cltrMngNo"), nm)
    st["alerted"] = list(alerted)[-3000:]
    return hits, checked, notes
