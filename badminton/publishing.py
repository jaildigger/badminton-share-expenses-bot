"""Durable group summaries, separate from private organizer controls."""
from .calculator import money
from .telegram import TelegramError


def summary(store, owner, tid):
    t = store.training(owner, tid)
    header = "Бадминтон · {} · {} · #{}".format(t["date"], t["location"], tid)
    if t["status"] == "draft":
        return header + "\n\nРасчёт пересматривается. Прежние суммы неактуальны. Дождитесь нового итога."
    result = store.calculation(owner, tid)
    members = store.participants(tid)
    shuttles = sum(p["shuttle_count"] * p["shuttle_price"] for p in members)
    lines = [header, "Участников: {}".format(len(members)),
             "Корт: {} · Воланы: {}".format(money(t["court_cost"]), money(shuttles)),
             "Всего: {}".format(money(result.total)), "", "Стоимость участия:"]
    lines += ["{}: доля {} · баланс {}".format(p["name"], money(result.shares[p["person_id"]]), money(result.balances[p["person_id"]])) for p in members]
    lines += ['Баланс: плюс — доплатить, минус — получить.']
    transfers = store.transfers(tid)
    payers = store.all("SELECT c.person_id,p.name FROM court_payments c JOIN people p ON p.id=c.person_id WHERE c.training_id=? AND c.amount>0", (tid,))
    lines.append("")
    if len(payers) == 1 and all(
            tr["sender"] == payers[0]["person_id"] or tr["recipient"] == payers[0]["person_id"]
            for tr in transfers):
        payer = payers[0]
        lines.append("Все переводят плательщику корта: {}.".format(payer["name"]))
        suppliers = {p["person_id"] for p in members
                     if p["shuttle_count"] > 0 and result.balances[p["person_id"]] < 0}
        refunds = ["{} {} thb".format(tr["recipient_name"],
                   format((tr["amount"] - tr["paid"]) / 100, ".2f").rstrip("0").rstrip("."))
                   for tr in transfers if tr["sender"] == payer["person_id"]
                   and tr["recipient"] in suppliers and tr["amount"] > tr["paid"]]
        if refunds:
            lines.append("{} должен вернуть: {}.".format(payer["name"], ", ".join(refunds)))
    else:
        # Free courts and historical direct settlements have no single collector.
        for tr in transfers:
            remaining = tr["amount"] - tr["paid"]
            if remaining:
                lines.append("{} → {}: {}.".format(tr["sender_name"], tr["recipient_name"], money(remaining)))
        if not any(tr["amount"] > tr["paid"] for tr in transfers):
            lines.append("Переводы не нужны.")
    lines += ["", "Тренировка закрыта." if t["status"] == "closed" else "Отметки оплат обновляет организатор."]
    return "\n".join(lines)


def queue_updates(store, owner, force_tid=None, notify_uid=None):
    for pub in store.all("SELECT p.* FROM publications p JOIN trainings t ON t.id=p.training_id WHERE t.owner=?", (owner,)):
        tid = pub["training_id"]
        text = summary(store, owner, tid)
        if text != pub["desired"] or tid == force_tid:
            store.execute("UPDATE publications SET desired=? WHERE training_id=?", (text, tid))
            if notify_uid is not None:
                store.execute("UPDATE publications SET notify_uid=? WHERE training_id=?", (notify_uid, tid))
            store.enqueue("publishTraining", {"training_id": tid})


def chunks(text):
    # UTF-16 is Telegram's length unit; leave room for the part label.
    parts, current = [], ""
    for line in text.splitlines():
        if len((current + line + "\n").encode("utf-16-le")) // 2 > 3000:
            parts.append(current.rstrip())
            current = ""
        current += line + "\n"
    parts.append(current.rstrip())
    return [part + ("\n\nЧасть {} из {}".format(i+1, len(parts)) if len(parts) > 1 else "") for i, part in enumerate(parts)]


def deliver(store, api, tid):
    pub = store.one("SELECT p.*,t.owner FROM publications p JOIN trainings t ON t.id=p.training_id WHERE p.training_id=?", (tid,))
    if not pub:
        return
    texts = chunks(pub["desired"])
    saved = {r["part"]: r for r in store.all("SELECT * FROM publication_messages WHERE training_id=?", (tid,))}
    first_delivery = not saved
    # Old extra parts are explicitly superseded when the report shrinks.
    for part in range(max(len(texts), len(saved))):
        text = texts[part] if part < len(texts) else "Расчёт #{} обновлён. Актуальные данные — в первой части сообщения.".format(tid)
        old = saved.get(part)
        if old and old["text"] == text:
            continue
        payload = {"chat_id": pub["chat_id"], "text": text}
        message_id = old["message_id"] if old else None
        if message_id:
            try:
                api.call("editMessageText", dict(payload, message_id=message_id))
            except TelegramError as error:
                if error.reason == "message_missing":
                    message_id = None
                elif error.reason != "not_modified":
                    raise
        if message_id is None:
            if pub["thread_id"]:
                payload["message_thread_id"] = pub["thread_id"]
            message_id = api.call("sendMessage", payload)["message_id"]
        with store.db:
            store.execute("INSERT OR REPLACE INTO publication_messages VALUES (?,?,?,?)", (tid, part, message_id, text))
    if first_delivery:
        with store.db:
            store.enqueue("sendMessage", {"chat_id": pub["notify_uid"] or pub["owner"], "text": "Итог тренировки #{} опубликован в «{}». Отметки оплат будут обновляться в этом сообщении.".format(tid, pub["title"])})


def notify_failure(store, tid):
    pub = store.one("SELECT p.title,p.notify_uid,t.owner FROM publications p JOIN trainings t ON t.id=p.training_id WHERE p.training_id=?", (tid,))
    if pub:
        store.enqueue("sendMessage", {"chat_id": pub["notify_uid"] or pub["owner"], "text": "Не удалось опубликовать или обновить итог #{} в «{}». Проверьте, что бот состоит в группе и может отправлять сообщения, затем нажмите «Обновить итог в группе».".format(tid, pub["title"])})
