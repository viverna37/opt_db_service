"""
Проверка подписи Telegram initData мини-аппа (тот же валидатор, что в
taxi_db_service). Ключ подписи — bot token конкретного тенанта: у каждого
оптовика свой бот, поэтому initData, подписанная ботом одного тенанта, не
пройдёт проверку у другого.

Алгоритм:
1. Разобрать initData как query-string (key=value&key=value...).
2. Убедиться, что каждый ключ встречается один раз и hash есть ровно один раз.
3. Отсортировать оставшиеся пары по ключу, склеить "key=value" через \\n —
   это и есть launch_params.
4. secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
5. signature = hex(HMAC_SHA256(key=secret_key, msg=launch_params))
6. Сравнить signature с hash (constant-time), проверить свежесть auth_date.
"""
import hashlib
import hmac
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """initData отсутствует, повреждена, просрочена или подпись не сходится"""


def validate_webapp_init_data(init_data: str, bot_token: str, max_age_seconds: int) -> dict[str, str]:
    """
    Проверяет initData и возвращает её поля (без hash) — среди них 'user',
    JSON-строка с данными аккаунта (id, first_name, last_name, username...).
    Кидает InitDataError, если что-то не сходится.
    """
    pairs = parse_qsl(init_data, keep_blank_values=True)
    if not pairs:
        raise InitDataError("initData пустая")

    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or keys.count("hash") != 1:
        raise InitDataError("дублирующиеся параметры или hash встречается не один раз")

    data = dict(pairs)
    received_hash = data.pop("hash")

    try:
        auth_date = int(data["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("некорректный auth_date") from exc

    age = time.time() - auth_date
    if age < -60 or age > max_age_seconds:
        raise InitDataError("initData просрочена")

    launch_params = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret_key, launch_params.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, received_hash):
        raise InitDataError("подпись не совпадает")

    return data
