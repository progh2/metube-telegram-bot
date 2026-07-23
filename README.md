# MeTube Telegram Bot

[![test](https://github.com/progh2/metube-telegram-bot/actions/workflows/test.yml/badge.svg)](https://github.com/progh2/metube-telegram-bot/actions/workflows/test.yml)

텔레그램으로 유튜브 링크를 보내면 NAS의 [MeTube](https://github.com/alexta69/metube)가 대신 다운로드해 주는 작은 텔레그램 봇입니다.

A tiny Telegram bot that forwards YouTube (or any yt-dlp-supported) links to your self-hosted [MeTube](https://github.com/alexta69/metube) instance, so your NAS downloads them for you.

```
링크 전송  →  형식 버튼 선택  →  NAS가 알아서 다운로드
```

---

## 목차

- [기능](#기능--features)
- [동작 원리](#동작-원리--how-it-works)
- [구조](#구조--architecture)
- [설치](#설치--setup)
- [사용법](#사용법--usage)
- [환경 변수](#환경-변수--environment-variables)
- [주의사항](#주의사항--caveats)
- [문제 해결](#문제-해결--troubleshooting)
- [테스트](#테스트--tests)

---

## 기능 / Features

- 🎬 영상 최고화질(MP4) / 🎵 최고음질 MP3 / 🎬+🎵 둘 다 — 링크마다 버튼으로 선택
- `둘 다` 선택 시 MeTube의 URL 중복 제한을 우회해 **영상과 음원을 모두** 등록
- 응답은 **링크 미리보기 카드 없이 2~3줄**로 간결하게 (썸네일·설명이 반복되지 않음)
- 한 메시지에 링크 여러 개를 넣으면 각각에 대해 버튼이 따로 표시됨
- 재생목록 링크 지원 (MeTube가 전체를 대기열에 등록)
- 폴링(long polling) 방식이라 포트 포워딩·외부 노출 불필요
- `ALLOWED_CHAT_IDS`로 허용된 사용자만 사용 가능
- 단일 파일(`bot.py`) + 의존성 2개 — 읽고 고치기 쉬움

---

## 동작 원리 / How it works

봇 자체는 **다운로드를 하지 않습니다.** 텔레그램과 MeTube 사이에서 "링크를 받아 형식을
물어보고, MeTube의 `/add` API로 넘기는" 얇은 중계기 역할만 합니다. 실제 다운로드,
대기열 관리, 파일 저장은 전부 MeTube(내부적으로 yt-dlp)가 담당합니다.

### 전체 흐름 (시퀀스)

```mermaid
sequenceDiagram
    autonumber
    actor U as 사용자
    participant TG as Telegram 서버
    participant B as metube-telegram-bot<br/>(bot.py)
    participant M as MeTube<br/>(:8081)
    participant Y as yt-dlp / YouTube

    Note over B,TG: 봇이 getUpdates 롱폴링으로 접속<br/>(인바운드 포트 개방 불필요)

    U->>TG: https://youtu.be/... 전송
    TG-->>B: Update (message)

    B->>B: allowed(chat_id) 확인
    alt 허용되지 않은 챗 ID
        B-->>TG: "허용되지 않은 사용자입니다"
        TG-->>U: 거부 메시지
    else 허용됨
        B->>B: URL_RE로 URL 추출
        B->>B: token = uuid4[:10]<br/>pending[token] = url
        B-->>TG: sendMessage + InlineKeyboard<br/>(callback_data = "mp4:token" 등)
        TG-->>U: 🎬 영상 / 🎵 MP3 / 🎬+🎵 둘 다

        U->>TG: 버튼 탭
        TG-->>B: Update (callback_query)
        B->>TG: answerCallbackQuery (로딩 표시 해제)
        B->>B: url = pending.pop(token)

        alt 토큰 없음 (봇 재시작 등)
            B-->>TG: "요청이 만료되었어요"
        else 토큰 유효
            B->>M: POST /add<br/>{url, quality:"best", format, auto_start:true}
            M-->>B: {"status": "ok"}
            M->>Y: 다운로드 시작
            Y-->>M: 미디어 파일
            M->>M: 저장 폴더에 기록
            B-->>TG: editMessageText "✅ 요청 완료"<br/>(링크 미리보기 비활성)
            TG-->>U: 결과 표시
        end
    end
```

### 왜 "둘 다"는 한 번에 보내지 않나요?

MeTube는 **대기열 중복을 URL만으로 판정**합니다. `format`은 판정 키에 포함되지 않기 때문에,
같은 링크를 mp4·mp3로 연달아 보내면 **두 번째 요청은 조용히 버려집니다.**

```python
# MeTube app/ytdl.py — __add_entry()
key = entry.get('webpage_url') or entry['url']      # ← URL 단독이 키
if self.queue.exists(key) or self.pending.exists(key):
    return {'status': 'ok', 'msg': f'Already in queue: {title}'}   # HTTP 200
```

다행히 **완료된 항목(`done`)은 중복 검사 대상이 아니므로**, 앞선 다운로드가 대기열을 떠나면
같은 URL을 다른 형식으로 다시 등록할 수 있습니다. 봇은 이 성질을 이용합니다.

```mermaid
sequenceDiagram
    autonumber
    participant B as bot.py
    participant M as MeTube

    Note over B: "🎬+🎵 둘 다" 선택

    B->>M: POST /add (format=mp3)
    Note right of B: 음원이 영상보다 빨리 끝나므로<br/>MP3를 먼저 등록
    M-->>B: {"status":"ok"}
    B-->>B: "✅ 요청 완료 / ⏳ 영상은 이어서" 표시
    B-->>B: 백그라운드 작업 시작

    loop 중복이 아닐 때까지 (15초 → 최대 5분 간격, 최대 2시간)
        B->>M: POST /add (format=mp4)
        M-->>B: {"status":"ok",<br/>"msg":"Already in queue"}
        Note right of B: 아직 MP3 처리 중 → 대기 후 재시도
    end

    Note over M: MP3 완료 → queue에서 done으로 이동

    B->>M: POST /add (format=mp4)
    M-->>B: {"status":"ok"}  (중복 아님)
    B-->>B: "✅ 둘 다 요청 완료" 로 메시지 수정
```

재시도 간격을 점점 늘리는 이유는, MeTube가 `/add` 요청마다 yt-dlp로 메타데이터를 다시 조회하기
때문입니다. 짧은 간격으로 반복하면 대상 사이트에서 요청 제한에 걸릴 수 있습니다.

### 왜 토큰을 쓰나요? (핵심 설계 포인트)

텔레그램의 `callback_data`는 **64바이트 제한**이 있어 긴 URL을 그대로 담을 수 없습니다.
그래서 URL마다 짧은 토큰(`uuid4().hex[:10]`)을 발급해 `callback_data`에는 `형식:토큰`만
넣고, 실제 URL은 프로세스 메모리의 `pending` 딕셔너리에 보관합니다.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> 대기중 : URL 수신 시<br/>pending[token] = url
    대기중 --> 소비됨 : 버튼 클릭<br/>pending.pop(token)
    소비됨 --> [*] : MeTube로 전달
    대기중 --> 소멸 : 봇 재시작 /<br/>컨테이너 재배포
    소멸 --> [*] : "요청이 만료되었어요"
```

> **의도된 트레이드오프**: `pending`은 디스크에 저장하지 않습니다. 봇을 재시작하면
> 아직 누르지 않은 버튼은 무효가 되고, 다시 누르면 만료 안내가 나옵니다.
> 링크를 다시 보내면 됩니다. (DB 없이 단일 파일을 유지하기 위한 선택)

### 성공/실패 판정

MeTube 응답은 **HTTP 200이고 본문의 `status`가 `"error"`가 아니면 성공**으로 봅니다.
`Content-Type` 헤더는 신뢰하지 않습니다 — 과거에 헤더 기반 판정 때문에 정상 요청을
실패로 표시하는 버그가 있었습니다.

```mermaid
flowchart TD
    A["POST /add"] --> B{"예외 발생?"}
    B -- "예 (연결 실패/타임아웃)" --> F["❌ 실패<br/>str(e) 표시"]
    B -- 아니오 --> C{"r.ok (HTTP 2xx)?"}
    C -- 아니오 --> G["❌ 실패<br/>msg 또는 HTTP 코드"]
    C -- 예 --> D{"body.status == 'error'?"}
    D -- 예 --> G
    D -- 아니오 --> E["✅ 성공"]
```

---

## 구조 / Architecture

### 배포 구성 (컴포넌트)

```mermaid
flowchart LR
    subgraph internet["인터넷"]
        TG["Telegram Bot API<br/>api.telegram.org"]
        YT["YouTube 등<br/>yt-dlp 지원 사이트"]
    end

    subgraph nas["NAS (시놀로지 / Docker)"]
        subgraph c1["container: metube-telegram-bot"]
            BOT["bot.py<br/>python-telegram-bot v21"]
        end
        subgraph c2["container: metube"]
            MT["MeTube<br/>:8081"]
            YD["yt-dlp"]
        end
        VOL[("다운로드 폴더<br/>/downloads")]
    end

    PHONE["📱 텔레그램 앱"] <--> TG
    BOT -- "① getUpdates<br/>아웃바운드 롱폴링" --> TG
    BOT -- "② POST /add<br/>내부망 HTTP" --> MT
    MT --> YD
    YD -- "③ 다운로드" --> YT
    YD --> VOL
    PHONE -. "웹 UI로 진행 확인" .-> MT
```

**포인트**: 봇은 텔레그램으로 **바깥으로 나가는** 연결만 만듭니다. NAS에 포트를 열거나
공인 IP를 노출할 필요가 없습니다. 봇 → MeTube 호출은 같은 내부망(또는 같은 도커 네트워크)에서
이루어집니다.

### 모듈 구조 (bot.py)

```mermaid
classDiagram
    class Config {
        +BOT_TOKEN: str
        +METUBE_URL: str
        +ALLOWED_CHAT_IDS: set[int]
        +URL_RE: Pattern
        +pending: dict[str, str]
    }

    class Handlers {
        +cmd_start(update, ctx) async
        +cmd_id(update, ctx) async
        +on_message(update, ctx) async
        +on_button(update, ctx) async
    }

    class Actions {
        +handle_single(ctx, chat, msg, url, fmt) async
        +handle_both(ctx, chat, msg, url) async
        +queue_followup(...) async
        +edit_text(ctx, chat, msg, text) async
    }

    class MetubeClient {
        +metube_add(url, fmt) AddResult
        +metube_add_async(url, fmt) async
    }

    class AddResult {
        <<NamedTuple>>
        +ok: bool
        +duplicate: bool
        +msg: str
    }

    class Auth {
        +allowed(chat_id) bool
    }

    class Application {
        <<python-telegram-bot>>
        +run_polling()
        +create_task()
    }

    Application --> Handlers : 이벤트 디스패치
    Handlers --> Auth : 챗 ID 검사
    Handlers --> Config : pending 읽기/쓰기
    Handlers --> Actions : 형식별 처리 위임
    Actions --> MetubeClient : 다운로드 요청
    Actions --> Application : 후속 등록 태스크 예약
    MetubeClient --> AddResult : 결과 반환
    MetubeClient --> Config : METUBE_URL
```

| 핸들러 | 트리거 | 하는 일 |
|---|---|---|
| `cmd_start` | `/start` | 사용 안내 출력 |
| `cmd_id` | `/id` | 현재 대화의 챗 ID 표시 (`ALLOWED_CHAT_IDS` 설정용) |
| `on_message` | 명령이 아닌 텍스트 | URL 추출 → 토큰 발급 → 형식 선택 버튼 표시 |
| `on_button` | 인라인 버튼 콜백 | 토큰으로 URL 복원 → `handle_single()` / `handle_both()` 로 위임 |

### 파일

| 파일 | 설명 |
|---|---|
| `bot.py` | 봇 전체 로직 (단일 파일) |
| `test_bot.py` | 테스트 (표준 라이브러리 `unittest`만 사용) |
| `Dockerfile` | `python:3.12-slim` 기반 이미지 |
| `docker-compose.example.yml` | 공개용 템플릿 — 복사해서 `docker-compose.yml`로 사용 |
| `.gitignore` | 실제 토큰이 들어가는 `docker-compose.yml`, `.env` 제외 |

---

## 요구 사항 / Requirements

- 실행 중인 MeTube 인스턴스 (예: 시놀로지 Container Manager)
- Docker + docker-compose
- 텔레그램 봇 토큰 ([@BotFather](https://t.me/BotFather)에서 `/newbot`으로 발급)

---

## 설치 / Setup

```bash
git clone https://github.com/progh2/metube-telegram-bot.git
cd metube-telegram-bot
cp docker-compose.example.yml docker-compose.yml
# docker-compose.yml 을 열어 BOT_TOKEN, METUBE_URL 입력
docker-compose up -d --build
```

시놀로지 Container Manager를 쓴다면: 이 폴더를 `/volume1/docker/` 아래에 두고
**프로젝트 → 생성 → 폴더 선택 → 기존 docker-compose.yml 사용**으로 올리면 됩니다.

### 사용자 제한 (필수에 가까운 권장) / Restrict access

1. 봇에게 `/id` 를 보내 내 챗 ID 확인
2. `docker-compose.yml` 의 `ALLOWED_CHAT_IDS=` 에 입력 (쉼표로 여러 명 가능)
3. `docker-compose up -d` 로 재적용

```mermaid
flowchart LR
    A["/id 전송"] --> B["챗 ID 확인<br/>예: 123456789"]
    B --> C["ALLOWED_CHAT_IDS=123456789"]
    C --> D["docker-compose up -d"]
    D --> E["이제 나만 사용 가능 ✅"]
```

> ⚠️ 비워두면 **봇 아이디를 아는 누구나** 여러분의 NAS에 다운로드를 시킬 수 있습니다.
> 텔레그램 봇 아이디는 검색으로 노출될 수 있으니 꼭 설정하세요.

---

## 사용법 / Usage

1. 봇과의 대화창에 링크를 보냅니다. (유튜브 앱의 "공유 → 텔레그램"이 가장 편합니다)
2. 뜨는 버튼에서 형식을 선택합니다.

| 버튼 | 결과 |
|---|---|
| 🎬 영상 (최고화질) | MP4, `quality: best` |
| 🎵 MP3 (최고음질) | MP3 오디오만 |
| 🎬+🎵 둘 다 | MP3를 먼저 등록하고, 영상은 MP3 처리가 끝나는 대로 **자동으로 이어서** 등록 ([이유](#왜-둘-다는-한-번에-보내지-않나요)) |

3. `✅ 요청 완료` 가 뜨면 대기열 등록이 끝난 것입니다.
   `둘 다`를 골랐다면 영상이 추가되는 시점에 메시지가 `✅ 둘 다 요청 완료` 로 바뀝니다.
4. 진행 상황과 완료 파일은 MeTube 웹 UI(기본 `:8081`)에서 확인합니다.

### 명령어

| 명령 | 설명 |
|---|---|
| `/start` | 사용 안내 |
| `/id` | 이 대화의 챗 ID 확인 |

---

## 환경 변수 / Environment variables

| 변수 | 설명 | 기본값 |
|---|---|---|
| `BOT_TOKEN` | 텔레그램 봇 토큰 (**필수** — 없으면 기동 실패) | - |
| `METUBE_URL` | MeTube 주소 (끝의 `/`는 자동 제거) | `http://localhost:8081` |
| `ALLOWED_CHAT_IDS` | 허용할 챗 ID (쉼표 구분, 비우면 **전체 허용**) | (비어 있음) |

---

## 주의사항 / Caveats

**보안**

- 🔴 `BOT_TOKEN`은 절대 커밋하지 마세요. 실제 값이 든 `docker-compose.yml`은 `.gitignore`에 등록되어 있습니다. 토큰이 유출됐다면 BotFather에서 `/revoke`로 즉시 폐기하세요.
- 🔴 `ALLOWED_CHAT_IDS`를 비워두지 마세요. 인증이 사라져 아무나 NAS 대역폭과 저장 공간을 쓰게 됩니다.
- 🟡 `METUBE_URL`은 내부망 주소를 쓰세요. MeTube를 외부에 노출하고 있다면 별도 인증(리버스 프록시 등)을 두는 편이 안전합니다.

**동작상의 제약**

- 🟡 **재시작하면 대기 중인 버튼은 무효**가 됩니다. `pending`이 메모리에만 있기 때문입니다 (위 [상태 다이어그램](#왜-토큰을-쓰나요-핵심-설계-포인트) 참고). 링크를 다시 보내면 됩니다.
- 🟡 봇이 알려주는 것은 **"MeTube에 요청 성공"까지**입니다. 다운로드 자체의 성공/실패는 MeTube 웹 UI에서 확인해야 합니다.
- 🟡 `둘 다`를 고르면 두 파일이 **동시에 받아지지 않습니다.** MP3가 끝나야 영상이 대기열에 올라가므로, 중간에 확인하면 한쪽만 보입니다. 잠시 뒤 다시 보세요.
- 🟡 `둘 다`로 받으면 MeTube **완료 목록에는 한 줄만 남습니다.** MeTube가 완료 항목도 URL을 키로 저장해(`PersistentQueue.put`), 나중에 끝난 MP3가 영상 줄을 덮어쓰기 때문입니다. **파일은 둘 다 정상적으로 저장되어 있으니** 저장 폴더에서 확인하세요. 봇과 무관한 MeTube 동작이라 **웹 UI에서 수동으로 영상 → 음원 순서로 받아도 결과는 같습니다.** 키가 yt-dlp로 정규화된 `webpage_url` 이라 URL을 바꿔 별개 항목으로 만드는 우회도 통하지 않습니다.
- 🟡 `둘 다`의 영상 등록은 **MP3 처리가 끝난 뒤**에 이루어집니다. 앞선 다운로드가 2시간 안에 끝나지 않으면 포기하고 ⚠️ 안내를 표시하니, 그때는 링크를 다시 보내 🎬 영상을 선택하세요.
- 🟡 `둘 다`의 대기 중 후속 등록도 **봇을 재시작하면 사라집니다**. 영상이 아직 추가되지 않았다면 링크를 다시 보내세요.
- 🟡 이미 대기열에 있는 링크를 다시 요청하면 `ℹ️ 이미 대기열에 있어요` 가 표시됩니다. MeTube가 같은 URL을 형식과 무관하게 하나로 취급하기 때문이며, 오류가 아닙니다.
- 🟡 URL 추출은 `https?://\S+` 정규식입니다. 링크 뒤에 괄호·구두점이 붙어 있으면 그대로 포함될 수 있습니다.
- 🟡 재생목록 URL을 보내면 MeTube가 **전체를 등록**합니다. 한 편만 받고 싶다면 MeTube 컨테이너의 `YTDL_OPTIONS`에 `{"noplaylist": true}`를 설정하세요 (봇이 아니라 MeTube 쪽 설정입니다).
- 🟡 봇 인스턴스는 하나만 띄우세요. 같은 토큰으로 두 개가 폴링하면 업데이트를 서로 뺏어갑니다.

**코드 수정 시**

- 코드는 이미지 안으로 `COPY`되므로 수정 후 **반드시 재빌드**해야 반영됩니다: `docker-compose up -d --build` (캐시 문제 시 `docker-compose build --no-cache`). Container Manager GUI에서는 프로젝트 빌드 후 **중지 → 시작**까지 해야 합니다.

---

## 문제 해결 / Troubleshooting

| 증상 | 확인할 것 |
|---|---|
| 봇이 아무 반응이 없음 | 컨테이너 로그(`docker logs metube-telegram-bot`)에 `bot started`가 찍혔는지, `BOT_TOKEN`이 맞는지 |
| "허용되지 않은 사용자입니다" | `/id`로 확인한 값이 `ALLOWED_CHAT_IDS`에 있는지 (쉼표 구분, 공백 무관) |
| "요청 실패: ... Connection refused" | `METUBE_URL`이 컨테이너에서 접근 가능한 주소인지. `localhost`는 봇 컨테이너 자신을 가리킵니다 — NAS 내부 IP나 도커 네트워크상의 서비스명을 쓰세요 |
| "요청이 만료되었어요" | 봇이 재시작된 경우입니다. 링크를 다시 보내세요 |
| `둘 다`를 눌렀는데 영상이 안 보임 | 정상입니다. MP3 처리가 끝난 뒤 자동으로 추가되며, 완료되면 메시지가 `✅ 둘 다 요청 완료` 로 바뀝니다 |
| `둘 다`인데 완료 목록에 한 줄만 있음 | MeTube가 완료 항목을 URL 기준으로 저장해 나중 항목이 앞 항목을 덮어씁니다. **파일은 둘 다 저장되어 있습니다** — 저장 폴더에서 확인하세요 |
| MP3 파일이 영상과 다른 폴더에 있음 | MeTube의 `AUDIO_DOWNLOAD_DIR` 설정 때문입니다 (미설정 시 `DOWNLOAD_DIR` 과 동일). MeTube 컨테이너 설정이며 봇과 무관합니다 |
| 내가 보낸 링크에 썸네일 카드가 뜸 | 봇의 응답에는 미리보기가 꺼져 있지만, **내가 보낸 메시지**의 미리보기는 봇이 제어할 수 없습니다. 텔레그램에서 링크 전송 전 미리보기를 지우고 보내세요 |
| "이미 MeTube 대기열에 있어요" | 같은 링크가 이미 처리 중입니다. MeTube는 형식과 무관하게 URL 하나당 한 항목만 받습니다 |
| 요청은 성공인데 파일이 없음 | MeTube 웹 UI에서 해당 항목의 오류 메시지 확인 (지역 제한, 로그인 필요 영상 등) |

### 로컬에서 점검

```bash
pip install "python-telegram-bot==21.*" requests
python -m unittest -v          # 테스트 (35건)
python -m py_compile bot.py    # 문법 검사
```

---

## 테스트 / Tests

`test_bot.py` 35건. **추가 의존성 없이** 표준 라이브러리 `unittest`만 사용하며,
push/PR마다 GitHub Actions에서 실행됩니다.

```bash
python -m unittest -v
python -m unittest -k 둘_다      # 특정 테스트만
```

MeTube와 텔레그램은 가짜 객체로 대체합니다. 특히 `FakeMeTube` 는 실제 MeTube의
**URL 기반 대기열 중복 판정**을 그대로 흉내 내기 때문에, 서버 없이도 `둘 다` 동작을
끝까지 검증할 수 있습니다.

| 대상 | 검증 내용 |
|---|---|
| `metube_add` | 성공 / 중복 / HTTP 오류 / 연결 실패 / 비(非)JSON 응답, 요청 본문 |
| `allowed` | 허용 목록 비었을 때·포함될 때·아닐 때 |
| `on_message` | URL 여러 개 추출, 버튼 3종, `callback_data` 64바이트 제한, URL 없음, 권한 거부 |
| `handle_single` | 성공 / 중복은 ✅ 아님 / 실패 / 응답이 2줄로 간결한지 |
| `handle_both` | MP3 선행 등록, 대기 중엔 영상 미등록, 완료 후 자동 등록, 시한 초과, 첫 요청 실패 시 후속 미예약 |
| `on_button` | 토큰 만료·1회성, 형식별 요청, 권한 거부, 잘못된 `callback_data`, 로딩 표시 해제 |
| `build_app` | 링크 미리보기 비활성, 핸들러 등록 |

> 새 기능이나 버그 수정에는 테스트를 함께 추가합니다. 버그 수정 테스트에는
> 어떤 이슈의 회귀인지 docstring으로 남깁니다
> (예: `"""이슈 #1 회귀: 둘 다를 골랐으면 결국 둘 다 등록되어야 한다."""`).

## 주의 / Disclaimer

개인 소장 용도로만 사용하세요. 콘텐츠 다운로드 시 YouTube 서비스 약관 및 해당
콘텐츠의 저작권을 준수할 책임은 사용자에게 있습니다.

For personal use only. You are responsible for complying with YouTube's Terms of
Service and applicable copyright law.

## License

[MIT](LICENSE)
