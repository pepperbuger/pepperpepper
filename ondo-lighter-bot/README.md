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
| 3~7. Ondo 인증, 호가기, Lighter 헷지, 안전장치, 기록과 리포트 | 코드 완료, 단위 테스트와 dry_run 검증. 실제 키로는 아직 안 돌려봄 |
| 8. 소액 실운영 | 집에서 키를 넣고 시작 |

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
.venv/bin/python -m olbot run --duration 600          # dry_run: 주문 없이 10분, 호가 계산만
.venv/bin/python -m olbot run --live                  # 실운영. Ctrl+C로 종료하면 Ondo 호가 전부 취소
.venv/bin/python -m olbot report --hours 24           # 결과 보기
.venv/bin/python -m olbot report --add-reward 2026-10-27 2026-11-02 1.23 500   # 수요일 리워드 확인 후 기록
```

봇이 하는 일:
- Ondo에 post-only 호가를 시장가에서 18bps 떨어진 곳에 건다(리워드 인정 범위 25bps 안).
- 가격이 6bps 넘게 움직이면 다시 건다. Ondo REST 제한(초당 1회) 안에서 분당 약 25~30회 요청을 쓴다.
- 체결되면 Lighter에서 IOC 시장가로 반대 포지션을 잡는다. 10초마다 양쪽 포지션을 대사해서 어긋난 만큼 다시 맞춘다. 결과를 모르는 헷지 주문(응답 유실, 포지션 반영 지연)이 있으면 확인될 때까지(최대 10초) 같은 마켓에 다시 보내지 않는다.
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
