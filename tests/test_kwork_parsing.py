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
lm.generate_draft = lambda source, title, details, link: "Готов сделать бота. Когда нужен результат?"
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

print("\n" + "=" * 60)
if failures:
    print("ПРОВАЛЕНО проверок: %d" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("Все проверки пройдены.")
