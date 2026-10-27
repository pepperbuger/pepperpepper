# 집에서 이어가기 (3단계부터)

1~2단계는 클라우드 세션에서 끝냈다. 3단계부터는 키가 필요하므로 집 PC에서 Claude Code로 진행한다.

## Claude Code에 맡기기 전에 직접 할 것

1. 저장소 받기: `pepperbuger/pepperpepper`의 `claude/tender-ramanujan-hvzxq8` 브랜치(PR #1). PR이 병합됐다면 `main`.
2. Ondo 키 만들기: 샌드박스용 키를 먼저 만든다. 권한은 거래와 조회만, 출금 권한 없음. 집 PC 공인 IP를 화이트리스트에 등록한다.
3. Lighter 키 만들기: 테스트넷 계정에 API 키를 등록한다(인덱스 4~254). L1 지갑 개인키는 등록할 때 한 번만 쓰고 봇 PC에 두지 않는다.
4. `.env.example`을 `.env`로, `config.example.yaml`을 `config.yaml`로 복사하고 키를 채운다.
5. Claude Code가 대신 확인할 수 없는 것:
   - Ondo 유동성 풀 배분 방식: 25bps 조건이 맞는지, 매수와 매도를 양쪽 다 걸어야 하는지, 마켓별 배분 (앱 공지, 디스코드)
   - 한국 거주자의 리워드와 포인트 수령 자격
   - REST 요청 제한(초당 1회) 상향 요청: support@ondoperps.xyz

## Claude Code에 붙여넣을 요청

```
ondo-lighter-bot 폴더에서 작업한다. 먼저 CLAUDE.md, OVERVIEW.md, docs/SPEC.md를 읽어라.

현재 상태:
- SPEC 8번 구현 순서의 1단계(설정 검증)와 2단계(시세 수신)는 완료됐다.
- 설치: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
- 확인: .venv/bin/python -m pytest 가 전부 통과하고,
  .venv/bin/python -m olbot check-config --online 이 "검증 통과"를 출력해야 한다.
  이 둘이 안 되면 3단계 전에 그것부터 고친다.

할 일: SPEC 8번의 3단계부터 한 단계씩 진행한다.
- 각 단계의 완료 기준을 통과하면 멈추고 결과를 보고한다. 내가 확인하기 전에는 다음 단계로 넘어가지 않는다.
- 엔드포인트, 파라미터, 채널 이름은 추측하지 말고 docs.ondoperps.xyz/llms.txt와 apidocs.lighter.xyz/llms.txt에서 확인한다.
- 개발과 테스트는 Ondo 샌드박스와 Lighter 테스트넷에서만 한다.
- 메인넷 주문, 출금, 이체, 계정 등급 변경, API 키 등록은 실행 전에 반드시 나에게 묻는다.
- 키는 .env에서만 읽고, 로그와 커밋에 절대 남기지 않는다.
- 새 WS 클라이언트는 src/olbot/wsbase.py의 ReconnectingWS를 상속하고,
  메시지 파싱은 순수 함수로 분리해서 실제로 받은 메시지로 테스트한다.
- 금액과 가격 계산은 Decimal로 한다.
- 단계가 끝날 때마다 SPEC 2번 구조표와 8번 진행 표시, README 진행 상황을 갱신하고 커밋한다.
```

## 단계별 완료 기준

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| 3 | Ondo 인증: 요청 서명, WS 로그인, 잔고와 포지션 조회, 데드맨 스위치 | 샌드박스에서 잔고 조회 성공, 서명 문자열 단위 테스트 |
| 4 | 가짜 주문 모드(dry_run) 호가기 | 24시간 돌려서 분당 요청 50회 이하 |
| 5 | Lighter 헷지 | 테스트넷에서 주문, 체결 확인, 실패 재시도 테스트 |
| 6 | 안전장치 | 킬 스위치 조건 전부 테스트 통과, 프로세스 강제 종료 시 30초 안에 Ondo 호가 사라짐 확인 |
| 7 | 기록과 리포트 | 하루치 데이터로 리포트 출력 |
| 8 | 소액 실제 운영 | 메인넷 주문 전 사람 확인, 한쪽 500달러부터 |

## 중간에 확인시킬 것

- 4단계 전에 `watch`를 하루 이상 돌려 마켓별 dev_p95를 본다. 1시간 결과는 SOL 1.7, XRP 2.1, ENA 3.2, NEAR 11.3, LIT 14.2bps였다. 하루 결과로 `basis_deviation_bps`(기본 15)와 `basis_window_sec`(기본 3600)을 정한다.
- 1주차 마켓을 정할 때 리워드 몫(호가 두께)과 헷지 안정성(dev_p95)을 같이 본다. LIT, NEAR는 몫이 크지만 괴리가 흔들리고, SOL, XRP, ENA는 괴리가 안정적이다.
- 8단계 직전에 `scripts/ondo_depth_probe.py`를 다시 돌려 마켓 상태를 확인한다.
