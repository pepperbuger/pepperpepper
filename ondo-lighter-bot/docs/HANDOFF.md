# 집에서 실운영 시작하기 (500달러)

봇 코드는 다 들어 있다. 집 PC에서 할 일은 키를 만들고, 돈을 넣고, 실행하는 것이다.
실제 키로는 아직 한 번도 돌려보지 않았으니 처음 실행에서 한두 군데 고칠 일이 생길 수 있다. 그때는 아래 "문제가 생기면"의 요청문을 Claude Code에 붙여넣는다.

## 1. 설치

Python 3.11 이상. 윈도우는 아래 명령에서 `.venv/bin/`을 `.venv\Scripts\`로 바꾼다.

```
git clone https://github.com/pepperbuger/pepperpepper.git
cd pepperpepper
git checkout claude/tender-ramanujan-hvzxq8      # PR #1이 병합됐다면 생략 (main)
cd ondo-lighter-bot
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                      # 전부 통과해야 한다
```

## 2. 키 만들기

Ondo Perps (app.ondoperps.xyz, 오른쪽 위 주소 클릭 -> API Keys)
- 권한: 거래와 조회만. 출금 권한은 끈다.
- IP 화이트리스트: 집 PC의 공인 IPv4 주소를 넣는다 (네이버에 "내 IP" 검색). 공유기를 껐다 켜면 IP가 바뀔 수 있다.
- 키 ID(`ondoKeyId_...`)와 시크릿(`ondoApiSecret_...`)을 기록한다. 시크릿은 다시 볼 수 없다.

Lighter (app.lighter.xyz)
- API 키를 하나 만든다. 인덱스는 4~254 중 하나(0~3은 예약). 키 등록에는 지갑 서명이 필요하다.
- 계정 번호(account index) 확인: 브라우저에서 아래 주소를 연다. `sub_accounts`의 첫 번째 `index`가 메인 계정이다.
  `https://mainnet.zklighter.elliot.ai/api/v1/accountsByL1Address?l1_address=내지갑주소`
- 지갑 개인키는 봇에 넣지 않는다. 봇은 API 키의 개인키만 쓴다.

## 3. 돈 넣기

- Ondo Perps: 250 USDC
- Lighter: 250 USDC

## 4. 설정

```
cp config.live-500.example.yaml config.yaml
cp .env.example .env
```

`.env`에 키 다섯 개를 채운다: `ONDO_API_KEY_ID`, `ONDO_API_SECRET`, `LIGHTER_API_KEY_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`.

```
.venv/bin/python -m olbot check-config --online    # "검증 통과"가 나와야 한다. 메인넷 경고는 정상
```

## 5. 처음 실행 순서

1. dry_run 10분: `.venv/bin/python -m olbot run --duration 600`
   주문 없이 호가 계산만 한다. 1분마다 `DRY req/min=... | LIT[mid=... q=매수/매도 ...]` 줄이 나오면 정상.
2. 실운영 10분: `.venv/bin/python -m olbot run --live --duration 600`
   - 시작할 때 Lighter 키 확인, Ondo 주문 전부 취소, 레버리지 3배 설정, 포지션 대사를 하고 "시작 자산 ... USD"를 찍는다.
   - Ondo 앱에서 LIT, ENA에 매수와 매도 호가가 하나씩 걸렸는지 본다.
   - 10분 뒤 자동 종료되면 Ondo 앱에서 호가가 모두 사라졌는지 본다.
3. 데드맨 스위치 확인(권장): `run --live`를 켠 상태에서 터미널 창을 그냥 닫는다(Ctrl+C 말고). 30초 안에 Ondo 앱에서 호가가 사라져야 한다.
4. 계속 운영: `.venv/bin/python -m olbot run --live`. 끌 때는 Ctrl+C.

## 6. 결과 보기

```
.venv/bin/python -m olbot report --hours 24
```

- 호가 노출(USD-시간), 체결 건수와 수수료, 헷지 주문, 자산 변화가 나온다.
- 매주 수요일 Ondo 앱 리워드 페이지에서 받은 USDC와 포인트를 확인하고 기록한다:
  `.venv/bin/python -m olbot report --add-reward 2026-10-27 2026-11-02 받은USDC 받은포인트`
  그러면 "1,000 USD-시간당 리워드"가 계산된다. 이 숫자가 5,000달러로 키울지 정하는 기준이다.
- 로그는 `data/olbot.log`, 기록은 `data/olbot.sqlite`.

## 7. 멈추는 경우 (정상 동작)

| 로그 | 뜻 | 할 일 |
|---|---|---|
| `PAUSE: ...` | WS 끊김, 포지션 조회 실패, 증거금 사용률 높음 등. 원인이 사라지면 자동 재개 | 지켜본다. 포지션 조회 실패가 길어지면 인터넷과 Lighter, Ondo 상태 확인 |
| `send outcome unknown`, `unconfirmed order` | 헷지 주문 결과를 모름. 최대 10초 기다린 뒤 포지션으로 판단 | 반복되면 Lighter 앱에서 주문과 포지션 확인 |
| `LIT 호가 내림: deviation` | 두 거래소 가격 차이가 평소보다 크게 벌어짐 | 지켜본다 |
| `KILL: ...` | 헷지 안 된 노출 지속, 헷지 3회 실패, 자산 25달러 감소 | 원인 확인 후 재시작. Ondo와 Lighter 포지션이 서로 반대인지 앱에서 확인. 25달러는 정지 기준이라 실제 손실은 더 클 수 있다 |
| `cancel 실패` | Ondo 취소 요청 실패. 봇이 다음 루프에서 다시 시도 | 반복되면 Ondo 앱에서 호가가 남아 있는지 확인 |

## 8. 문제가 생기면

Claude Code를 `ondo-lighter-bot` 폴더에서 열고 붙여넣는다:

```
ondo-lighter-bot 폴더의 봇을 실제 키로 처음 돌리다가 문제가 생겼다. 먼저 CLAUDE.md, README.md, docs/SPEC.md를 읽어라.
아래 로그를 보고 원인을 찾아 고쳐라.
- 엔드포인트, 필드 이름, 채널 이름은 추측하지 말고 docs.ondoperps.xyz/llms.txt와 apidocs.lighter.xyz/llms.txt에서 확인한다.
- 주문을 보내는 테스트는 실행 전에 반드시 나에게 묻는다.
- 키는 .env에서만 읽고, 로그나 커밋에 남기지 않는다.
- 고친 뒤 .venv/bin/python -m pytest 가 전부 통과해야 한다.

[여기에 data/olbot.log의 마지막 50줄을 붙여넣는다. 키 값이 보이면 지운다]
```

## 참고: Ondo 샌드박스

Ondo 샌드박스는 builders@ondoperps.xyz에 연락해 팀 승인을 받아야 쓸 수 있고(웹사이트는 로그인 벽), 개인이 바로 쓸 수 없다. 그래서 이 봇은 메인넷 500달러 소액 운영으로 검증한다.
