# Ondo + Lighter 유동성 리워드 봇

Ondo Perps에 지정가(post-only) 호가를 걸어 USDC 리워드와 포인트를 받고, 체결되면 Lighter(수수료 0)에서 반대 포지션을 잡아 가격 위험을 지우는 봇이다. 실운영 코드까지 들어 있고, 집 PC에서 키를 넣고 실행한다.

## 들어 있는 것

| 파일 | 내용 |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | 개요: 무엇을, 왜 만드는지, 목표와 판단 기준 |
| [docs/SPEC.md](docs/SPEC.md) | 설계서: 확인된 API 사실, 구조, 호가와 헷지 로직, 위험 관리, 측정, 구현 순서 |
| [CLAUDE.md](CLAUDE.md) | Claude Code가 이 폴더에서 지킬 규칙 (자동으로 읽힘) |
| [config.live-500.example.yaml](config.live-500.example.yaml) | 500달러 실운영 설정 (LIT, ENA) |
| [config.example.yaml](config.example.yaml) | 기본 설정 예시 (샌드박스/테스트넷 주소) |
| [.env.example](.env.example) | 비밀값 예시 |
| [scripts/ondo_depth_probe.py](scripts/ondo_depth_probe.py) | 마켓 선별용 측정기. 지금 바로 실행 가능, API 키 불필요 |

## 진행 상황

| 단계 | 상태 |
|---|---|
| 1. 골격과 설정 | 완료 |
| 2. 시세 수신과 괴리 기록 | 완료 (1시간 검증) |
| 3~7. Ondo 인증, 호가기, Lighter 헷지, 안전장치, 기록과 리포트 | 테스트 171개 통과. 실제 키 조회 인증, Lighter 무체결 취소·응답 파싱·번호 조회 확인. Ondo 데드맨 강제 종료 시험 도구 준비. 실제 체결·헷지·데드맨 실행 검증은 대기 |
| 8. 소액 실운영 | 미시작. Ondo 시험용 증거금 반영 확인. 실제 체결/헷지/데드맨 검증과 운영 금액 결정이 남음 |

2026-10-28 집 PC 연결 확인: Ondo REST 잔고·포지션 및 WS 로그인, Lighter 키 등록 확인·계정·주문 조회 인증 성공. Ondo는 등록된 IPv4로 직접 연결한다. IPv6 우선 PC에서도 IP 허용목록이나 OS 네트워크 설정을 바꿀 필요가 없다. REST·WS 재연결·`verify-keys`에 같은 정책을 적용하며 TLS 호스트 검증을 유지한다. `websockets>=15`가 필요하다.

2026-10-29 사람 승인 후 Lighter LIT 매수 IOC 1건을 전송했다. 당시 시세 기준 약 10.22 USD, 수량 3.54로 `canceled-too-much-slippage`, 체결 0.00을 확인했다. 정리 주문은 보내지 않았고, 포지션 0·활성 주문 없음·시험 기록 `resolved`를 확인했다. 이어 `verify-keys`의 8개 항목이 모두 통과했으며, 실제 주문 응답 형식과 같은 번호 재조회도 성공했다. 식별값을 가린 실제 응답을 회귀 테스트로 보존했다. 실제 체결·부분 체결·헷지·Ondo 데드맨 검증은 아직 남아 있다.

같은 PC의 `D:\ondo-lighter-bot`에 설치하고 `mode: dry_run`으로 120초 실행했다. 설정 온라인 검증 통과, 두 거래소 공개 WS 각각 연결 1회·재연결 0회, 시세 샘플 8개·계산 호가 스냅샷 16개를 기록하고 정상 종료했다. 실제 주문·취소·체결·헷지·데드맨 변경은 없었다. 이는 짧은 접속 확인이며 24시간 요청 예산 검증이나 실주문 시험을 대신하지 않는다.

단발 시험은 `python -m olbot.trial`로 계획만 조회한다. 실행 플래그는 [HANDOFF](docs/HANDOFF.md)의 범위에 대한 사람 승인 후에만 사용한다. 전체 봇 `run --live`를 이 시험 대신 사용하지 않는다. 미확정 시험 기록은 `data/lighter-order-trial.json`에 보존되며, 해결될 때까지 live 시작을 차단한다.

2026-10-29에는 같은 설치에서 10분(600.55초) dry_run도 정상 종료했다. 두 거래소 WS 각각 연결 1회·재연결 0회, 시세 38개·계산 호가 스냅샷 76개, 체결·헷지 0건이다. 종료 시 `DRY req/min=15.2`는 호가 변경에 필요한 요청의 모의 환산치이며 실제 주문 REST 요청 수가 아니다. 24시간 요청 예산과 실제 헷지·데드맨 검증은 남아 있다.

Ondo 입금 후 `python -m olbot.ondo_trial`의 조회 계획도 확인했다. LIT 1개(기준 시세 약 3 USD), 진입 최대 5 USD, 기존 주문/포지션 없음·시험 증거금 조건 충족이다. 실제 데드맨 활성화/주문은 아직 실행하지 않았다. [제한 시험 범위](docs/ONDO_DEADMAN_TRIAL.md)에 대한 별도 승인이 필요하다. 입금한 소액 시험 자금으로 기존 500달러 운영 설정을 바로 켜지 않는다.

## 설치와 실행 (1~2단계)

Python 3.11 이상이 필요하다. 윈도우는 `.venv/bin/` 대신 `.venv\Scripts\`를 쓴다.

```
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.yaml config.yaml
cp .env.example .env

.venv/bin/python -m pytest                                  # 테스트
.venv/bin/python -m olbot check-config --online             # 1단계: 설정 검증 + 마켓 존재 확인
.venv/bin/python -m olbot watch --markets LIT,NEAR,ENA,SOL,XRP --duration 3600   # 2단계
```

`watch`는 주문을 보내지 않는다. 두 거래소 공개 시세만 받아서 `data/olbot.sqlite`에 괴리를 기록하고, 끝나면 마켓별 요약을 출력한다.

## 실운영 시작하기 (500달러)

자세한 준비 순서와 처음 돌릴 때 확인할 것은 [docs/HANDOFF.md](docs/HANDOFF.md)에 있다. 요약:

```
cp config.live-500.example.yaml config.yaml
cp .env.example .env                      # Ondo, Lighter 키 입력
.venv/bin/python -m olbot check-config --online
.venv/bin/python -m olbot verify-keys                 # 실제 키 인증 확인 (조회만, 주문 없음)
.venv/bin/python -m olbot run --duration 600          # dry_run: 주문 없이 10분, 호가 계산만
.venv/bin/python -m olbot run --live                  # 실운영. Ctrl+C로 종료하면 Ondo 호가 전부 취소
.venv/bin/python -m olbot report --hours 24           # 결과 보기
.venv/bin/python -m olbot report --add-reward 2026-10-27 2026-11-02 1.23 500   # 수요일 리워드 확인 후 기록
.venv/bin/python -m olbot pending list|check          # 결과를 모르는 헷지 주문 보기 / Lighter에 최종 상태 조회
```

봇이 하는 일:
- Ondo에 post-only 호가를 시장가에서 18bps 떨어진 곳에 건다(리워드 인정 범위 25bps 안).
- 가격이 6bps 넘게 움직이면 다시 건다. Ondo REST 제한(초당 1회) 안에서 분당 약 25~30회 요청을 쓴다.
- 체결되면 Lighter에서 IOC 시장가로 반대 포지션을 잡는다. 10초마다 양쪽 포지션을 대사한다. 결과를 모르는 헷지 주문은 같은 마켓에 다시 보내지 않으며, 10초가 지나면 모든 Ondo 호가를 일시정지한다. 시간이 지나도 미확정을 해제하지 않는다.
- 미확정은 전송 전에 SQLite에 저장하고 재시작 시 복원한다. 주문 수량 전체가 해당 방향으로 반영돼야 해제한다. 일부 체결/미체결은 최종 주문 상태 대사 기능이 추가될 때까지 자동 재개하지 않으므로 원래 주문을 확인해야 한다. 같은 마켓을 다른 봇/수동 주문으로 함께 거래하거나, DB를 지우거나 바꿔서 미확정을 우회하면 안 된다.
- 포지션 조회가 30초 넘게 실패하면 호가를 멈춘다. 헷지 안 된 노출 타이머는 조회 실패와 상관없이 체결 시점부터 돈다.
- 두 거래소 가격 차이가 평소보다 크게 벌어진 마켓은 호가를 내린다.
- 헷지 안 된 노출이 40달러를 20초 넘기거나, 헷지가 3번 연속 실패하거나, 자산이 시작 대비 25달러 줄면 호가를 전부 내리고 멈춘다(헷지는 계속). 이 값들은 정지 기준이지 최대 손실 보장이 아니다: 자산은 약 1분 간격으로 보고, 정지 뒤에도 헷지된 포지션은 남는다.
- Ondo 취소 요청이 실패하면 주문을 계속 추적해 다음 루프에서 다시 취소한다. 종료 시에는 3번까지 다시 시도하고, 그래도 실패하면 로그에 경고를 남긴다(데드맨 스위치가 약 30초 뒤 취소).
- 봇이나 PC가 죽어도 Ondo 데드맨 스위치가 30초 안에 호가를 취소한다. 정상 종료(Ctrl+C)하면 호가를 바로 취소하고 헷지된 포지션은 그대로 둔다.

## 2단계 검증 결과 (2026-10-27 10:43~11:43 UTC)

`watch --markets LIT,NEAR,ENA,SOL,XRP --duration 3600` 결과. 완료 기준(1시간 끊김 없이 5개 마켓 기록)을 통과했다.

- 연결: 두 거래소 모두 끊김 0회, 재연결 0회
- 기록: 마켓당 360개 샘플 중 359개가 양쪽 호가 있음 (첫 샘플은 연결 전), 최대 공백 10초

| 마켓 | 괴리 중앙값(bps) | 최소 | 최대 | 표준편차 | dev_p95 |
|---|---|---|---|---|---|
| SOL | 16.7 | 13.2 | 19.3 | 0.8 | 1.7 |
| XRP | 18.0 | 4.9 | 34.8 | 1.6 | 2.1 |
| ENA | 4.4 | -11.1 | 19.2 | 2.0 | 3.2 |
| NEAR | 16.3 | -6.5 | 47.8 | 5.1 | 11.3 |
| LIT | 31.7 | 12.4 | 73.1 | 5.7 | 14.2 |

괴리는 Lighter 중간가가 Ondo보다 비싼 정도. dev_p95는 괴리가 중앙값에서 벗어난 폭의 95% 수준이다.

읽는 법:
- SOL, XRP, ENA는 괴리가 안정적이라 헷지가 깔끔하다.
- LIT, NEAR는 Ondo 호가가 얇은 만큼 괴리도 흔들린다(LIT는 한때 중앙값보다 41bps 벗어남). 유동성 리워드 몫이 가장 큰 마켓이 헷지 비용도 가장 큰 마켓이라는 뜻이다. 기본값 `basis_deviation_bps: 15`이면 LIT는 시간의 약 5%를 쉬게 된다.
- NEAR는 처음 몇 분 괴리가 약 2bps였다가 1시간 중앙값은 16bps였다. 중앙값 기준점 자체도 움직이므로 1시간 창이 적당한지 하루 데이터로 다시 본다.

## 1주차 목표

수익이 아니라 측정이다. 마켓별로 "호가 1,000달러를 1시간 걸었을 때 받는 리워드"와 "체결 1건당 헷지 비용"을 숫자로 뽑는다. 수요일 USDC 지급 후 이 숫자로 2주차 마켓과 크기를 정한다.
