# Ondo + Lighter 봇 작업 규칙 (Claude Code용)

이 폴더는 Ondo Perps에 maker 호가를 걸고 체결분을 Lighter에서 헷지하는 봇이다. 설계는 `docs/SPEC.md`에 있고, 구현은 SPEC의 "8. 구현 순서"와 완료 기준을 따른다. 사용자는 단계마다 멈추지 말고 준비·수정·주문 없는 검증을 이어서 진행하도록 요청했다(2026-10-29). 실제 주문 등 사전 확인이 필요한 행동에서만 구체적인 범위를 정해 확인받는다.

## 근거 자료

- Ondo Perps: https://docs.ondoperps.xyz/llms.txt (각 페이지 URL에 `.md`를 붙이면 마크다운)
- Lighter: https://apidocs.lighter.xyz/llms.txt, 서명 구현 https://github.com/elliottech/lighter-go, Python SDK https://github.com/elliottech/lighter-python
- 엔드포인트 이름, 파라미터, enum 값, 채널 이름은 추측하지 말고 문서에서 확인한 뒤 쓴다. SPEC의 표와 문서가 다르면 문서를 따르고 SPEC을 고친다.

## 안전 규칙

- 메인넷 주문, 출금, 이체, 계정 등급 변경, API 키 등록은 실행 전에 반드시 사람에게 확인받는다.
- 사용자는 샌드박스 없이 진행하기로 했다. 메인넷 조회와 dry_run은 진행하되, 실제 주문은 거래소·마켓·금액·정리 절차를 정해 별도 승인을 받는다. 먼 가격의 post-only도 체결 가능성이 있으므로 무위험이라고 표현하지 않는다. 메인넷 키를 테스트넷에 재사용하지 않는다.
- 비밀값은 `.env`에서만 읽는다. 코드, 로그, 예외 메시지, 커밋에 키가 들어가면 안 된다.
- Lighter L1 지갑 개인키는 봇 코드가 읽지 않는다. 봇은 API 키(인덱스 4~254)만 쓴다.
- 기본 실행 모드는 `dry_run`이다. live는 설정 `mode: live`와 명령행 `--live`가 둘 다 있어야 한다.
- SPEC "5. 위험 관리"의 시작 절차, 데드맨 스위치, 킬 스위치는 live 전에 전부 테스트로 검증한다.

## Ondo 구현 주의

- REST 서명 문자열은 `timestamp + METHOD + path(쿼리 포함) + body`. 서명에 쓴 body 문자열과 실제 전송 body가 바이트 단위로 같아야 한다.
- 공개 베타 기본 REST 제한은 초당 1회다. 모든 REST 호출은 하나의 토큰 버킷을 거친다. 실시간 상태는 WS로 받고 REST는 주문과 가끔의 대사에만 쓴다.
- 호가 주문은 항상 `postOnly: true`. 별도 승인받은 비상 정리 주문만 `reduceOnly: true`인 IOC를 허용한다. 재호가는 일괄 취소 + 일괄 생성(최대 20개)으로 묶는다.
- 예외적인 실제 체결 시험은 `docs/PAIRED_TRIAL.md`의 범위(거래소별 진입 계획 최대 15 USD·합계 최대 4건)를 사람이 별도로 승인했을 때만 허용한다. 이때 Ondo 진입 1건은 체결 확인 목적의 non-post-only IOC다. 정규 봇의 maker 호가 규칙은 그대로다.
- 내 주문끼리 교차하면 기존 주문이 취소된다(자기 체결 방지). 매수 호가가 매도 호가 이상이 되는 상황을 만들지 않는다.
- 로그인 직후 `cancelAllOrdersAfterPerps` 데드맨 스위치를 켜고 주기적으로 갱신한다.

## Lighter 구현 주의 (Lighter 공식 AGENTS.md 요약)

- 가격과 수량은 정수(값 x 10^decimals). decimals는 `GET /api/v1/orderBookDetails`에서 받는다. 마켓 ID와 decimals를 하드코딩하지 않는다.
- `min_base_amount`, `min_quote_amount`를 지킨다.
- 논스는 API 키별로 1씩 증가한다. 키 하나당 전송을 직렬화한다. 결과가 불확실하면 다시 서명하기 전에 tx 상태와 nextNonce부터 확인한다(중복 주문 방지).
- 미확정 헷지를 시간 경과로 미체결 처리하지 않는다. 10초는 호가 일시정지 기준이다. 전송 전에 client_order_index와 함께 SQLite에 저장하고 재시작 시 복원한다. 해제는 `accountOrders`의 최종 상태(filled 또는 SDK enum에 있는 canceled-* 목록. 접두어 판정 금지)와 포지션 정확 일치가 둘 다 확인될 때만 한다. 응답 필드가 빠졌거나 이상하면 미확정을 유지한다. DB 삭제/교체로 우회하지 않는다.
- IOC는 `orderExpiry=0`, taker 주문의 `price`는 감수할 최악 가격이다.
- `code: 200`은 접수일 뿐이다. 체결은 WS나 `GET /api/v1/tx`로 확인한다.
- `clientOrderIndex`는 전 마켓에서 고유, 2^48-1 이하.
- WS는 2분 안에 한 번 이상 ping, 끊기면 자동 재연결과 재구독. 호가창 델타는 `begin_nonce`가 이전 nonce와 같은지 확인하고 아니면 재구독한다.
- Standard 계정은 분당 60 요청이다. 429/405를 받으면 물러난다.

## 개발 명령

- 설치: `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- 테스트: `.venv/bin/python -m pytest`
- 설정 검증: `.venv/bin/python -m olbot check-config --online`
- 시세 확인(주문 없음): `.venv/bin/python -m olbot watch --duration 600`
- 실행: `.venv/bin/python -m olbot run` (dry_run), `run --live` (실운영), `report --hours 24`
- 1~7단계 코드는 완료됐다. 실제 키로 처음 돌릴 때 생기는 문제를 고치는 게 다음 일이다. 새 WS 클라이언트는 `wsbase.ReconnectingWS`를 상속하고, 메시지 파싱은 `market.py`처럼 순수 함수로 분리해서 실제로 받은 메시지로 테스트한다.

## 코드 규칙

- Python 3.11+, asyncio. 의존성은 최소로: `lighter-sdk`, `websockets`, `httpx`, `pyyaml`, `python-dotenv`, 테스트는 `pytest`, `pytest-asyncio`.
- 금액과 가격 계산은 `Decimal`을 쓴다. float로 주문 가격을 만들지 않는다.
- 모든 거래소 호출은 타임아웃과 재시도 정책을 명시한다.
- 새 모듈을 추가하면 SPEC "2. 전체 구조"도 함께 고친다.
- 커밋 전에 `pytest`를 통과시킨다.
