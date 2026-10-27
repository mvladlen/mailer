"""Одноразовая авторизация Gmail. Запускайте на своём компьютере (нужен браузер), не на сервере.

    pip install google-auth-oauthlib
    python gmail_auth.py client_secret.json

Откроется браузер, вы разрешаете доступ (чтение и отправка), и рядом появится token.json.
Скопируйте его на сервер в GMAIL_TOKEN_PATH с правами 600. Файлы client_secret.json и
token.json — секреты: не коммитьте их и не отправляйте в чат.
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# Те же scopes, что в src/hotelbot/gmail_client.py: при изменении правьте оба места.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Использование: python gmail_auth.py client_secret.json")
    secrets = Path(sys.argv[1])
    if not secrets.exists():
        sys.exit(f"Файл не найден: {secrets}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    out = secrets.with_name("token.json")
    out.write_text(creds.to_json(), encoding="utf-8")
    out.chmod(0o600)
    print(f"Готово: {out}")


if __name__ == "__main__":
    main()
