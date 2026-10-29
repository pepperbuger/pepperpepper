# 집에서 실운영 시작하기 (500달러)

봇 코드는 다 들어 있다. 집 PC에서 할 일은 키를 만들고, 돈을 넣고, 실행하는 것이다.
2026-10-29 현재: 실제 키 조회 인증과 `verify-keys` 8개 항목이 통과했고, Lighter IOC 무체결 취소·실제 파싱·번호 재조회도 확인했다. 이후 Ondo LIT 1개 시험 호가는 거래소 기록상 생성 후 29.832초에 타임아웃 취소됐지만, 응답 형식 차이로 검사기의 실시간 판정은 실패했다. 실제 응답/ACK를 이용해 수정했으며, 정상 감시 재시험은 새 주문에 대한 승인 대기 중이다. 양쪽 포지션과 활성 주문은 없는 것으로 확인했고, 실제 체결·부분 체결·헷지는 아직 검증하지 않았다. 전체 봇은 dry_run 및 live 차단 상태로 유지한다.

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

Ondo 클라이언트는 REST·WS 모두 IPv4로 직접 접속한다. 허용목록에 IPv4가 있는데도 IPv6 접속으로 `ip_not_permitted`가 나는 문제를 피하기 위한 것이다. PC 전체 네트워크 설정이나 기존 IP 허용목록은 바꾸지 않는다. 설치 시 `websockets>=15`가 필요하다. `verify-keys`에서 과거 주문이 없어 실패한 항목은 인증 실패와 구분하며 실제 주문 시험은 별도 승인 후 진행한다.

Ondo Perps (app.ondoperps.xyz, 오른쪽 위 주소 클릭 -> API Keys)
- 권한: 거래와 조회만. 출금 권한은 끈다.
- IP 화이트리스트: 집 PC의 공인 IPv4 주소를 넣는다 (네이버에 "내 IP" 검색). 공유기를 껐다 켜면 IP가 바뀔 수 있다.
- 키 ID(`ondoKeyId_...`)와 시크릿(`ondoApiSecret_...`)을 기록한다. 시크릿은 다시 볼 수 없다.

Lighter (app.lighter.xyz)
- API 키를 하나 만든다. 인덱스는 4~254 중 하나(0~3은 예약). 키 등록에는 지갑 서명이 필요하다.
- 계정 번호(account index) 확인: 브라우저에서 아래 주소를 연다. `sub_accounts`의 첫 번째 `index`가 메인 계정이다.
  `https://mainnet.zklighter.elliot.ai/api/v1/accountsByL1Address?l1_address=내지갑주소`
- 지갑 개인키는 봇에 넣지 않는다. 봇은 API 키의 개인키만 쓴다.
- 예전에 받아둔 Lighter 키가 있으면 그대로 쓴다. 메인넷 키인지 테스트넷 키인지에 따라 `config.yaml`의 `lighter.base_url`과 `market_data.lighter_rest_url`을 같은 네트워크로 맞춘다(설정 예시는 메인넷). 계정 번호도 그 네트워크 기준이다.

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
.venv/bin/python -m olbot verify-keys              # 실제 키로 인증만 확인. 주문 없음, 데드맨도 켜지 않음
```

`verify-keys`는 Ondo 잔고/포지션 조회, Ondo WS 로그인, Lighter 키 등록 확인과 계정 조회, Lighter 주문 조회 인증, Lighter 주문 응답 형식, Lighter 주문 번호 조회를 차례로 보고 각각 OK/실패를 찍는다. 전부 OK여야 다음으로 간다.
- 주문 응답 형식과 번호 조회는 이 Lighter 계정의 최근 끝난 주문 기록으로 확인한다(주문을 새로 내지 않는다). 봇이 미확정 헷지를 풀 때 쓰는 파서와 조회 경로를 실제 응답으로 한 번 통과시키는 단계다.
- "끝난 주문 기록이 없어"가 나오면: 인증 성공과 별개로 실제 주문 응답 형식이 미검증이라는 뜻이다. 공개 시세를 쓰는 dry_run은 진행할 수 있지만 live는 보류한다. 실제 주문 시험은 마켓·수량·감수할 손실·종료 절차를 사람이 확인하고 승인한 뒤 진행한다. 완료 주문 기록이 생기면 다시 실행한다.
- "파서가 실제 주문을 거부"가 나오면: 출력된 status와 필드 목록을 "문제가 생기면"의 요청문과 함께 Claude Code에 붙여넣는다. 고치기 전에는 live로 가지 않는다.
- "client_order_index가 없어"가 나오면: 앱 주문에는 번호가 없을 수 있다. 같은 내용을 붙여넣고 확인을 요청한다.
- `HTTP 401 ip_not_permitted`: Ondo 키의 IP 화이트리스트에 지금 공인 IP가 없음
- `HTTP 403 forbidden_country`: 접속 지역 차단 (미국 등). 한국 집 PC에서는 나오지 않아야 한다
- `timestamp_too_far`: PC 시계가 틀림. 윈도우 시간 동기화
- `key id not found`: 키 ID 오타

## 5. 처음 실행 순서

### 실제 주문 기록이 없을 때의 제한 시험

`python -m olbot.trial`은 메인넷 LIT의 현재 최소 수량·가격·증거금 충족 여부를 조회만 한다. 사람 승인 후에만 다음을 실행한다.

```
.venv/bin/python -m olbot.trial --execute --approve-max-entry-usd 12 --approve-cleanup
```

- 범위: Lighter LIT만, 진입 IOC 매수 최대 12 USD(현 시세 기준), 체결되면 확인된 수량만 reduce-only IOC 매도 1회로 정리한다. 처음 가격 상한은 최우선 매수보다 1% 아래로 정해 미체결 종료를 의도한다. 시장 급변 시 체결될 수 있고, 정리 주문이 실패하거나 일부만 체결될 수도 있다. 손실 상한 보장이 아니다.
- 기존 포지션/활성 주문이 있거나 가용 증거금이 15 USDC보다 적으면 실행을 거부한다. 이 15 USDC는 시험의 보수적인 여유 조건이며 전체 봇의 운영 자본이 아니다. 같은 계정의 다른 거래는 중단한 상태여야 한다.
- 기록 파일이 이미 있으면 새 주문을 보내지 않는다. 플래그 없이 다시 실행하면 기존 번호를 조회한다. 미확정이나 부분 정리이면 기록을 보존하고 판단을 요청한다. 파일을 지워 재시도하지 않는다.
- 이 시험은 출금·이체·레버리지 변경·키 등록·Ondo 주문·전체 봇 live 실행을 하지 않는다. 종료 상태와 0 포지션 확인 후 `verify-keys`의 실제 형식/번호 조회를 다시 확인한다.

### 전체 봇 시험 (별도 승인 필요)

Ondo 첫 시험의 호가는 실제 타임아웃으로 취소됐지만, 응답의 선택 필드 생략/소문자 취소 사유 때문에 검사기 판정은 실패했다. 파서를 수정하고 무체결·포지션 0·활성 주문 없음을 대사했으며, 최초 실패 기록과 live 차단은 유지했다. 새 호가가 필요한 정상 감시 재시험은 별도 승인 후 `--retry-resolved`로 실행한다. 이전 기록을 삭제하지 않는다.

Ondo 입금 후에도 바로 전체 봇을 켜지 않는다. 이번에 준비한 소액 자금은 [최소 수량 데드맨 시험](ONDO_DEADMAN_TRIAL.md)에만 사용한다. `python -m olbot.ondo_trial`로 계획을 조회하고, 실제 시험은 문서의 주문·계정 전체 취소 타이머·비상 정리 범위를 승인받은 뒤 수행한다. 전체 운영은 데드맨·실제 헷지 검증과 잔고에 맞는 주문 금액을 별도로 확인한 뒤 진행한다.

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
| `send outcome unknown`, `헷지 주문 미확정` | 결과를 모르는 헷지. 같은 마켓의 추가 헷지를 막고 10초가 지나면 모든 Ondo 호가 일시정지 | 봇이 주문 번호로 Lighter에 최종 상태를 계속 조회해서, 확정되면 스스로 풀고 재개한다. 오래 안 풀리면 `pending check`로 상태를 본다 |
| `LIT 호가 내림: deviation` | 두 거래소 가격 차이가 평소보다 크게 벌어짐 | 지켜본다 |
| `KILL: ...` | 헷지 안 된 노출 지속, 헷지 3회 실패, 자산 25달러 감소 | 원인 확인 후 재시작. Ondo와 Lighter 포지션이 서로 반대인지 앱에서 확인. 25달러는 정지 기준이라 실제 손실은 더 클 수 있다 |
| `cancel 실패` | Ondo 취소 요청 실패. 봇이 다음 루프에서 다시 시도 | 반복되면 Ondo 앱에서 호가가 남아 있는지 확인 |

미확정 헷지는 `storage.sqlite_path`의 `pending_hedges`에 주문 번호(client_order_index)와 함께 전송 전에 저장되며, 재시작해도 같은 계정의 기록을 복원한다. 기록이 있는 마켓을 설정에서 빼면 시작을 거부한다. DB 삭제/교체로 우회하지 말고 기존 DB를 유지한다.

미확정은 시간이 지나도 풀리지 않는다. 봇은 그 주문 번호로 Lighter `accountOrders`를 조회해서, 주문이 `filled` 또는 공식 취소 상태(`canceled-*` 13종)로 끝났고 포지션이 그 체결 수량만큼 반영된 것까지 확인한 뒤에만 푼다. 일부 체결이나 미체결(가격 초과 취소 등)도 이렇게 확정되면 남은 양만 더 넓은 가격으로 다시 헷지하고, 다른 정지 사유가 없으면 호가를 자동 재개한다. 거래소에서 주문을 찾지 못하는 동안(아직 반영 전이거나 전송 안 됨)은 계속 기다린다.

사람이 직접 확인할 때:
```
.venv/bin/python -m olbot pending list            # 미확정 기록 보기
.venv/bin/python -m olbot pending check           # 주문 번호로 Lighter에 최종 상태 조회 (조회만)
.venv/bin/python -m olbot pending clear LIT --yes # Lighter 앱에서 확인한 뒤에만. 확인 없이 지우면 중복 헷지 위험
```
`clear`가 필요한 경우는 주문 번호가 없는 예전 기록이거나, `check`에서 계속 "찾지 못함"이 나오는데 Lighter 앱에서 그 주문이 없다는 걸 확인한 경우뿐이다. 동일 계정/마켓에 다른 봇이나 수동 주문을 섞지 않는다(포지션 반영 확인이 틀어진다).

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
