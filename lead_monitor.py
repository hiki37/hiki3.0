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

   ВАЖНО: письма от Kwork бывают двух видов. Дайджест "Новые проекты на бирже"
   фильтруется по ключевым словам, а личные письма (покупатель написал в личку,
   оформил заказ, прислал предложение) уходят в Telegram ВСЕГДА, без фильтра -
   это уже не лид, а клиент, который ждёт ответа.

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
import html as html_module
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
    "разработ", "программист", "верстальщик", "программ",
    # телеграм-боты и мини-приложения
    "telegram bot", "телеграм бот", "тг бот", "чат-бот", "chatbot", "бот для телеграм",
    "mini app", "миниапп", "мини-апп", "tma", "web app", "веб-апп", "телеграм-приложение",
    # ИИ, боты, агенты, automation - категории, которые сам отметил в Kwork
    "ии-бот", "ai-бот", "ии агент", "ai agent", "ии-агент", "ai-агент",
    "искусственный интеллект", "нейросет", "chatgpt", "gpt", "llm",
    "машинное обучение", "machine learning", "интернет вещей", "iot",
    "скрипт", "автоматизация",
    # мобильная разработка
    "мобильное приложение", "ios", "android", "flutter", "react native",
    # сервера, хостинг, десктоп-программы
    "сервер", "хостинг", "деплой", "deploy", "программа на заказ",
    # разработка игр
    "unity", "unreal", "разработка игр", "геймдев", "gamedev", "игровой движок", "godot",
    # смежное
    "интернет-магазин", "квиз", "парсер", "интеграция api", "автоматизация сайта",
]

# Опциональная более широкая фраза для встроенного текстового поиска hh.ru
# (см. ИСТОЧНИК 4 ниже) - можно оставить как есть, работает вместе с KEYWORDS.
HH_SEARCH_TEXT = (
    "лендинг OR фронтенд OR frontend OR fullstack OR "
    '"телеграм бот" OR "telegram bot" OR "mini app" OR верстальщик OR '
    'unity OR unreal OR "разработка игр" OR геймдев OR gamedev'
)

# Черновик отклика от Claude на каждый подходящий лид - платная фича (нужны
# кредиты на console.anthropic.com), поэтому по умолчанию выключено.
USE_AI_DRAFT = False
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ==================== ИСТОЧНИК 1: FL.RU (RSS) ====================

FLRU_ENABLED = True

# Список категорий FL.ru, которые проверяем (полный список категорий сайта
# смотри внизу страницы https://www.fl.ru/projects/, кнопка "RSS"):
#   5/37 -> Программирование > Веб-программирование
#   2    -> Разработка сайтов (отдельная категория, раньше вообще не была подключена)
#   16   -> Разработка игр
FLRU_RSS_URLS = [
    "https://www.fl.ru/rss/all.xml?category=5&subcategory=37",
    "https://www.fl.ru/rss/all.xml?category=2",
    "https://www.fl.ru/rss/all.xml?category=16",
]

# Weblancer - RSS-лента, которая тут была (jobs.rss), перестала существовать
# (проверено напрямую - 404, не разовый сбой). Похоже, разработчики Weblancer
# отключили RSS совсем. Выключаю источник, чтобы не засорял лог ошибкой
# каждый прогон - если они когда-нибудь вернут RSS, включается одной строкой.
WEBLANCER_ENABLED = False
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
HH_USER_AGENT = "lead-monitor-personal-script/1.0"

# ==================== ИСТОЧНИК 4: HH.RU - ШИРОКИЙ ПОИСК ====================

# В отличие от ИСТОЧНИКА 3 (только "Яндекс Крауд"), тут ищем по ключевым
# словам (HH_SEARCH_TEXT выше) сразу у ВСЕХ работодателей на hh.ru, с упором
# на проектную/удалённую занятость - это ближе всего к разовым заказам,
# а не к постоянному найму. Источник расширяет охват без ввода новой биржи.
HH_BROAD_ENABLED = True
HH_BROAD_PARAMS = {
    "text": HH_SEARCH_TEXT,
    "schedule": "remote",       # удалённая работа - большинству фрилансеров это и нужно
    "per_page": 100,
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


def strip_html(text, keep_newlines=False):
    """HTML -> читаемый текст.

    keep_newlines=True сохраняет переносы строк (нужно для постов из
    Telegram, где перенос - часть смысла), иначе всё схлопывается в одну
    строку (так удобнее матчить по ключевым словам).
    """
    if not text:
        return ""
    # Сначала убираем блоки <style> и <script> целиком вместе с содержимым
    # (именно оттуда вываливается CSS в уведомления от Kwork)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    if keep_newlines:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|tr|li)\s*>", "\n", text, flags=re.IGNORECASE)
    # Теперь убираем оставшиеся HTML-теги
    text = re.sub(r"<[^>]+>", " ", text)
    # &amp; &nbsp; &gt; и прочие сущности -> нормальные символы.
    # Без этого в уведомление уезжает "Разработка и IT &gt; Боты".
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    if keep_newlines:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        # Схлопываем множественные пробелы/переносы
        text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def notify(source, title, description, link, details=None):
    """details - уже собранный текст уведомления. Если он задан, description
    не используется: источник сам решил, что и как показывать (у Kwork,
    например, это цена + рубрика + покупатель отдельными строками)."""
    body = details if details is not None else (description or "")[:300]
    msg = f"🆕 {source}\n\n{title}"
    if body:
        msg += f"\n\n{body}"
    if link:
        msg += f"\n\n🔗 {link}"

    if USE_AI_DRAFT and (title or description):
        draft = generate_draft(title, description)
        if draft:
            msg += f"\n\n✍️ Черновик отклика:\n{draft}"

    send_telegram(msg)
    print(f"[+] {source}: {title}")


USER_AGENT = "lead-monitor-personal-script/1.0"


def fetch_url(url, timeout=15):
    """Обычный GET с таймаутом и User-Agent. Возвращает текст страницы."""
    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text


def fetch_feed(url, timeout=15):
    """
    Скачивает RSS-ленту с настоящим таймаутом и отдаёт её в feedparser.
    Важно: feedparser.parse(url) сам по себе таймаута не имеет и может
    зависнуть на весь запуск, если сервер ленты подвиснет - та же беда,
    что была с IMAP. Поэтому качаем сами через requests(timeout=...),
    а feedparser только разбирает уже скачанные байты.
    """
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "lead-monitor-personal-script/1.0"})
    r.raise_for_status()
    return feedparser.parse(r.content)


# ---------------------- FL.RU ----------------------

def check_flru(seen):
    if not FLRU_ENABLED:
        return
    for url in FLRU_RSS_URLS:
        try:
            feed = fetch_feed(url)
        except Exception as e:
            print(f"[fl.ru] ошибка загрузки ленты ({url}) ({type(e).__name__}): {e}")
            continue

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
        feed = fetch_feed(WEBLANCER_RSS_URL)
    except Exception as e:
        print(f"[weblancer] ошибка загрузки ленты ({type(e).__name__}): {e}")
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
    """Возвращает СЫРОЕ тело письма (HTML как есть, без снятия тегов) -
    это важно для check_kwork_mail, которому нужны настоящие <a href=...>
    ссылки на конкретные заказы. Кто хочет чистый текст - сам вызывает
    strip_html() на результате."""
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
        # HTML предпочтительнее, если есть - там и живут ссылки на заказы;
        # plain-текст оставляем только как резерв, если HTML-версии вообще нет.
        return html if html else plain
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore")
        except Exception:
            text = ""
        return text


# Письма от Kwork бывают двух разных сортов, и путать их нельзя:
#
#  1. Дайджест "Новые проекты на бирже Kwork" - список чужих заказов, которые
#     ещё нужно отфильтровать по ключевым словам (интересно далеко не всё).
#
#  2. Личные письма: покупатель написал тебе в личку, прислал предложение,
#     оформил заказ. Это не "лид", это уже КЛИЕНТ, который ждёт ответа.
#     Такие письма отправляются в Telegram ВСЕГДА, мимо фильтра по ключевым
#     словам - раньше они молча выбрасывались, потому что в тексте письма
#     ("Получены новые сообщения от Vkira7") нет ни одного слова из KEYWORDS.
KWORK_DIRECT_SUBJECT_MARKERS = (
    "новые сообщения",
    "новое сообщение",
    "заказ",
    "предложени",
    "отклик",
    "оплат",
    "сделка",
    "арбитраж",
)

# Куски вёрстки письма, которые не несут смысла и раньше уезжали в Telegram
# вперемешку с названием заказа ("Название Покупатель Цена Заполнить...").
_KWORK_BOILERPLATE = (
    "перейти на kwork.ru",
    "Название Покупатель Цена",
    "Ваши настройки на бирже",
    "Доступно коннектов",
    "Дата пополнения",
    "Любимых рубрик",
    "Уведомления",
    "Что это?",
    "Изменить",
    "Настроить",
    "Отписаться",
    "Ответить на сайте",
    "Если вы не хотите получать письма от нас, вы можете отписаться от рассылки",
    "Будьте всегда на связи и не пропускайте важные события.",
    "Скачайте приложение Kwork.",
)


def clean_kwork_text(text):
    """Убирает из текста письма шапку/подвал/подписи Kwork."""
    for junk in _KWORK_BOILERPLATE:
        text = text.replace(junk, " ")
    # "+3 новых подходящих проекта За последний час на бирже Kwork размещено
    # 18 новых проектов. В ваших любимых рубриках ... доступно 3 новых проекта."
    text = re.sub(r"\+?\d+\s+нов\w+\s+подходящ\w+\s+проект\w*", " ", text)
    text = re.sub(r"За последни\w+[^.]*\.", " ", text)
    text = re.sub(r"В ваших любимых рубриках[^.]*\.", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_kwork_direct_mail(subject):
    low = (subject or "").lower()
    if "новые проекты на бирже" in low:
        return False
    return any(marker in low for marker in KWORK_DIRECT_SUBJECT_MARKERS)


# Ссылка на конкретный заказ внутри дайджеста. Kwork отдаёт её как
# https://kwork.ru/new_offer?project=3216597 - обрати внимание на "=",
# из-за которого старое выражение (ожидавшее "project3216597") не
# срабатывало НИ РАЗУ, и каждое письмо уходило в аварийную ветку одним
# слипшимся куском текста. "=?" оставлен на случай, если Kwork вернёт
# старый формат без знака равенства.
_KWORK_PROJECT_HREF = re.compile(
    r'href="(https?://(?:www\.)?kwork\.ru/new_offer\?project=?(\d+)[^"]*)"',
    re.IGNORECASE,
)


def parse_kwork_digest(body):
    """Разбирает HTML дайджеста Kwork на отдельные проекты.

    Каждый заказ в письме - это строка таблицы <tr> из трёх ячеек:
    название+рубрика, покупатель со статистикой, цена. Возвращает список
    словарей; пустой список означает "вёрстку письма разобрать не удалось".
    """
    projects = []
    for row in re.findall(r"<tr\b.*?</tr>", body, flags=re.DOTALL | re.IGNORECASE):
        href = _KWORK_PROJECT_HREF.search(row)
        if not href:
            continue
        link, project_id = href.group(1), href.group(2)

        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.DOTALL | re.IGNORECASE)
        first_cell = cells[0] if cells else row

        title_match = re.search(
            r'<a[^>]+href="[^"]*new_offer\?project=?\d+[^"]*"[^>]*>(.*?)</a>',
            row, flags=re.DOTALL | re.IGNORECASE,
        )
        title = strip_html(title_match.group(1)) if title_match else ""
        if not title:
            continue

        # Рубрика лежит в отдельном <div> сразу после ссылки на заказ.
        category = ""
        cat_match = re.search(
            r"</a>\s*</div>\s*<div[^>]*>(.*?)</div>", first_cell,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if cat_match:
            category = strip_html(cat_match.group(1))
        else:
            rest = strip_html(first_cell).replace(title, " ").strip()
            category = rest
        # Kwork иногда ставит двойную стрелку: "Скрипты, боты > > ИИ-боты"
        category = re.sub(r"(?:\s*>\s*)+", " > ", category).strip(" >")

        buyer, buyer_stats = "", ""
        if len(cells) > 1:
            user_match = re.search(r"kwork\.ru/user/([^\"?/]+)", cells[1], re.IGNORECASE)
            if user_match:
                buyer = user_match.group(1)
            stats_text = strip_html(cells[1])
            bits = []
            projects_match = re.search(r"(\d+)\s+проект\w*\s+на\s+бирже", stats_text)
            if projects_match:
                bits.append(projects_match.group(0))
            hired_match = re.search(r"(\d+)%\s+нанято", stats_text)
            if hired_match:
                bits.append(hired_match.group(0))
            buyer_stats = ", ".join(bits)

        price = strip_html(cells[2]) if len(cells) > 2 else ""

        projects.append({
            "id": project_id,
            "title": title,
            "category": category,
            "buyer": buyer,
            "buyer_stats": buyer_stats,
            "price": price,
            "link": link,
        })
    return projects


def format_kwork_details(project):
    lines = []
    if project["price"]:
        lines.append("💰 " + project["price"])
    if project["category"]:
        lines.append("📂 " + project["category"])
    if project["buyer"]:
        who = "👤 " + project["buyer"]
        if project["buyer_stats"]:
            who += " (" + project["buyer_stats"] + ")"
        lines.append(who)
    return "\n".join(lines)


def handle_kwork_digest(seen, subject, body):
    projects = parse_kwork_digest(body)

    if not projects:
        # Аварийная ветка: Kwork поменял вёрстку. Лучше грубое уведомление,
        # чем тишина - но хотя бы без шапки, подвала и слова "Цена".
        print("[kwork-mail] ВНИМАНИЕ: не удалось разобрать письмо на отдельные "
              "заказы - шлю письмо целиком. Похоже, Kwork поменял вёрстку.")
        text = clean_kwork_text(strip_html(body))
        if matches_keywords(subject + " " + text[:5000]):
            notify("Kwork (письмо не разобрано)", subject, "", "https://kwork.ru/projects",
                   details=text[:400])
        return

    for project in projects:
        # Дедупликация по номеру заказа, а не по письму: один и тот же заказ
        # приходит в нескольких дайджестах подряд, а уведомить надо один раз.
        uid = "kwork-project:" + project["id"]
        if uid in seen:
            continue
        seen.add(uid)
        if matches_keywords(project["title"] + " " + project["category"]):
            notify("Kwork", project["title"], "", project["link"],
                   details=format_kwork_details(project))


def handle_kwork_direct(seen, subject, body):
    """Личное письмо: покупатель написал/заказал. Шлём всегда, без фильтра."""
    text = clean_kwork_text(strip_html(body))

    author = ""
    author_match = re.search(r"kwork\.ru/inbox/([^\"?/]+)", body, re.IGNORECASE)
    if author_match:
        author = author_match.group(1)
    else:
        author_match = re.search(r"\bот\s+([A-Za-z0-9_.\-]{2,})", text)
        if author_match:
            author = author_match.group(1)

    link_match = re.search(
        r'href="(https?://(?:www\.)?kwork\.ru/(?:inbox|track|new_offer|payer_orders)[^"]*)"',
        body, re.IGNORECASE,
    )
    link = link_match.group(1) if link_match else "https://kwork.ru/inbox"

    title = subject
    if author:
        title = "Покупатель " + author + " ждёт ответа"

    notify("Kwork — ЛИЧНОЕ ⚡", title, "", link, details=text[:500])


def kwork_search(conn, since):
    """Ищем письма от Kwork за нужный период.

    Фильтр по отправителю отдаём самому серверу (FROM) - иначе скрипт качает
    подряд ВСЮ почту за сутки и отбрасывает лишнее уже у себя. Если сервер
    такой поиск не поддерживает, откатываемся на поиск только по дате.
    """
    for criteria in ('(SINCE "%s" FROM "kwork")' % since, '(SINCE "%s")' % since):
        try:
            status, data = conn.search(None, criteria)
        except Exception as e:
            print("[kwork-mail] поиск %s не сработал (%s): %s" % (criteria, type(e).__name__, e))
            continue
        if status == "OK" and data and data[0] is not None:
            return data[0].split()
    return []


def check_kwork_mail(seen):
    if not KWORK_ENABLED:
        return
    if not IMAP_USER or not IMAP_PASSWORD:
        print("[kwork-mail] IMAP_USER/IMAP_PASSWORD не заданы - пропускаю")
        return

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=20)
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)

        # Сутки, а не 3 часа: SINCE в IMAP работает с точностью до ДАТЫ, и при
        # запуске сразу после полуночи трёхчасовое окно теряло вечернюю почту.
        since = imap_date(datetime.now() - timedelta(days=1))

        for num in kwork_search(conn, since):
            # BODY.PEEK - читаем письмо, не помечая его прочитанным у тебя в почте
            status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            sender = decode_mime(msg.get("From", ""))
            if KWORK_SENDER_MATCH.lower() not in sender.lower():
                continue

            # Дедупликация ТОЛЬКО по Message-ID письма. Раньше здесь была ещё
            # проверка "тема + дата", но дата бралась как Date[:16] - а это
            # "Wed, 27 Aug 2026", то есть точность до СУТОК, а не до часа.
            # Тема у всех дайджестов одна и та же, поэтому из десятка писем
            # за день обрабатывалось ровно одно, а остальные молча выкидывались.
            # Именно поэтому бот "перестал присылать сообщения".
            message_id = msg.get("Message-ID") or ("num:" + num.decode())
            uid = "kwork:" + message_id
            if uid in seen:
                continue
            seen.add(uid)

            subject = decode_mime(msg.get("Subject", ""))
            body = get_email_body(msg)

            if is_kwork_direct_mail(subject):
                handle_kwork_direct(seen, subject, body)
            else:
                handle_kwork_digest(seen, subject, body)

    except Exception as e:
        print("[kwork-mail] ошибка (%s): %s" % (type(e).__name__, e))
    finally:
        # Раньше соединение закрывалось только при удачном проходе: любая
        # ошибка в середине оставляла висеть открытый IMAP-сокет.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


# ---------------------- ЯНДЕКС КРАУД (через hh.ru API) ----------------------

def hh_get(params):
    """GET к api.hh.ru с понятной диагностикой при ошибке. Возвращает dict или None."""
    try:
        r = requests.get(
            "https://api.hh.ru/vacancies",
            params=params,
            headers={"User-Agent": HH_USER_AGENT},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[hh.ru] HTTP {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[hh.ru] ошибка запроса ({type(e).__name__}): {e}")
        return None


def check_hh_crowd(seen):
    if not HH_ENABLED:
        return
    data = hh_get({"employer_id": HH_EMPLOYER_ID, "per_page": 50})
    if data is None:
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
    data = hh_get(HH_BROAD_PARAMS)
    if data is None:
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


# ---------------------- ИСТОЧНИК 5: SUPERJOB (API) ----------------------
#
# У SuperJob официальный API, но не полностью открытый как у hh.ru: нужно
# зарегистрировать своё приложение на api.superjob.ru/register/ и получить
# Secret key. Для обычного поиска вакансий (без вывода контактов) хватает
# только этого ключа в заголовке X-Api-App-Id - полноценный OAuth-логин
# пользователя тут НЕ нужен.

SUPERJOB_ENABLED = True
SUPERJOB_API_KEY = os.environ.get("SUPERJOB_API_KEY", "")  # Secret key приложения, см. README
SUPERJOB_SEARCH_KEYWORD = "разработчик"  # простой текстовый поиск, можно поменять - один широкий термин надёжнее, чем фраза из нескольких слов (не факт, что SuperJob ищет по фразе так же, как hh.ru)


def check_superjob(seen):
    if not SUPERJOB_ENABLED:
        return
    if not SUPERJOB_API_KEY:
        print("[superjob] SUPERJOB_API_KEY не задан - пропускаю")
        return
    try:
        r = requests.get(
            "https://api.superjob.ru/2.0/vacancies/",
            params={"keyword": SUPERJOB_SEARCH_KEYWORD, "period": 7, "count": 100, "place_of_work": 2},  # place_of_work=2 = "на дому" - т.е. удалённо, а не в офисе/на производстве
            headers={"X-Api-App-Id": SUPERJOB_API_KEY},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[superjob] HTTP {r.status_code}: {r.text[:300]}")
            return
        data = r.json()
    except Exception as e:
        print(f"[superjob] ошибка запроса ({type(e).__name__}): {e}")
        return

    for item in data.get("objects", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "superjob:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("profession", "Без названия")
        description = strip_html(" ".join(filter(None, [item.get("work"), item.get("candidat")])))
        link = item.get("link", "")

        if matches_keywords(f"{title} {description}"):
            notify("SuperJob", title, description, link)


# ---------------------- ИСТОЧНИК 6: TELEGRAM-КАНАЛЫ ----------------------
#
# У Telegram нет своего RSS для каналов. Раньше тут стоял сторонний мост
# RSSHub (rsshub.app) - публичный бесплатный инстанс, который почти всегда
# отдаёт 429/403 из чужого CI. То есть источник формально был, а работал
# примерно никогда.
#
# Теперь читаем канал напрямую, без посредников: у КАЖДОГО публичного канала
# есть веб-версия https://t.me/s/ИМЯ_КАНАЛА - обычная HTML-страница с
# последними постами. Ни логина, ни токена, ни бота, ни стороннего сервиса.
# Это та же страница, которую видит любой человек без Telegram.
#
# Каналы ниже - просто пример; смело меняй список на свои, формат тот же
# (только имя канала, без "@" и без "t.me/"). Общий фильтр KEYWORDS
# применяется и здесь, так что канал со смешанной тематикой не завалит
# уведомлениями подряд.

TELEGRAM_CHANNELS_ENABLED = True
TME_BASE = "https://t.me/s"
TELEGRAM_CHANNELS = [
    "frilanser_vacansii",
    "distantsiya",
]

# Максимум постов из одного канала за прогон - страница t.me/s отдаёт около
# 20 последних, и если канал внезапно окажется новым (ничего не в seen),
# без этого потолка можно получить сразу двадцать уведомлений подряд.
TELEGRAM_MAX_POSTS_PER_RUN = 8


def parse_tme_page(page, channel):
    """Достаёт посты из HTML-страницы https://t.me/s/КАНАЛ.

    Каждый пост обёрнут в <div class="tgme_widget_message_wrap ...">, внутри
    есть data-post="канал/НОМЕР" и текст в div.tgme_widget_message_text.
    """
    posts = []
    blocks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for block in blocks:
        post_match = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not post_match:
            continue
        post_id = post_match.group(2)

        text_match = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)'
            r'(?:<div class="tgme_widget_message_footer'
            r'|<span class="tgme_widget_message_meta'
            r'|<div class="tgme_widget_message_bubble_end)',
            block, flags=re.DOTALL,
        )
        if not text_match:
            continue
        text = strip_html(text_match.group(1), keep_newlines=True)
        if not text:
            continue

        posts.append({
            "id": post_id,
            "text": text,
            "link": "https://t.me/%s/%s" % (channel, post_id),
        })
    return posts


def check_telegram_channels(seen):
    if not TELEGRAM_CHANNELS_ENABLED:
        return
    for channel in TELEGRAM_CHANNELS:
        try:
            page = fetch_url("%s/%s" % (TME_BASE, channel))
        except Exception as e:
            print("[tg:%s] ошибка загрузки страницы (%s): %s" % (channel, type(e).__name__, e))
            continue

        posts = parse_tme_page(page, channel)
        if not posts:
            print("[tg:%s] постов не найдено - канал закрыт, переименован "
                  "или Telegram поменял вёрстку" % channel)
            continue

        sent = 0
        for post in posts:
            uid = "tg:%s:%s" % (channel, post["id"])
            if uid in seen:
                continue
            seen.add(uid)
            if sent >= TELEGRAM_MAX_POSTS_PER_RUN:
                continue
            if matches_keywords(post["text"]):
                first_line = post["text"].split("\n", 1)[0][:100]
                notify("Telegram: " + channel, first_line, "", post["link"],
                       details=post["text"][:400])
                sent += 1


# ---------------------- ИСТОЧНИК 7: HH.RU - ПРОЕКТНАЯ РАБОТА ----------------------
#
# НОВЫЙ источник. Отличается от ИСТОЧНИКА 4 не ключевыми словами, а типом
# занятости: там ищутся обычные удалённые вакансии (наём в штат), а тут -
# employment=project ("проектная работа") и employment=part ("частичная
# занятость"). Это ровно та прослойка, где сидит не работодатель, а ЗАКАЗЧИК
# с разовой задачей: сделать бота, скрипт, интеграцию, лендинг - то есть
# работа, которую реально закрыть за вечер с Claude Code, а не выйти в офис.
#
# Тот же публичный API hh.ru, что уже используется выше: без токена, без
# логина, без ключей - нужен только вежливый User-Agent.

HH_PROJECT_ENABLED = True
HH_PROJECT_PARAMS = {
    "text": HH_SEARCH_TEXT,
    "employment": ["project", "part"],
    "per_page": 100,
    "order_by": "publication_time",
}


def check_hh_project(seen):
    if not HH_PROJECT_ENABLED:
        return
    data = hh_get(HH_PROJECT_PARAMS)
    if data is None:
        return

    for item in data.get("items", []):
        vac_id = item.get("id")
        if not vac_id:
            continue
        uid = "hhproject:" + str(vac_id)
        if uid in seen:
            continue
        seen.add(uid)

        title = item.get("name", "Без названия")
        employer = (item.get("employer") or {}).get("name", "")
        snippet = item.get("snippet") or {}
        description = strip_html(" ".join(
            filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
        ))
        link = item.get("alternate_url", "")

        salary = item.get("salary") or {}
        details = []
        if salary.get("from") or salary.get("to"):
            money = "💰 "
            if salary.get("from"):
                money += "от %s " % salary["from"]
            if salary.get("to"):
                money += "до %s " % salary["to"]
            money += salary.get("currency") or ""
            details.append(money.strip())
        if employer:
            details.append("👤 " + employer)
        if description:
            details.append(description[:300])

        if matches_keywords(title + " " + description):
            notify("hh.ru — проектная работа", title, "", link,
                   details="\n".join(details))


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
            for url in FLRU_RSS_URLS:
                try:
                    feed = fetch_feed(url)
                    for entry in feed.entries:
                        uid = "flru:" + (entry.get("id") or entry.get("link", ""))
                        if uid != "flru:":
                            seen.add(uid)
                except Exception as e:
                    print(f"[fl.ru] ошибка индексации ({url}) ({type(e).__name__}): {e}")
        if WEBLANCER_ENABLED:
            try:
                feed = fetch_feed(WEBLANCER_RSS_URL)
                for entry in feed.entries:
                    uid = "weblancer:" + (entry.get("id") or entry.get("link", ""))
                    if uid != "weblancer:":
                        seen.add(uid)
            except Exception as e:
                print(f"[weblancer] ошибка индексации ({type(e).__name__}): {e}")
        if HH_ENABLED:
            data = hh_get({"employer_id": HH_EMPLOYER_ID, "per_page": 50})
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hh:" + str(item["id"]))
        if HH_BROAD_ENABLED:
            data = hh_get(HH_BROAD_PARAMS)
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hhbroad:" + str(item["id"]))
        if HH_PROJECT_ENABLED:
            data = hh_get(HH_PROJECT_PARAMS)
            for item in (data or {}).get("items", []):
                if item.get("id"):
                    seen.add("hhproject:" + str(item["id"]))
        if SUPERJOB_ENABLED and SUPERJOB_API_KEY:
            try:
                r = requests.get(
                    "https://api.superjob.ru/2.0/vacancies/",
                    params={"keyword": SUPERJOB_SEARCH_KEYWORD, "period": 7, "count": 100, "place_of_work": 2},  # place_of_work=2 = "на дому" - т.е. удалённо, а не в офисе/на производстве
                    headers={"X-Api-App-Id": SUPERJOB_API_KEY},
                    timeout=15,
                )
                if r.status_code < 400:
                    for item in r.json().get("objects", []):
                        if item.get("id"):
                            seen.add("superjob:" + str(item["id"]))
                else:
                    print(f"[superjob] HTTP {r.status_code}: {r.text[:300]}")
            except Exception as e:
                print(f"[superjob] ошибка индексации ({type(e).__name__}): {e}")
        if TELEGRAM_CHANNELS_ENABLED:
            for channel in TELEGRAM_CHANNELS:
                try:
                    for post in parse_tme_page(fetch_url(f"{TME_BASE}/{channel}"), channel):
                        seen.add(f"tg:{channel}:{post['id']}")
                except Exception as e:
                    print(f"[tg:{channel}] ошибка индексации ({type(e).__name__}): {e}")
        save_seen(seen)
        print("Проиндексировано. Дальше - только новое: заказы, письма Kwork, вакансии hh.ru.")
        return

    for check_fn in (check_kwork_mail, check_flru, check_hh_crowd, check_weblancer,
                      check_hh_broad, check_hh_project, check_superjob,
                      check_telegram_channels):
        print(f"-> {check_fn.__name__}")
        try:
            check_fn(seen)
        except Exception as e:
            print(f"[main] ошибка в {check_fn.__name__}: {e}")

    save_seen(seen)
    print(f"Проверка завершена. Всего в памяти: {len(seen)}.")


if __name__ == "__main__":
    main()
