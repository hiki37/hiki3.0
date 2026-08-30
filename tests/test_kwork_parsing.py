#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты разбора писем Kwork на настоящих письмах из ящика.

Фикстуры рядом - это реальные письма Kwork (дайджест новых проектов и
уведомление о личном сообщении), а не выдуманная вёрстка. Запуск:

    python3 tests/test_kwork_parsing.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lead_monitor as lm

HERE = os.path.dirname(os.path.abspath(__file__))
sent = []
real_send_telegram = lm.send_telegram   # настоящая функция, для теста обрезки
real_build_draft = lm.build_draft
lm.send_telegram = lambda text: sent.append(text)

failures = []


def check(condition, message):
    if condition:
        print("  ok  - " + message)
    else:
        print("  FAIL- " + message)
        failures.append(message)


def read(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


print("1. Дайджест разбирается на отдельные заказы")
digest = read("fixture_kwork_digest.html")
projects = lm.parse_kwork_digest(digest)
check(len(projects) == 2, "найдено 2 заказа (получено %d)" % len(projects))

first = projects[0]
check(first["id"] == "3216597", "id заказа = 3216597 (получено %r)" % first["id"])
check(first["title"] == "Настройка AI bardeen", "название без мусора (%r)" % first["title"])
check(first["price"] == "3 000 Р", "цена вытащена (%r)" % first["price"])
check(first["buyer"] == "avbv", "покупатель вытащен (%r)" % first["buyer"])
check("47 проектов на бирже" in first["buyer_stats"] and "53% нанято" in first["buyer_stats"],
      "статистика покупателя вытащена (%r)" % first["buyer_stats"])
check(first["link"] == "https://kwork.ru/new_offer?project=3216597",
      "ссылка ведёт на конкретный заказ (%r)" % first["link"])
check(">  >" not in first["category"] and "&gt;" not in first["category"],
      "рубрика без двойных стрелок и HTML-сущностей (%r)" % first["category"])
check(first["category"] == "Разработка и IT > Скрипты, боты и mini apps > ИИ-боты",
      "рубрика целиком (%r)" % first["category"])

second = projects[1]
check(second["id"] == "3216742" and second["title"] == "Установить Hermes Agent на готовый VPS Beget",
      "второй заказ разобран (%r)" % second["title"])

print("\n2. Мусор из шапки письма в заказы не попадает")
for project in projects:
    check("Название" not in project["title"] and "Покупатель" not in project["title"]
          and "Цена" not in project["title"],
          "в названии нет шапки таблицы (%r)" % project["title"])
check(not any(p["id"] in ("441", "5") for p in projects),
      "ссылки на faq/профиль не приняты за заказы")

print("\n3. Уведомление собирается читаемым")
sent[:] = []
lm.handle_kwork_digest(set(), "Новые проекты на бирже Kwork", digest)
check(len(sent) == 2, "отправлено 2 уведомления (получено %d)" % len(sent))
if sent:
    msg = sent[0]
    print("  --- как это придёт в Telegram ---")
    for line in msg.splitlines():
        print("  | " + line)
    check("Название Покупатель Цена" not in msg, "в сообщении нет 'Название Покупатель Цена'")
    check("3 000" in msg, "в сообщении есть цена")
    check("kwork.ru/new_offer?project=3216597" in msg, "в сообщении есть ссылка на заказ")

print("\n4. Дедупликация по номеру заказа, а не по письму")
seen = set()
sent[:] = []
lm.handle_kwork_digest(seen, "Новые проекты на бирже Kwork", digest)
first_round = len(sent)
lm.handle_kwork_digest(seen, "Новые проекты на бирже Kwork", digest)
check(len(sent) == first_round, "повторный дайджест с теми же заказами не дублирует уведомления")

print("\n5. Личное сообщение от покупателя доходит ВСЕГДА, мимо ключевых слов")
message_mail = read("fixture_kwork_message.html")
check(lm.is_kwork_direct_mail("Новые сообщения на Kwork.ru"), "письмо опознано как личное")
check(not lm.is_kwork_direct_mail("Новые проекты на бирже Kwork"), "дайджест не опознан как личный")
check(not lm.matches_keywords(lm.clean_kwork_text(lm.strip_html(message_mail))),
      "в самом сообщении покупателя нет ни одного слова из KEYWORDS - "
      "проходить фильтр ему нечем, поэтому фильтр для таких писем и отключён")
check(lm.matches_keywords(lm.strip_html(message_mail)),
      "раньше письмо цеплялось за фильтр только случайно, словом 'сайт' "
      "из подвала 'Ответить на сайте' - и уходило нечитаемым куском")
sent[:] = []
lm.handle_kwork_direct(set(), "Новые сообщения на Kwork.ru", message_mail)
check(len(sent) == 1, "уведомление всё равно отправлено (получено %d)" % len(sent))
if sent:
    msg = sent[0]
    print("  --- как это придёт в Telegram ---")
    for line in msg.splitlines():
        print("  | " + line)
    check("vkira7" in msg.lower(), "в сообщении есть имя покупателя")
    check("kwork.ru/inbox/vkira7" in msg, "в сообщении есть ссылка на переписку")
    check("отписаться от рассылки" not in msg.lower(), "подвал письма вычищен")

print("\n6. strip_html чистит сущности и стили")
check(lm.strip_html("<style>a{color:red}</style>Разработка и IT &gt; Боты &amp; скрипты")
      == "Разработка и IT > Боты & скрипты", "сущности раскодированы, CSS вырезан")

print("\n7. Разбор страницы Telegram t.me/s")
tme = '''
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchannel/451">
<div class="tgme_widget_message_text js-message_text" dir="auto">Нужен телеграм бот<br/>для приёма заявок. Бюджет 15000</div>
<div class="tgme_widget_message_footer compact js-message_footer">footer</div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchannel/452">
<div class="tgme_widget_message_text js-message_text" dir="auto">Продам гараж</div>
<div class="tgme_widget_message_footer compact js-message_footer">footer</div>
</div></div>
'''
posts = lm.parse_tme_page(tme, "testchannel")
check(len(posts) == 2, "найдено 2 поста (получено %d)" % len(posts))
if len(posts) == 2:
    check(posts[0]["link"] == "https://t.me/testchannel/451", "ссылка на пост собрана")
    check("\n" in posts[0]["text"], "переносы строк в посте сохранены")
    check(lm.matches_keywords(posts[0]["text"]), "пост про бота проходит фильтр")
    check(not lm.matches_keywords(posts[1]["text"]), "пост про гараж отсеивается")

print("\n8. Черновики откликов: только для нужных источников")
check(lm.wants_draft("Kwork"), "Kwork - черновик нужен")
check(lm.wants_draft("Kwork — ЛИЧНОЕ ⚡"), "личка Kwork - черновик нужен")
check(lm.wants_draft("Telegram: frilanser_vacansii"), "Telegram - черновик нужен")
check(not lm.wants_draft("FL.ru"), "FL.ru - черновика НЕТ (там отклик платный)")
check(not lm.wants_draft("SuperJob"), "SuperJob - черновика нет")

print("\n9. Черновик попадает в сообщение")
lm.build_draft = lambda source, title, details, link: "Готов сделать бота. Когда нужен результат?"
sent[:] = []
lm.notify("Kwork", "Нужен простой тг-бот для отзывов", "", "https://kwork.ru/new_offer?project=1", details="💰 2 000 Р")
check(len(sent) == 1, "уведомление отправлено")
if sent:
    print("  --- как это придёт в Telegram ---")
    for line in sent[0].splitlines():
        print("  | " + line)
    check("ЧЕРНОВИК ОТКЛИКА" in sent[0], "черновик приложен")
    check("Готов сделать бота" in sent[0], "текст черновика на месте")

sent[:] = []
lm.notify("FL.ru", "Небольшой модуль", "", "https://www.fl.ru/projects/1/", details="💰 15 000 ₽")
check(sent and "ЧЕРНОВИК" not in sent[0], "к заказу с FL.ru черновик не прикладывается")

lm.build_draft = real_build_draft   # вернуть настоящий на остальные проверки

print("\n10. Длинное сообщение обрезается, а не теряется")
captured = []
class FakeResponse:
    status_code = 200
    def raise_for_status(self): pass
lm.requests.post = lambda url, data=None, timeout=None: (captured.append(data["text"]), FakeResponse())[1]
lm.time.sleep = lambda s: None
real_send_telegram("я" * 9000)
check(len(captured) == 1 and len(captured[0]) <= 4100,
      "сообщение обрезано до предела Telegram (получено %d символов)"
      % (len(captured[0]) if captured else -1))
check(captured and captured[0].endswith("[...обрезано]"), "обрезка помечена в тексте")

print("\n11. Бесплатные шаблонные черновики (без API-ключа)")
lm.ANTHROPIC_API_KEY = ""
lm.DRAFT_ENGINE = "auto"
del lm.generate_draft   # если движок всё-таки полезет в API - будет видно
lm.generate_draft = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("без ключа в платный API ходить нельзя"))

# заголовки настоящих заказов из прогонов бота
cases = [
    ("Нужен простой тг-бот для отзывов", "бот"),
    ("БОТ Телеграмм на Python для группы", "бот"),
    ("Спарсить 400к страниц с сайта", "парсер"),
    ("Автоматизация смет с помощью ИИ, Access, Excel, и тд", "скрипт"),
    ("Нам нужен специалист по синхронизации 1с и сайта.", "интеграц"),
    ("Создать сервер VPS с готовым решением Docker", "сервер"),
    ("Срочно доработать сайт на вордпресс", "сайт"),
]
for title, expect in cases:
    draft = lm.build_draft("Kwork", title, "", "")
    ok = draft and expect in draft.lower() and draft.rstrip().endswith("?")
    check(ok, "%-45s -> тип опознан, вопрос в конце" % title[:45])

print("\n  --- пример готового черновика ---")
for line in lm.build_draft("Kwork", "Нужен простой тг-бот для отзывов", "", "").split(". "):
    print("  | " + line.strip() + ("" if line.strip().endswith("?") else "."))

print("\n12. Незнакомая задача - сдержанная заготовка, без обещаний")
draft = lm.build_draft("Kwork", "Помощник по проекту", "", "")
check(draft and draft.rstrip().endswith("?"), "вопрос на месте")
check("готов взяться" in draft.lower(), "общая заготовка, без выдуманной конкретики")

print("\n13. Личное сообщение получает ответ, а не отклик на заказ")
draft = lm.build_draft("Kwork — ЛИЧНОЕ ⚡", "Покупатель vkira7 ждёт ответа", "", "")
check("прошу прощения" in draft.lower(), "это извинение за задержку, а не отклик")
check("покажу рабочий результат" not in draft.lower(), "нет текста отклика на заказ")

print("\n14. Платный движок падает - лид всё равно уходит с заготовкой")
lm.ANTHROPIC_API_KEY = "sk-test"
lm.generate_draft = lambda *a, **k: None    # как будто кончились кредиты
draft = lm.build_draft("Kwork", "Нужен простой тг-бот для отзывов", "", "")
check(draft and "бот" in draft.lower(), "откат на бесплатный шаблон сработал")
lm.ANTHROPIC_API_KEY = ""

print("\n15. Англоязычные источники: свой фильтр и свой черновик")
check(lm.is_english_source("Hacker News — Seeking freelancer"), "HN опознан как англоязычный")
check(lm.is_english_source("RemoteOK"), "RemoteOK опознан")
check(not lm.is_english_source("Kwork"), "Kwork - не англоязычный")

# русский список на английском тексте почти не срабатывает - ради этого и заведён KEYWORDS_EN
us_task = "SEEKING FREELANCER | Remote | We need a Python scraper to pull product data"
check(lm.matches_keywords(us_task, lm.KEYWORDS_EN), "английский фильтр ловит задачу")

draft = lm.build_draft("Hacker News — Seeking freelancer", us_task, "", "")
print("  --- черновик для американского заказчика ---")
for line in draft.split(". "):
    print("  | " + line.strip() + ("" if line.strip().endswith("?") else "."))
check(draft.startswith("Hi!"), "черновик по-английски")
check("scraper" in draft.lower(), "тип задачи опознан (scraper)")
check(draft.rstrip().endswith("?"), "вопрос в конце")
check("Здравствуйте" not in draft, "русского текста в англоязычном черновике нет")

check(lm.build_draft("Kwork", "Нужен парсер сайта", "", "").startswith("Здравствуйте"),
      "русский источник по-прежнему получает русский черновик")

print("\n16. HN: берём только тех, кто ИЩЕТ исполнителя")
hits = [
    {"objectID": "1", "author": "acme",
     "comment_text": "SEEKING FREELANCER<p>Remote | We need a Python scraper. Email me at a@b.co"},
    {"objectID": "2", "author": "dev42",
     "comment_text": "SEEKING WORK<p>Python developer, 10 years, available for contract"},
]
sent[:] = []
lm.hn_algolia_get = lambda path, params: (
    {"hits": [{"objectID": "999", "title": "Ask HN: Freelancer? Seeking freelancer? (August 2026)",
               "created_at_i": 1}]} if path == "/search" else {"hits": hits})
lm.check_hn_freelance(set())
check(len(sent) == 1, "отправлен только заказчик, не соискатель (получено %d)" % len(sent))
if sent:
    check("news.ycombinator.com/item?id=1" in sent[0], "ссылка на комментарий заказчика")
    check("acme" in sent[0], "автор указан")

print("\n17. Англоязычный фильтр не пропускает мусор (регрессия из боевого прогона)")
# Ровно эти вакансии RemoteOK прислал в Telegram, пока фильтр был дырявым
junk = ["Kitchen Porter", "Mail Sorter", "Gardener", "Ramp Attendant",
        "Removalist Offsider", "Cook FT Northpoint", "Retail Store Associate",
        "Bell Captain", "Quantity Surveyor", "Post Office Manager",
        "Store Manager", "Vehicle Progressor", "Joiner", "Lead Estimator"]
for title in junk:
    check(not lm.matches_keywords(title, lm.KEYWORDS_EN), "отсеян: %s" % title)

good = ["Senior Backend Engineer Build AI Agents", "Python Developer",
        "Full Stack Engineer", "Need a web scraper for product data",
        "Telegram bot developer", "Wordpress landing page fixes",
        "LLM / prompt engineer for RAG app"]
for title in good:
    check(lm.matches_keywords(title, lm.KEYWORDS_EN), "пропущен: %s" % title)

print("\n18. Короткие куски не ловятся внутри других слов")
traps = [("Corporate capital markets analyst", "api в capital"),
         ("Physical therapist assistant", "api в therapist"),
         ("Employment laws specialist", "aws в laws"),
         ("Warehouse storage operative", "rag в storage"),
         ("Excellent customer service rep", "excel в excellent")]
for text, why in traps:
    check(not lm.matches_keywords(text, lm.KEYWORDS_EN), "не поймался %s" % why)

print("\n19. Один источник не съедает весь лимит за прогон")
lm._notify_state["sent"] = 0
lm._notify_state["skipped"] = 0
lm._notify_state["by_source"] = {}
sent[:] = []
for i in range(30):
    lm.notify("RemoteOK", "Job %d" % i, "", "")
for i in range(3):
    lm.notify("Hacker News — Seeking freelancer", "Client %d" % i, "", "")
from_remoteok = sum(1 for m in sent if "RemoteOK" in m)
from_hn = sum(1 for m in sent if "Hacker News" in m)
check(from_remoteok <= lm.MAX_NOTIFICATIONS_PER_SOURCE,
      "RemoteOK ограничен %d (отправлено %d)" % (lm.MAX_NOTIFICATIONS_PER_SOURCE, from_remoteok))
check(from_hn == 3, "лиды Hacker News дошли, несмотря на поток RemoteOK (дошло %d)" % from_hn)

print("\n" + "=" * 60)
if failures:
    print("ПРОВАЛЕНО проверок: %d" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("Все проверки пройдены.")
