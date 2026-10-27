# Ondo + Lighter 유동성 리워드 봇 설계서

작성: 2026-10-27. 아래 사실 관계는 이 날짜에 Ondo Perps와 Lighter 공식 문서, 그리고 공개 API 응답으로 확인했다. 집에서 구현을 시작하기 전에 "11. 구현 전에 다시 확인할 것"부터 다시 본다.

## 0. 목표와 범위

목표: Ondo Perps에 post-only 지정가 호가를 걸어 USDC 유동성 리워드, 거래량 리워드, Ondo 포인트를 받는다. 호가가 체결되면 Lighter(Standard 계정, 수수료 0)에서 반대 포지션을 잡아 가격 위험을 지운다.

1주차 목표는 수익이 아니라 측정이다. 마켓별로 "호가를 1,000달러 x 1시간 걸었을 때 받는 리워드"와 "체결 1건당 헷지 비용"을 숫자로 뽑는 것이 완료 기준이다.

하지 않는 것:
- 방향성 베팅. 헷지 안 된 포지션은 사고로 취급한다.
- 속도 경쟁. Lighter Standard는 taker 300ms 지연이 있다. 체결을 많이 받는 전략이 아니라 호가를 오래 걸어두는 전략이다.
- Ondo 전용 마켓(BRENT, COPPER, NFLX, SMSN, SKHYNIX 등). Lighter에 없으면 헷지가 안 된다.

## 1. 확인된 사실

### 1-1. Ondo Perps

| 항목 | 값 | 출처 |
|---|---|---|
| REST | `https://api.ondoperps.xyz` (샌드박스 `https://api.ondoperps-sandbox.xyz`) | Builder Integration Guide |
| WebSocket | `wss://api.ondoperps.xyz/ws` | WS spec |
| 마켓 이름 | `{BASE}-USD.P` (예: `BTC-USD.P`) | `/v1/markets` |
| REST 인증 | 헤더 `ONDO-KEY-ID`, `ONDO-TIMESTAMP`(ms), `ONDO-SIGN` = hex(HMAC-SHA256(secret, timestamp + METHOD + path_with_query + body)). 시각 허용 오차: 10초 늦음, 1초 빠름 | API Key Authentication |
| WS 로그인 | `{"op":"login","args":{"key":id,"time":ms,"sign":hex(HMAC-SHA256(secret, time + "ondo_perps_ws_login"))}}` | WS spec |
| REST 속도 제한 | 공개 베타 기간 기본 초당 1회. 상향은 support@ondoperps.xyz에 계정 ID와 용도를 보내 요청 | Public Beta |
| WS 제한 | 초당 25 요청(버스트 50), 메시지 최대 32KB, 180초 무응답이면 끊김. `{"op":"ping"}` 주기 전송 | WS spec |
| 주문 | `POST /v1/perps/orders`. 필드 `market, side(buy/sell), type(limit/market), price, size, timeInForce, postOnly, reduceOnly, clientOrderId` | Create Order |
| 일괄 주문 | `POST /v1/perps/orders/batch`, 최대 20개, 일부만 성공할 수 있음 | Create Batched Orders |
| 취소 | `DELETE /v1/perps/orders`(전체), 일괄 취소, ID별 취소 | Orders API |
| post-only | 체결될 주문이면 400 `post_only_has_match` | Create Order |
| 자기 체결 방지 | 내 주문끼리는 체결되지 않고, 교차하는 기존 내 주문이 취소됨 | Order Types |
| 데드맨 스위치 | WS 채널 `cancelAllOrdersAfterPerps`, `timeout_seconds` 안에 하트비트가 없으면 모든 주문 취소 | WS spec |
| WS 채널 | 공개 `topOfBooksPerps`, 깊이, 체결, 펀딩, 마크가. 비공개 `fillsPerps`, 주문, 포지션, 잔고, 펀딩 지급 | WS spec |
| 수수료 | 프로모션 maker 0.01%, taker 0.025% (기본 0.02%, 0.05%). 14일 거래량 등급 있음 | Fees |
| 펀딩 | 1시간 정산, 기본 이자 0.00125%/시간(연 약 10.95%), 상한 시간당 1%. 주식은 0.5배 감쇠 | Funding Rates |
| 청산 수수료 | 청산 명목가의 1.5% | Fees |
| 포지션 상한 | 베타 기간 마켓당 계정당 100만 달러 | Public Beta |
| 주말 | 주식 등은 외부 가격이 끊기는 주말에 내부 가격 발견으로 전환. 마크가 변동은 업데이트당 기본 50bps로 제한 | Weekend Trading |

리워드 (앱 화면, 2026-10-27 기준):
- USDC 주간 풀 22만: 유동성 풀 10만(시장가 근처 지정가를 호가창에 유지), 거래량 풀 10만, 액티비티 풀 2만. 매주 수요일 지난주 성과로 지급.
- 유동성 풀 조건이 "중간가 25bps 이내"라는 건 2차 기사 기준이다. 산정 공식(양방향 필요 여부, 거리 가중, 시간 가중, 마켓별 배분)은 확인하지 못했다.
- Ondo 포인트: 주 500만, 거래 활동 기준.

### 1-2. Lighter

| 항목 | 값 | 출처 |
|---|---|---|
| REST / WS | `https://mainnet.zklighter.elliot.ai`, `wss://mainnet.zklighter.elliot.ai/stream` (테스트넷 `testnet.zklighter.elliot.ai`) | Agent instructions |
| SDK | Python `pip install lighter-sdk`, `import lighter`, 주문은 `lighter.SignerClient` | SDK 문서, github.com/elliottech/lighter-python |
| Standard 계정 | maker 0%, taker 0%, taker와 취소 300ms 지연, 분당 60 요청 | Account Types, Rate Limits |
| 가격과 수량 | 정수(값 x 10^decimals). decimals는 `GET /api/v1/orderBookDetails` | Agent instructions |
| 최소 주문 | 마켓별 `min_base_amount`, `min_quote_amount` | orderBooks |
| 논스 | API 키별로 1씩 증가. 결과가 불확실하면 tx 상태와 nextNonce부터 확인(중복 주문 방지) | Agent instructions |
| IOC | `orderExpiry=0`, taker 주문의 `price`는 감수할 최악 가격 | Agent instructions |
| clientOrderIndex | 전 마켓에서 고유, 2^48-1 이하 | Agent instructions |
| API 키 인덱스 | 4~254 사용(0~3 예약). 키 등록에는 L1 지갑 서명 필요 | Agent instructions |
| WS | 2분 안에 최소 한 번 ping. 호가창은 스냅샷 후 델타, `begin_nonce` 연속성 확인 | Agent instructions |
| `200` 응답 | 접수일 뿐 체결이 아님. WS나 `GET /api/v1/tx`로 확인 | Agent instructions |

### 1-3. 실측 데이터 (2026-10-27 08:55 UTC, `scripts/ondo_depth_probe.py`)

- Ondo 활성 퍼프 73개 중 63개가 Lighter에도 같은 심볼로 있다. 주식, 지수, 금, 은, 원유(WTI), 외환까지 겹친다.
- Ondo 전용(헷지 불가): BRENT, COPPER, DKNG, GLW, NFLX, NKE, PURR, SKHYNIX, SMSN, URNM.
- 중간가 +-25bps 안 호가 물량(양쪽 합):

| 구분 | 예시 |
|---|---|
| 두꺼움, 내 몫 작음 | BTC 약 190만 달러, US500 약 360만, XAU 약 320만, GOOGL 약 480만 |
| 중간 | SOL 59만, HYPE 54만, ZEC 78만, NVDA 67만, TSLA 120만 |
| 얇음, 헷지 가능 | LIT 2.7만, NEAR 2.7만, MSTR 4만, MRNA 6만, CBRS 11만, HOOD 15만, COIN 19만, META 20만, ONDO 22만, XRP 27만, ENA 34만 |

- 같은 심볼이라도 같은 기초자산이 아닐 수 있다. OPENAI는 Lighter 최근가가 Ondo 중간가보다 약 477bps, ZHIPU는 약 -112bps 차이가 났다. 가격 괴리 검사를 통과하지 못하는 마켓은 호가를 걸지 않는다. 주식은 미국 장 마감 시간이라 최근가가 낡았을 수 있으니, 비교는 양쪽 실시간 중간가로 한다.
- 정상 마켓에도 평소 괴리가 꾸준히 있다. 2026-10-27 10:40 UTC 실시간 중간가 기준으로 Lighter가 LIT 약 32bps, XRP 약 17bps, SOL 약 16bps, ENA 약 6bps, NEAR 약 2bps 비쌌고, 1분 동안 거의 변하지 않았다. 헷지 손익을 흔드는 건 괴리의 크기가 아니라 진입과 청산 사이의 변화이므로, 괴리 검사는 "최근 중앙값에서 벗어난 정도"로 한다(3번 1항).
- 1시간 기록(10:43~11:43 UTC) 결과, 괴리가 중앙값에서 벗어난 폭의 95% 수준(dev_p95)은 SOL 1.7, XRP 2.1, ENA 3.2bps로 안정적이었고 NEAR 11.3, LIT 14.2bps로 컸다. 호가가 얇아 리워드 몫이 큰 마켓일수록 괴리가 흔들려 헷지 비용이 크다. 자세한 표는 README "2단계 검증 결과".

## 2. 전체 구조

Python 3.11+, asyncio 단일 프로세스.

```
ondo-lighter-bot/
  CLAUDE.md               Claude Code 작업 규칙
  pyproject.toml          패키지와 의존성 (pip install -e '.[dev]')
  config.example.yaml     설정 예시 (config.yaml로 복사)
  .env.example            비밀값 예시 (.env로 복사, 커밋 금지)
  docs/SPEC.md            이 문서
  scripts/
    ondo_depth_probe.py   마켓 선별용 읽기 전용 측정기 (완성)
  src/olbot/
    main.py               실행 진입점: check-config, watch 명령 (완성)
    config.py             설정과 비밀값 로딩, 검증 (완성)
    wsbase.py             재연결, ping, 연결 이벤트를 처리하는 WS 공통 클래스 (완성)
    markets.py            기초자산 -> Ondo 마켓 이름, Lighter market_id 연결 (완성)
    market.py             양쪽 최우선 호가, 괴리(bps), 괴리 중앙값 추적과 판정 (완성)
    watch.py              2단계: 시세 수신과 괴리 기록 실행기 (완성)
    ondo/ws.py            공개 채널 topOfBooksPerps (완성)
    ondo/private_ws.py    로그인, fillsPerps, positionsPerps, 데드맨 스위치 (완성)
    ondo/rest.py          HMAC 서명, 토큰 버킷, 잔고/포지션/레버리지/전체 취소/일괄 주문 (완성)
    ratelimit.py          Ondo REST 공용 토큰 버킷 (완성)
    lighter/ws.py         공개 채널 ticker/{market_id} (완성)
    lighter/client.py     SDK SignerClient 래퍼: IOC 시장가 헷지, 계정 포지션 조회 (완성)
    quoter.py             호가 가격과 수량 계산, 재호가 판단 (완성)
    hedger.py             포지션 차이 기준 헷지, 실패 시 슬리피지 넓혀 재시도 (완성)
    risk.py               킬 스위치와 일시정지 판단 (완성)
    engine.py             시작 절차, 호가/헷지/위험/기록 루프, 종료 절차 (완성)
    ledger.py             SQLite: basis_samples, events, fills, hedges, quote_snapshots, equity, rewards (완성)
    report.py             운영 리포트, 주간 리워드 수동 입력 (완성)
  tests/                  pytest
```

Lighter 시세는 호가창 전체(스냅샷+델타) 대신 최우선 호가만 주는 `ticker/{market_id}` 채널을 쓴다. 공정가와 괴리 계산에는 최우선 호가로 충분하고, 델타 연속성 관리가 필요 없다. 공개 시세는 `?readonly=true`로 접속한다.

데이터 흐름:
1. `market.py`가 Ondo `topOfBooksPerps`와 Lighter 호가창을 받아 마켓별 공정가와 괴리를 갱신한다.
2. `quoter.py`가 목표 호가를 계산하고, 현재 걸린 호가와 차이가 기준 이상일 때만 `ondo/rest.py`에 일괄 취소와 일괄 생성을 요청한다.
3. `ondo/ws.py`의 `fillsPerps` 이벤트가 오면 `hedger.py`가 Lighter에 IOC 주문을 보낸다.
4. `risk.py`는 모든 상태를 보고 위반 시 킬 스위치를 실행한다.
5. 모든 사건은 `ledger.py`에 남는다.

## 3. 호가 로직

마켓별 설정: `offset_bps`, `size_usd_per_side`, `max_inventory_usd`.

1. 공정가 = Ondo 중간가. 괴리(Lighter 중간가 / Ondo 중간가 - 1)를 마켓별로 추적해서 아래 경우 그 마켓은 호가를 내린다(`market.basis_status`).
   - 괴리가 최근 `basis_window_sec`(기본 1시간) 중앙값에서 `basis_deviation_bps`(기본 15) 이상 벗어남: 두 거래소가 따로 움직이는 중이라 헷지가 불안하다.
   - 괴리 절대값이 `basis_sanity_bps`(기본 100)를 넘음: 같은 심볼이라도 다른 자산으로 본다(OPENAI 사례).
   - 처음에는 괴리 크기 자체를 기준(30bps)으로 하려 했지만, 정상 마켓인 LIT의 평소 괴리가 약 32bps라 이 방식은 버렸다.
2. 매수 호가 = 공정가 x (1 - offset_bps/1e4), 매도 호가 = 공정가 x (1 + offset_bps/1e4). 기본 offset 18bps. 25bps 안쪽에 들어가되, 가격이 조금 움직여도 band를 벗어나지 않게 여유를 둔다.
3. 가격은 `quoteIncrement`, 수량은 `baseIncrement`에 맞춰 내림한다. 항상 `postOnly=true`, `timeInForce=GTC`.
4. 재고 기울이기: 순포지션이 롱이면 두 호가를 `skew_bps_per_1k` x (재고 USD/1000)만큼 아래로 내린다. 숏이면 위로 올린다. `|재고| >= max_inventory_usd`이면 재고를 늘리는 쪽 호가는 걸지 않는다.
5. 재호가 조건(모두 bps 기준): 현재 호가와 목표 호가 차이 > `requote_threshold_bps`(기본 6), 또는 현재 호가가 band(25bps) 밖으로 나감, 또는 마켓 비활성화.
6. 요청 예산: REST 초당 1회라 분당 60회가 상한이다. 안전과 대사용으로 10회를 남기면 분당 약 50회, 재호가 1번에 일괄 취소+일괄 생성 2회이니 분당 약 25번 재호가가 가능하다. 그래서 1주차는 마켓 3~5개로 제한한다. 재호가 우선순위는 band 밖으로 나간 호가가 먼저다.
7. 주말이나 장 마감처럼 외부 가격이 끊긴 시간의 주식 마켓은 설정 `quote_when_underlying_closed`(기본 false)로 제어한다.

## 4. 헷지 로직

목표: 마켓별로 `Lighter 포지션 = -Ondo 포지션`.

1. `fillsPerps` 이벤트가 오면 해당 마켓의 차이 = Ondo 포지션 + Lighter 포지션을 계산한다.
2. 차이가 Lighter 최소 주문보다 크면 Lighter에 IOC 주문을 보낸다. 최악 가격 = Lighter 중간가 +-`hedge_max_slippage_bps`(기본 15).
3. 주문 결과는 세 가지로 나눈다.
   - 확실히 거절(서명 실패, HTTP 400, 응답 코드가 200이 아님): 바로 더 넓은 가격으로 재시도한다(최대 `hedge_retries`회).
   - 접수됨: 1.5초 뒤 Lighter 포지션을 다시 읽는다. 주문 방향으로 절반 이상 움직였으면 체결로 본다.
   - 모름(응답 유실 같은 통신 예외, 또는 접수됐는데 포지션에 아직 안 보임, 또는 다시 읽기 실패): 미확정 주문으로 기록하고 그 마켓에는 새 헷지 주문을 보내지 않는다. 대사 때마다 포지션이 움직였는지 확인하고, 10초(`PENDING_CONFIRM_SEC`)가 지나도 안 움직이면 미체결로 보고 실패 1회로 센 뒤 더 넓은 가격으로 다시 보낸다.
   회차가 실패하거나 미확정이 만료될 때마다 연속 실패 수가 늘고, 3이면 킬 스위치. 헷지는 예외를 밖으로 내지 않아서 킬 뒤에도 헷지 루프는 계속 돈다.
   Lighter 주문은 SDK의 논스 관리자를 거치도록 `api_key_index`를 넘기지 않는다(넘기면 논스 관리를 건너뛰는 경로를 탄다).
4. 최소 주문보다 작은 차이는 쌓아 두었다가 넘으면 헷지한다.
5. 10초마다 대사: 양쪽 포지션을 REST로 읽어 비교한다(Ondo 요청 분당 6회). WS 포지션 채널은 청산된 포지션을 0으로 보낸다는 보장이 없어 쓰지 않는다. 체결 직후 5초 동안은 REST 스냅샷이 체결 전 값일 수 있어 그 마켓은 덮어쓰지 않는다. 어긋나 있으면 2번과 같은 방식으로 맞춘다.
6. 헷지 비용 기록: Lighter 체결가와 Ondo 체결가의 차이(bps)를 매 건 남긴다. 이 숫자가 전략의 핵심 비용이다.

재고 처리 모드(`inventory_mode`):
- `flatten`(기본): 재고 기울이기로 반대쪽 체결을 유도해 자연스럽게 줄인다.
- `hold_for_funding`: 펀딩이 유리한 방향(예: Ondo 숏이 기본 이자를 받는 쪽)이면 헷지된 재고를 유지하고, 불리해지면 flatten으로 바꾼다.

## 5. 위험 관리 (가장 중요)

이 전략의 최대 손실은 헷지 한쪽만 열린 채로 방치되는 경우다. 7,500달러 노출에서 5% 움직이면 375달러, 기본 시나리오 1~2개월 수익이 날아간다.

1. 시작 시: Ondo 주문 전부 취소 -> 양쪽 포지션 대사 -> 차이 헷지 -> 그 다음에 호가 시작.
2. 데드맨 스위치: 로그인 직후 `cancelAllOrdersAfterPerps`를 `timeout_seconds=30`으로 구독하고 10초마다 갱신. 봇이나 PC가 죽어도 30초 안에 Ondo 호가가 내려간다.
3. 정상 종료(SIGINT/SIGTERM): 호가 전부 취소, 헷지된 포지션은 유지, 마지막 상태 기록. 그래서 밤에 PC를 꺼도 된다.
4. 킬 스위치 조건(하나라도 해당하면 호가 전부 취소, 신규 호가 중단, 알림):
   - 헷지 안 된 노출 > `max_unhedged_usd`(기본 300)가 `max_unhedged_sec`(기본 20초) 이상. 노출 타이머는 위험 루프가 매초 로컬 상태(체결 즉시 반영)로 갱신하므로, 포지션 조회가 계속 실패해도 작동한다
   - Ondo 또는 Lighter WS 끊김 > 10초
   - Lighter 주문 오류 연속 3회
   - 어느 한쪽 증거금 사용률 > `max_margin_usage`(기본 0.5)
   - 마켓 괴리 판정이 `deviation` 또는 `sanity` (해당 마켓만 중단)
   - 일시정지: 양쪽 포지션 조회가 `positions_stale_sec`(기본 30초) 넘게 실패하면 호가를 멈추고, 조회가 돌아오면 재개
   - 당일 실현+미실현 손익 < `-daily_loss_limit_usd`(기본 100)
5. 레버리지: 양쪽 3배 고정. 시작할 때 Ondo `POST /v1/perps/leverage`로 설정하고 확인.
6. 키 관리:
   - Ondo API 키는 거래와 조회 권한만, 출금 권한 없음, IP 화이트리스트 등록.
   - Lighter는 L1 지갑 키가 아니라 별도 API 키(인덱스 4~254)만 봇에 둔다. L1 키는 API 키 등록할 때 한 번만 쓰고 봇 PC에 두지 않는다.
   - 비밀값은 `.env`에만, `.gitignore`에 포함, 로그에 절대 출력하지 않는다.
7. 실행 모드:
   - `dry_run`: 주문을 보내지 않고 낼 호가만 기록한다. 하루 돌려서 재호가 빈도와 요청 예산을 확인한다.
   - `live`: 실제 주문. `live`로 바꾸려면 설정과 명령행 플래그(`--live`) 둘 다 필요.

## 6. 측정과 리포트

SQLite 테이블(`data/olbot.sqlite`):
- `quote_snapshots`: 30초마다 마켓별 내 호가(가격, 수량, 중간가 대비 bps, band 안 여부)
- `band_depth`: 5분마다 마켓별 band 안 전체 호가 물량(내 물량 포함)
- `fills`: Ondo 체결(가격, 수량, maker/taker, 수수료)
- `hedges`: Lighter 체결(가격, 수량, 헷지까지 걸린 ms, Ondo 체결가 대비 비용 bps)
- `funding`: 양쪽 펀딩 지급과 수령
- `rewards`: 주간 USDC 리워드와 포인트. 앱에서 보고 수동 입력(`python -m olbot.report add-reward ...`)
- `events`: 킬 스위치, 재연결, 오류

주간 리포트(마켓별):

| 항목 | 계산 |
|---|---|
| 호가 노출 | band 안에 걸린 USD x 시간 (USD-시간) |
| 추정 점유율 | 내 band 안 물량 / band 안 전체 물량의 시간 평균 |
| 거래량 | Ondo 체결 명목가 합계 |
| 비용 | Ondo 수수료 + 헷지 비용 + 펀딩 순지급 |
| 리워드 | 주간 USDC (마켓별로 안 나오면 호가 노출 비중으로 배분) |
| 핵심 지표 | 1,000 USD-시간당 리워드, 체결 1건당 순손익 |

## 7. 기본 설정과 1주차 계획

자본: Ondo 2,500달러, Lighter 2,500달러(총 5,000달러), 레버리지 3배.

마켓 선정 기준(probe 결과로 판단):
1. Lighter 24시간 거래량 500만 달러 이상(헷지 유동성)
2. Ondo band 안 물량이 얇을 것(내 몫이 커짐)
3. 양쪽 실시간 중간가 괴리가 안정적일 것 (`watch` 요약의 dev_p95가 작을수록 좋다)
4. 1주차는 24시간 거래되는 코인 위주. 주식은 2주차부터.

1주차 후보: LIT, NEAR, ENA, XRP 중 3개 + 비교용 SOL 1개. 한쪽당 호가 500~1,000달러, 마켓당 최대 재고 2,000달러, 전체 최대 재고 5,000달러.

일정:
- 0일차: dry_run 24시간. 재호가 빈도, 요청 예산, 괴리 검사 동작 확인.
- 1~7일차: live 소액. 매일 리포트 확인.
- 수요일 USDC 지급 후: 마켓별 1,000 USD-시간당 리워드 계산 -> 2주차 마켓과 크기 결정.

## 8. 구현 순서 (Claude Code에 단계별로 요청)

각 단계는 완료 기준을 만족해야 다음으로 간다.

1. [완료] 골격과 설정: `pyproject.toml`, `config.py`, 로깅, `.env` 로딩. 완료 기준: `python -m olbot check-config` 통과. `--online`을 붙이면 설정한 마켓이 두 거래소에 실제로 있는지도 확인한다.
2. [완료] 시세 수신(키 불필요): Ondo `topOfBooksPerps` + Lighter `ticker` 구독, 재연결, 공정가와 괴리 계산. 완료 기준: 1시간 끊김 없이 마켓 5개 괴리를 기록. 실행: `python -m olbot watch --markets LIT,NEAR,ENA,SOL,XRP --duration 3600`. 결과는 README의 "2단계 검증 결과"에 있다.
3~7단계는 "500달러로 바로 실운영해서 결과를 보자"는 결정에 따라 한 번에 만들었다(2026-10-27). 단위 테스트(52개)와 실시세 dry_run으로 검증했고, 실제 키로는 집에서 처음 돌린다. 구현하면서 단순화한 것:
   - 재호가는 개별 주문 취소 대신 마켓 단위 전체 취소 + 일괄 생성(요청 2회)으로 한다. 주문 ID를 추적하지 않아도 되고 남는 주문이 생기지 않는다.
   - 헷지 체결 확인은 Lighter 포지션을 다시 읽어 차이로 판단한다. 체결 이벤트를 놓치거나 중복으로 받아도 결과가 틀어지지 않는다.
   - 손실 한도는 손익 계산 대신 두 거래소 자산 합계(Ondo marginBalance + Lighter total_asset_value)의 시작 대비 감소로 본다.
   - 마켓별로 괴리 이탈 기준을 따로 줄 수 있다(markets[].basis_deviation_bps). LIT는 25.
3. [코드 완료] Ondo 인증: HMAC REST 서명, WS 로그인, 잔고/포지션 조회, 데드맨 스위치. 완료 기준: 잔고 조회 성공(샌드박스 접근이 없으면 메인넷 키로 조회만), 단위 테스트로 서명 문자열 검증.
4. [코드 완료, dry_run 확인: 분당 요청 약 25~30회] dry_run 호가기: 호가 계산, 재호가 판단, 요청 예산 토큰 버킷. 완료 기준: 24시간 dry_run에서 분당 요청 50회를 넘지 않음.
5. [코드 완료] Lighter 헷지: SDK로 IOC 주문, 논스 관리, 체결 확인. 완료 기준: 테스트넷에서 주문, 체결 확인, 실패 재시도 테스트.
6. [코드 완료] 위험 관리: 킬 스위치 전 조건, 시작/종료 절차. 완료 기준: 각 조건을 모의로 발생시키는 테스트 통과, 실제로 프로세스를 강제 종료했을 때 30초 안에 Ondo 호가가 사라지는지 샌드박스에서 확인.
7. [코드 완료] 기록과 리포트. 완료 기준: 하루치 데이터로 리포트 출력.
8. 소액 live (500달러, LIT와 ENA). 순서는 docs/HANDOFF.md.

## 9. 예상 손익 (가정, 1주차 측정으로 교체할 숫자)

5,000달러 기준 주간: 유동성 풀 몫 10~200달러, 거래량 풀 - maker 수수료 +5~30달러, 헷지 비용 -10~-30달러, 펀딩 0~+10달러. 월 환산 비관 약 -1%, 기본 3~5%, 낙관 약 20%. 유동성 풀 배분 공식을 모르는 상태의 추정이다. 풀이 고정 금액이라 투입금을 늘려도 수익이 비례해서 늘지 않는다.

## 10. 알려진 위험

- 리워드 규칙은 공지 없이 바뀔 수 있다(베타).
- 같은 심볼이라도 기초자산이나 가격 산정 방식이 다를 수 있다(OPENAI, ZHIPU 사례).
- 주식 마켓은 미국 장 마감과 주말에 양쪽이 각자 내부 가격으로 움직이다가 재개장 때 다르게 튈 수 있다.
- Ondo 공개 베타 점검 중에는 호가 취소가 안 될 수 있다. 데드맨 스위치도 거래소가 살아 있어야 동작한다.
- Lighter 300ms 지연 동안 헷지 가격이 밀린다. 변동성이 큰 시간에는 호가 offset을 넓힌다.
- Ondo와 Lighter 사이 증거금 재조정은 수동이다. 한쪽 증거금 사용률이 높아지면 알림만 보낸다.

## 11. 구현 전에 다시 확인할 것

- Ondo 샌드박스 접근: 공식 가이드상 builders@ondoperps.xyz에 연락해 초대 코드와 API 키 관리를 받아야 하고, 샌드박스 웹사이트는 로그인 벽에 막혀 있다(2026-10-27). 접근을 못 받으면 3단계는 메인넷 조회 전용으로 검증하고, 실제 주문 검증은 체결 불가능한 먼 가격의 최소 수량 post-only 주문으로 사람 승인 후 한다(docs/HANDOFF.md).

- Ondo 유동성 풀 산정 방식: 거리 조건(25bps?), 양방향 필요 여부, 시간 가중, 마켓별 배분, API 주문 포함 여부. 앱의 리워드 페이지와 공지사항, 디스코드에서 확인.
- REST 초당 1회 제한 상향 요청 여부(support@ondoperps.xyz).
- 한국 거주자의 Ondo 리워드와 포인트 수령 자격.
- 각 마켓의 Ondo 펀딩 정산 주기(`/v1/markets`의 `fundingIntervalDivisions` 의미).
- Ondo와 Lighter의 같은 심볼이 같은 기초자산인지, 마켓별 가격 산정 문서로 확인.
- 평소 괴리(LIT 약 32bps 등)가 왜 생기는지. 펀딩 차이나 지수 산정 방식 차이라면 하루 이상 `watch`를 돌려 시간대별로 얼마나 움직이는지 본다. 괴리가 크게 움직이는 마켓은 헷지 비용이 커진다.

## 출처

- Ondo Perps 문서 색인: https://docs.ondoperps.xyz/llms.txt (페이지 URL 끝에 `.md`를 붙이면 마크다운)
- Lighter 문서 색인: https://apidocs.lighter.xyz/llms.txt
- Lighter Python SDK: https://github.com/elliottech/lighter-python
