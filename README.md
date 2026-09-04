# Garmin → Google Sheets

Синхронизация Garmin Forerunner 255 с Google Таблицей. Раз в день забирает тренировки и дневные метрики с Garmin Connect и дописывает их на листы **Activities** и **Daily**.

Лист **Сводная** скрипт не трогает — там формулы.

## Что попадает в таблицу

**Activities** — все активности с 1 января 2026, не только бег. Повторно те же тренировки не добавляются (по ID).

**Daily** — шаги, сон, HRV, стресс, Body Battery, VO2 и остальное по дням. Сегодняшний день не пишется: он ещё не закрыт. Вчера всегда обновляется.

Часовой пояс: `Asia/Novosibirsk`.

## Таблица

В таблице должны быть два листа с такими именами:

- `Activities`
- `Daily`

Заголовки скрипт создаёт и дополняет сам.

Таблицу нужно расшарить на email сервис-аккаунта Google (права **Редактор**). ID таблицы — кусок из ссылки:

```
https://docs.google.com/spreadsheets/d/SHEET_ID/edit
```

## GitHub Actions

Workflow `Garmin to Google Sheets Sync` запускается каждый день в **11:00** по Новосибирску (`04:00 UTC`) и вручную из вкладки Actions.

Секреты репозитория (Settings → Secrets and variables → Actions):

| Секрет | Что положить |
|---|---|
| `GARMIN_EMAIL` | почта Garmin Connect |
| `GARMIN_PASSWORD` | пароль Garmin Connect |
| `GOOGLE_CREDENTIALS` | весь JSON ключа сервис-аккаунта |
| `SHEET_ID` | ID таблицы из URL |

Токены Garmin кэшируются между запусками, чтобы не логиниться паролем каждый день (Garmin за это часто отвечает 429).

## Локальный запуск

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Файл `.env` в корне (он уже в `.gitignore`):

```
GARMIN_EMAIL=you@mail.com
GARMIN_PASSWORD=your-password
SHEET_ID=id-из-ссылки-на-таблицу
GOOGLE_CREDENTIALS={"type": "service_account", "project_id": "..."}
```

`GOOGLE_CREDENTIALS` — одна строка, весь JSON ключа.

```
python sync_garmin.py
```

## Настройки

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `SYNC_START_DATE` | `2026-01-01` | с какой даты забирать активности и Daily |
| `SYNC_TIMEZONE` | `Asia/Novosibirsk` | «сегодня» и «вчера» |
| `GARMIN_TOKEN_DIR` | `~/.garth` | где хранить сессию Garmin |

## Google Cloud (если ключа ещё нет)

1. Создать проект в [Google Cloud Console](https://console.cloud.google.com/).
2. Включить Google Sheets API и Google Drive API.
3. IAM → Service Accounts → создать аккаунт → Keys → JSON.
4. Поделиться таблицей с email этого аккаунта, роль Редактор.
