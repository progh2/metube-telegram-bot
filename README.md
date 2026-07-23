# MeTube Telegram Bot

텔레그램으로 유튜브 링크를 보내면 NAS의 [MeTube](https://github.com/alexta69/metube)가 다운로드하도록 해주는 간단한 텔레그램 봇입니다.

A tiny Telegram bot that forwards YouTube (or any yt-dlp-supported) links to your self-hosted [MeTube](https://github.com/alexta69/metube) instance, so your NAS downloads them for you.

## 기능 / Features

- 🎬 영상 최고화질(MP4) / 🎵 최고음질 MP3 / 🎬+🎵 둘 다 — 링크마다 버튼으로 선택
- 재생목록 링크 지원 (MeTube가 전체를 대기열에 등록)
- 폴링 방식이라 포트 포워딩·외부 노출 불필요
- `ALLOWED_CHAT_IDS`로 허용된 사용자만 사용 가능

## 요구 사항 / Requirements

- 실행 중인 MeTube 인스턴스 (예: 시놀로지 Container Manager)
- Docker + docker-compose
- 텔레그램 봇 토큰 ([@BotFather](https://t.me/BotFather)에서 `/newbot`으로 발급)

## 설치 / Setup

```bash
git clone https://github.com/YOUR_ID/metube-telegram-bot.git
cd metube-telegram-bot
cp docker-compose.example.yml docker-compose.yml
# docker-compose.yml 을 열어 BOT_TOKEN, METUBE_URL 입력
docker-compose up -d --build
```

시놀로지 Container Manager를 쓴다면: 이 폴더를 `/volume1/docker/` 아래에 두고
**프로젝트 → 생성 → 폴더 선택 → 기존 docker-compose.yml 사용**으로 올리면 됩니다.

### 사용자 제한 (권장) / Restrict access (recommended)

1. 봇에게 `/id` 를 보내 내 챗 ID 확인
2. `docker-compose.yml` 의 `ALLOWED_CHAT_IDS=` 에 입력 (쉼표로 여러 명 가능)
3. `docker-compose up -d` 로 재적용

비워두면 봇 아이디를 아는 누구나 사용할 수 있으니 꼭 설정하세요.

## 사용법 / Usage

봇에게 링크를 보내고, 뜨는 버튼에서 형식을 선택하면 끝.
진행 상황은 MeTube 웹 UI에서 확인할 수 있습니다.

## 환경 변수 / Environment variables

| 변수 | 설명 | 기본값 |
|---|---|---|
| `BOT_TOKEN` | 텔레그램 봇 토큰 (필수) | - |
| `METUBE_URL` | MeTube 주소 | `http://localhost:8081` |
| `ALLOWED_CHAT_IDS` | 허용할 챗 ID (쉼표 구분, 비우면 전체 허용) | (비어 있음) |

## 주의 / Disclaimer

개인 소장 용도로만 사용하세요. 콘텐츠 다운로드 시 YouTube 서비스 약관 및 해당
콘텐츠의 저작권을 준수할 책임은 사용자에게 있습니다.

For personal use only. You are responsible for complying with YouTube's Terms of
Service and applicable copyright law.

## License

[MIT](LICENSE)
