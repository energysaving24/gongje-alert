# -*- coding: utf-8 -*-
"""
카카오 '나에게 보내기' 최초 1회 토큰 발급 도우미 (내 PC에서 실행)

실행:  python kakao_setup.py
→ 출력되는 주소를 브라우저에 붙여넣고, 알림 받을 카카오 계정(010-8954-3531)으로 로그인·동의
→ 이동된 주소창 전체(https://localhost/?code=...)를 복사해 붙여넣기
→ 화면에 나온 REFRESH TOKEN 을 GitHub Secret 'KAKAO_REFRESH_TOKEN' 에 저장
"""
import json
import urllib.parse
import requests

REDIRECT_URI = "https://localhost"

rest_key = input("카카오 REST API 키: ").strip()
secret = input("Client Secret (사용 안 하면 그냥 Enter): ").strip()

auth_url = ("https://kauth.kakao.com/oauth/authorize?"
            + urllib.parse.urlencode({"client_id": rest_key, "redirect_uri": REDIRECT_URI,
                                      "response_type": "code", "scope": "talk_message"}))
print("\n1) 아래 주소를 브라우저에 붙여넣고 로그인·동의하세요:\n")
print(auth_url)
print("\n2) '사이트에 연결할 수 없음' 화면이 떠도 정상입니다. 주소창 전체를 복사하세요.")
redirected = input("\n주소창 내용 붙여넣기: ").strip()
code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query).get("code", [redirected])[0]

data = {"grant_type": "authorization_code", "client_id": rest_key,
        "redirect_uri": REDIRECT_URI, "code": code}
if secret:
    data["client_secret"] = secret
r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=30)
j = r.json()
if "refresh_token" not in j:
    print("\n❌ 실패:", json.dumps(j, ensure_ascii=False))
    print("→ 코드는 1회용이라 다시 실행해서 새로 로그인하세요.")
    input("\n창을 닫으려면 Enter...")
    raise SystemExit(1)

# 바로 테스트 메시지
tpl = {"object_type": "text", "text": "[설정 완료] 공제조합 알림(회생파산·법원경매·온비드)이 이 카톡으로 옵니다.",
       "link": {"web_url": "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeList.work",
                "mobile_web_url": "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeList.work"},
       "button_title": "게시판 보기"}
t = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                  headers={"Authorization": f"Bearer {j['access_token']}"},
                  data={"template_object": json.dumps(tpl, ensure_ascii=False)}, timeout=30)
print("\n테스트 카톡:", "전송 성공 (나와의 채팅 확인)" if t.ok else f"실패 {t.text}")

from pathlib import Path
out = Path(__file__).with_name("kakao_refresh_token.txt")
out.write_text(j["refresh_token"], encoding="utf-8")

print("\n✅ 아래 값을 GitHub Secret 'KAKAO_REFRESH_TOKEN' 에 저장하세요:\n")
print(j["refresh_token"])
print(f"\n(같은 값이 파일에도 저장됨: {out})")
print("※ GitHub에 넣은 뒤에는 이 txt 파일을 삭제하세요.")
input("\n창을 닫으려면 Enter...")
