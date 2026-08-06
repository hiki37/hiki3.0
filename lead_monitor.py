#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Монитор фриланс-лидов -> уведомления в Telegram.

Источники (оба легальные, ни один не логинится на биржу и не отправляет отклики сам):

1. FL.ru  - официальная RSS-лента заказов.
   https://feedback.fl.ru/knowledge-bases/2/articles/17264-podpiska-na-zakazyi-po-rss

2. Kwork  - у Kwork нет публичного API/RSS для третьих лиц. Поэтому источник другой:
   почтовый ящик, куда САМ Kwork присылает уведомления о новых заказах.
   Сначала включи их: на kwork.ru -> Настройки -> Уведомления -> Email, по своим категориям.
   Дальше этот скрипт читает эти письма по IMAP и пересылает их в Telegram.
   Он не логинится на kwork.ru и не имеет отношения к самому сайту вообще.

3. Яндекс Крауд - это не биржа фриланса, а найм в саму Яндекс (сотрудники поддержки,
   продаж, контента, тестирования и т.д. с зарплатой на карту). На их сайте
   crowd.yandex.ru нет категории "разработка" и нет официального RSS/API.
   Зато "Яндекс Крауд" зарегистрирован как отдельный работодатель на hh.ru,
   а у hh.ru есть официальный публичный API без токенов и логина - именно
   через него скрипт и следит за вакансиями этого работодателя.
   Отклик на вакансию - это отклик с резюме на hh.ru, ты делаешь это сам.

Во всех случаях отклик/резюме ты отправляешь сам, вручную, за пару секунд.

ЗАПУСК: этот скрипт больше не крутится в бесконечном цикле сам по себе -
он делает одну проверку и завершается. Расписание (например, раз в 15 минут)
задаёт GitHub Actions (см. .github/workflows/monitor.yml) - это бесплатно
и не требует своего сервера/ПК. Токен и chat_id скрипт берёт из переменных
окружения (секретов), а не хранит в коде - так репозиторий можно спокойно
делать публичным, и лимит бесплатных минут GitHub Actions вообще не действует.
"""

import email
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header

import feedparser
import requests

# ==================== ОБЩИЕ НАСТРОЙКИ ====================

# Токен и chat_id теперь берутся из переменных окружения (GitHub Secrets),
# а не хранятся в самом файле - см. .github/workflows/monitor.yml.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEEN_FILE = "seen_leads.json"

# Ключевые слова для фильтра (без учёта регистра), общие для всех источников.
# Пустой список [] = не фильтровать, брать всё.
KEYWORDS = [
    # сайты, лендинги, вёрстка
    "сайт", "лендинг", "одностраничник", "одностраничн", "landing", "визитка",
    "html", "css", "javascript", "js", "typescript",
    "react", "vue", "next.js", "nuxt", "фронтенд", "frontend", "backend", "фулстек", "fullstack",
    "верстка", "вёрстка", "wordpress", "tilda", "тильда",
    "разработ", "программист", "верстальщик",
    # телеграм-боты и мини-приложения
    "telegram bot", "телеграм бот", "тг бот", "чат-бот", "chatbot", "бот для телеграм",
    "mini app", "миниапп", "мини-апп", "tma", "web app", "веб-апп", "телеграм-приложение",
    # смежное
    "интернет-магазин", "квиз", "парсер", "интеграция api", "автоматизация сайта",
]

# Опциональная более широкая фраза для встроенного текстового поиска hh.ru
# (см. ИСТОЧНИК 4 ниже) - можно оставить как есть, работает вместе с KEYWORDS.
HH_SEARCH_TEXT = (
    "лендинг OR фронтенд OR frontend OR fullstack OR "
    '"телеграм бот" OR "telegram bot" OR "mini app" OR верстальщик'
)

# Черновик отклика от Claude на каждый подходящий лид - платная фича (нужны
# кредиты на console.anthropic.com), поэтому по умолчанию выключено.
USE_AI_DRAFT = False
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ==================== ИСТОЧНИК 1: FL.RU (RSS) ====================

FLRU_ENABLED = True

# category=5 -> "Программирование", subcategory=37 -> "Веб-программирование".
# Другой раздел - смотри низ страницы https://www.fl.ru/projects/, кнопка "RSS".
FLRU_RSS_URL = "https://www.fl.ru/rss/all.xml?category=5&subcategory=37"

# Weblancer - у них когда-то были RSS по категориям, сейчас разработчики
# оставили только один общий поток по всем разделам сразу, поэтому фильтр
# по ключевым словам (тот же список KEYWORDS) тут особенно важен.
WEBLANCER_ENABLED = True
WEBLANCER_RSS_URL = "https://www.weblancer.net/rss/jobs.rss"

# ==================== ИСТОЧНИК 2: KWORK (ПОЧТА) ====================

KWORK_ENABLED = True

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")  # Gmail: imap.gmail.com; Mail.ru: imap.mail.ru
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")  # пароль приложения, не обычный пароль от почты
IMAP_FOLDER = "INBOX"
KWORK_SENDER_MATCH = "kwork"          # подстрока в адресе отправителя письма

# ==================== ИСТОЧНИК 3: ЯНДЕКС КРАУД (через hh.ru) ====================

HH_ENABLED = True

# id работодателя "Яндекс Крауд" на hh.ru (страница: https://hh.ru/employer/9498112).
# Если вдруг захочешь другого работодателя - открой его страницу на hh.ru,
# id будет в адресной строке.
HH_EMPLOYER_ID = "9498112"

# Публичный API hh.ru не требует токена, но просит представиться в User-Agent.
# Можно вписать сюда свою почту - это просто вежливость, не обязательно.
HH_USER_AGENT = "lead-monitor-script (contact: your-email@example.com)"

# ==================== ИСТОЧНИК 4: HH.RU - ШИРОКИЙ ПОИСК ====================

# В отличие от ИСТОЧНИКА 3 (только "Яндекс Крауд"), тут ищем по ключевым
# словам (HH_SEARCH_TEXT выше) сразу у ВСЕХ работодателей на hh.ru, с упором
# на проектную/удалённую занятость - это ближе всего к разовым заказам,
# а не к постоянному найму. Источник расширяет охват без ввода новой биржи.
HH_BROAD_ENABLED = True
HH_BROAD_PARAMS = {
    "text": HH_SEARCH_TEXT,
    "schedule": "remote",       # удалённая работа
    "employment": "project",    # проектная (не постоянный найм)
    "per_page": 50,
    "order_by": "publication_time",
}

# =====================================================


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def matches_keywords(text):
    if not KEYWORDS:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in KEYWORDS)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[telegram] ошибка отправки: {e}")


def generate_draft(title, description):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            "Заказ на фриланс-бирже.\n"
            f"Название: {title}\nОписание: {description}\n\n"
            "Напиши короткий (2-4 предложения) персональный отклик фрилансера "
            "веб-разработчика на этот заказ. По-русски, без канцелярита, "
            "по существу задачи, с уточняющим вопросом или сроком."
        )
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[ai-draft] ошибка генерации: {e}")
        return None


def notify(source, title, description, link):
    msg = f"🆕 {source}\n\n{title}\n\n{description[:300]}"
    if link:
        msg += f"\n\n🔗 {link}"

    if USE_AI_DRAFT and (title or description):
        draft = generate_draft(title, description)
        if draft:
            msg += f"\n\n✍️ Черновик отклика:\n{draft}"

    send_telegram(msg)
    print(f"[+] {source}: {title}")


# ---------------------- FL.RU ----------------------

def check_flru(seen):
    if not FLRU_ENABLED:
        return
    try:
        feed = feedparser.parse(FLRU_RSS_URL)
    except Exception as e:
        print(f"[fl.ru] ошибка загрузки ленты: {e}")
        return

    for entry in feed.entries:
        uid = "flru:" + (entry.get("id") or entry.get("link", ""))
        if uid == "flru:" or uid in seen:
            continue
        seen.add(uid)

        title = strip_html(entry.get("title", "Без названия"))
        description = strip_html(entry.get("summary", ""))
        link = entry.get("link", "")

        if matches_keywords(f"{title} {description}"):
            notify("FL.ru", title, description, link)


# ---------------------- WEBLANCER ----------------------

def check_weblancer(seen):
    if not WEBLANCER_ENABLED:
        return
    try:
        feed = feedparser.parse(WEBLANCER_RSS_URL)
    except Exception as e:
        print(f"[weblancer] ошибка загрузки ленты: {e}")
        return

    for entry in feed.entries:
        uid = "weblancer:" + (entry.get("id") or entry.get("link", ""))
        if uid == "weblancer:" or uid in seen:
            continue
        seen.add(uid)

        title = strip_html(entry.get("title", "Без названия"))
        description = strip_html(entry.get("summary", ""))
        link = entry.get("link", "")

        if matches_keywords(f"{title} {description}"):
            notify("Weblancer", title, description, link)


# ---------------------- KWORK (ПОЧТА) ----------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d):
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    result = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                result += text.decode(enc or "utf-8", errors="ignore")
            except (LookupError, TypeError):
                # некоторые сервера присылают нестандартные имена кодировок
                # (например "unknown-8bit") - в этом случае просто берём utf-8
                result += text.decode("utf-8", errors="ignore")
        else:
            result += text
    return result


def get_email_body(msg):
    if msg.is_multipart():
        plain, html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
        return plain if plain else strip_html(html)
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore")
        except Exception:
            text = ""
        return text if msg.get_content_type() == "text/plain" else strip_html(text)


def check_kwork_mail(seen):
    if not KWORK_ENABLED:
        return
    if not IMAP_USER or not IMAP_PASSWORD:
        print("[kwork-mail] IMAP_USER/IMAP_PASSWORD не заданы - пропускаю")
        return
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=20)
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)

        since = imap_date(datetime.now() - timedelta(days=2))
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            conn.logout()
            return

        for num in data[0].split():
            # BODY.PEEK - читаем письмо, не помечая его прочитанным у тебя в почте
            status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            sender = decode_mime(msg.get("From", ""))

            if KWORK_SENDER_MATCH.lower() not in sender.lower():
                continue

            message_id = msg.get("Message-ID") or f"num:{num.decode()}"
            uid = "kwork:" + message_id
            if uid in seen:
                continue
            seen.add(uid)

            subject = decode_mime(msg.get("Subject", "Письмо от Kwork"))
            body = get_email_body(msg)
            snippet = strip_html(body)[:400]

            if matches_keywords(f"{subject} {snippet}"):
                notify("Kwork (почта)", subject, snippet, "")

        conn.close()
        conn.logout()
    except Exception as e:
        print(f"[kwork-mail] ошибка ({type(e).__name__}): {e}")


# ---------------------- ЯНДЕКС КРАУД (через hh.ru API) ----------------------

def check_hh_crowd(seen):
    if not HH_ENABLED:
        return
    try:
        r = requests.get(
            "https://api.hh.ru/vacancies",
            params={"employer_id": HH_EMPLOYER_ID, "per_page": 50},
            headers={"User-Agent": HH_USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[hh.ru] ошибка запроса: {e}")
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hh:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        snippet = item.get("snippet") or {}
        description = " ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        )
        description = strip_html(description)
        link = item.get("alternate_url", "")

        if matches_keywords(f"{title} {description}"):
            notify("Яндекс Крауд (hh.ru)", title, description, link)


# ---------------------- HH.RU: ШИРОКИЙ ПОИСК ----------------------

def check_hh_broad(seen):
    if not HH_BROAD_ENABLED:
        return
    try:
        r = requests.get(
            "https://api.hh.ru/vacancies",
            params=HH_BROAD_PARAMS,
            headers={"User-Agent": HH_USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[hh.ru-broad] ошибка запроса: {e}")
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hhbroad:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        employer = (item.get("employer") or {}).get("name", "")
        snippet = item.get("snippet") or {}
        description = " ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        )
        description = strip_html(description)
        link = item.get("alternate_url", "")

        if matches_keywords(f"{title} {description}"):
            label = f"hh.ru ({employer})" if employer else "hh.ru"
            notify(label, title, description, link)


# ---------------------- ОДИН ЗАПУСК ----------------------

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ошибка] Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
              "(переменные окружения / GitHub Secrets). Проверка отменена.")
        return

    print("Проверка лидов...")
    seen = load_seen()

    if not seen:
        print("Первый запуск: индексирую текущие заказы/вакансии без уведомлений...")
        if FLRU_ENABLED:
            feed = feedparser.parse(FLRU_RSS_URL)
            for entry in feed.entries:
                uid = "flru:" + (entry.get("id") or entry.get("link", ""))
                if uid != "flru:":
                    seen.add(uid)
        if WEBLANCER_ENABLED:
            feed = feedparser.parse(WEBLANCER_RSS_URL)
            for entry in feed.entries:
                uid = "weblancer:" + (entry.get("id") or entry.get("link", ""))
                if uid != "weblancer:":
                    seen.add(uid)
        if HH_ENABLED:
            try:
                r = requests.get(
                    "https://api.hh.ru/vacancies",
                    params={"employer_id": HH_EMPLOYER_ID, "per_page": 50},
                    headers={"User-Agent": HH_USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                for item in r.json().get("items", []):
                    if item.get("id"):
                        seen.add("hh:" + str(item["id"]))
            except Exception as e:
                print(f"[hh.ru] ошибка индексации: {e}")
        if HH_BROAD_ENABLED:
            try:
                r = requests.get(
                    "https://api.hh.ru/vacancies",
                    params=HH_BROAD_PARAMS,
                    headers={"User-Agent": HH_USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                for item in r.json().get("items", []):
                    if item.get("id"):
                        seen.add("hhbroad:" + str(item["id"]))
            except Exception as e:
                print(f"[hh.ru-broad] ошибка индексации: {e}")
        save_seen(seen)
        print("Проиндексировано. Дальше - только новое: заказы, письма Kwork, вакансии hh.ru.")
        return

    for check_fn in (check_flru, check_kwork_mail, check_hh_crowd, check_weblancer, check_hh_broad):
        print(f"-> {check_fn.__name__}")
        try:
            check_fn(seen)
        except Exception as e:
            print(f"[main] ошибка в {check_fn.__name__}: {e}")

    save_seen(seen)
    print(f"Проверка завершена. Всего в памяти: {len(seen)}.")


if __name__ == "__main__":
    main()
