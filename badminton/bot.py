"""Russian conversation UI. All Telegram output goes through the durable outbox."""
import json
from datetime import datetime, timedelta, timezone

from .calculator import ValidationError, hours, hours_input, money, money_input, quantity_input
from .publishing import queue_updates


PAGE_SIZE = 8


class Bot:
    def __init__(self, store, admin_ids, membership=None):
        self.s = store
        self.admin_ids = set(admin_ids)
        self.username = "badminton_share_bot"
        self.membership = membership

    def scope(self, uid):
        return 0 if self.s.setting("shared_workspace") == "1" else uid

    def access_error(self, uid):
        if uid in self.admin_ids:
            return None
        group = self.s.one("SELECT * FROM group_bindings WHERE owner=0") if self.scope(uid) == 0 else None
        if not group or not self.membership:
            return "Доступ только для участников подключённой группы. Попросите владельца бота подключить чат командой /bind."
        try:
            if self.membership(group["chat_id"], uid):
                return None
        except ValidationError as error:
            return str(error)
        return "Доступ только для участников подключённой группы. Вступите в чат и отправьте /start снова."

    def check_revision(self, state):
        if self.s.setting("shared_workspace") == "1" and state.get("_revision") != self.s.revision():
            raise ValidationError("Общие данные уже изменил другой участник. Откройте /history или /start и повторите действие с актуальными данными.")

    def send(self, uid, text, buttons=(), state=None):
        generation, _ = self.s.session(uid)
        generation += 1
        state = dict(state or {}, _revision=self.s.revision())
        self.s.execute("UPDATE sessions SET generation=?,state=? WHERE owner=?",
                       (generation, json.dumps(state), uid))
        # Keep messages below Telegram's 4096-character limit. Keyboard belongs to last part.
        chunks = []
        current = ""
        for line in text.splitlines():
            if len(current) + len(line) + 1 > 3500:
                chunks.append(current)
                current = ""
            current += line + "\n"
        chunks.append(current.rstrip())
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": uid, "text": chunk}
            if i == len(chunks) - 1 and buttons:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": label, "callback_data": "{}|{}".format(generation, action)}]
                    for label, action in buttons]}
            self.s.enqueue("sendMessage", payload)

    def prompt(self, uid, text, kind, tid=0, **extra):
        state = dict(kind=kind, tid=tid, **extra)
        self.send(uid, text + "\n\n/cancel — отменить ввод.",
                  [("Отмена", "view:{}".format(tid) if tid else "home")], state)

    def home(self, uid):
        self.send(uid, "Бадминтон · расчёт тренировок\n\nСоздайте тренировку или откройте историю.\n"
                  "Время по умолчанию — 2 часа, воланов — 0. Валюта — тайский бат.\n"
                  "История тренировок и справочники общие для участников группы.",
                  [("Новая тренировка", "new"), ("Тренировки и история", "history:0"),
                   ("Добавить участника в справочник", "register"),
                   ("Добавить локацию", "locregister"), ("Группа для публикаций", "group")])

    def group_message(self, uid, message):
        if uid not in self.admin_ids or message.get("from", {}).get("is_bot"):
            return
        command = message.get("text", "").strip().split()
        if not command:
            return
        name, _, target = command[0].partition("@")
        if name != "/bind" or (target and target.lower() != self.username.lower()):
            return
        chat = message["chat"]
        current = self.s.one("SELECT * FROM group_bindings WHERE owner=?", (self.scope(uid),))
        if self.scope(uid) == 0 and current and current["chat_id"] != chat["id"]:
            self.s.enqueue("sendMessage", {"chat_id": uid, "text": "Уже подключена общая группа «{}». Привязка другого чата отключена, чтобы не открыть ему историю этой группы.".format(current["title"])})
            return
        self.s.execute("INSERT OR REPLACE INTO group_bindings VALUES (?,?,?,?)",
                       (self.scope(uid), chat["id"], chat.get("title", "Группа"), message.get("message_thread_id")))
        self.s.enqueue("sendMessage", {"chat_id": uid, "text": "Группа «{}» подключена. В личном чате откройте зафиксированный расчёт и нажмите «Опубликовать итог в группе».".format(chat.get("title", "Группа"))})

    def group_help(self, uid):
        group = self.s.one("SELECT * FROM group_bindings WHERE owner=?", (self.scope(uid),))
        text = "Подключена группа: {}.\n\n".format(group["title"]) if group else "Группа пока не подключена.\n\n"
        text += "Владелец бота добавляет его в администраторы группы и отправляет там /bind@{}. После этого любой участник группы может открыть личный чат с ботом и отправить /start.\n\nИстория и справочники общие. Ввод данных и отметки оплат остаются в личном чате.".format(self.username)
        self.send(uid, text, [("Главное меню", "home")])

    def process(self, update):
        """Commit input, state, financial writes, responses and polling offset together."""
        update_id = update["update_id"]
        if update_id < int(self.s.setting("offset", "0")):
            return
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        user = callback.get("from", {}) if callback else message.get("from", {})
        uid = user.get("id")
        # Check Telegram before opening a write transaction; transient failures retry the update.
        denial = self.access_error(uid) if uid and message.get("chat", {}).get("type") == "private" else None
        with self.s.db:
            if update_id < int(self.s.setting("offset", "0")):
                return
            callback = update.get("callback_query")
            message = callback.get("message", {}) if callback else update.get("message", {})
            user = callback.get("from", {}) if callback else message.get("from", {})
            uid = user.get("id")
            if callback:
                self.s.enqueue("answerCallbackQuery", {"callback_query_id": callback["id"]})
            if uid and not callback and message.get("chat", {}).get("type") in ("group", "supergroup"):
                self.group_message(uid, message)
                self.s.set_setting("offset", update_id + 1)
                return
            if not uid or message.get("chat", {}).get("type") != "private":
                self.s.set_setting("offset", update_id + 1)
                return
            if denial:
                self.s.enqueue("sendMessage", {"chat_id": uid, "text": denial})
                self.s.set_setting("offset", update_id + 1)
                return
            generation, state = self.s.session(uid)
            self.s.execute("SAVEPOINT action")
            try:
                if callback:
                    raw = callback.get("data", "")
                    token, separator, action = raw.partition("|")
                    if not separator or token != str(generation):
                        self.s.enqueue("sendMessage", {"chat_id": uid, "text": "Это старое меню. Используйте последнее сообщение бота или /start."})
                    else:
                        if action.split(":")[0] in {"select", "remove", "lock", "payfull", "undo", "unlock", "close", "reopen", "publishconfirm", "loc"}:
                            self.check_revision(state)
                        self.action(uid, action)
                elif "text" in message:
                    if not message["text"].strip().startswith("/") and state.get("kind") not in (None, "location", "newdate"):
                        self.check_revision(state)
                    self.text(uid, message["text"].strip(), state)
                else:
                    self.s.enqueue("sendMessage", {"chat_id": uid, "text": "Используйте кнопки или отправьте текст."})
                queue_updates(self.s, self.scope(uid), notify_uid=uid)
            except ValidationError as error:
                self.s.execute("ROLLBACK TO action")
                # Preserve the active prompt and its buttons on validation failures.
                self.s.enqueue("sendMessage", {"chat_id": uid, "text": str(error) + "\nИсправьте ввод или используйте /cancel."})
            self.s.execute("RELEASE action")
            self.s.set_setting("offset", update_id + 1)

    def history(self, uid, page):
        records = self.s.all("SELECT t.*,l.name location FROM trainings t JOIN locations l ON l.id=t.location_id WHERE t.owner=? ORDER BY t.date DESC,t.id DESC", (self.scope(uid),))
        buttons = [("{} · {} · {}".format(t["date"], t["location"], {"draft": "черновик", "settling": "оплаты", "closed": "закрыта"}[t["status"]]), "view:{}".format(t["id"])) for t in records[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
        buttons += self.pages("history", page, len(records))
        buttons += [("Главное меню", "home")]
        self.send(uid, "Тренировки" if records else "Пока нет тренировок.", buttons)

    @staticmethod
    def pages(prefix, page, count):
        buttons = []
        if page:
            buttons.append(("Предыдущие", "{}:{}".format(prefix, page-1)))
        if (page+1)*PAGE_SIZE < count:
            buttons.append(("Следующие", "{}:{}".format(prefix, page+1)))
        return buttons

    def locations(self, uid, date, tid=0, page=0):
        records = self.s.all("SELECT * FROM locations WHERE owner=? AND active=1 ORDER BY id", (self.scope(uid),))
        buttons = [(r["name"], "loc:{}".format(r["id"])) for r in records[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
        buttons += self.pages("locpage", page, len(records))
        buttons += [("Добавить новую локацию", "locadd"), ("Отмена", "view:{}".format(tid) if tid else "home")]
        self.send(uid, "Выберите локацию тренировки на {}.".format(date), buttons,
                  {"kind": "location", "date": date, "tid": tid})

    def view(self, uid, tid):
        t = self.s.training(self.scope(uid), tid)
        members = self.s.participants(tid)
        payments = self.s.all("SELECT c.*,p.name FROM court_payments c JOIN people p ON p.id=c.person_id WHERE training_id=? AND amount>0 ORDER BY person_id", (tid,))
        shuttle_total = sum(p["shuttle_count"]*p["shuttle_price"] for p in members)
        lines = ["Тренировка #{} · {}".format(tid, t["date"]), t["location"],
                 "Корт: {} · Воланы: {}".format(money(t["court_cost"]), money(shuttle_total)),
                 "Общие расходы: {}".format(money(t["court_cost"]+shuttle_total)), "", "Участники: {}".format(len(members))]
        for p in members:
            lines.append("{}{} — {} ч · воланы: {} × {} = {}".format(
                p["name"], " (гость)" if not p["saved"] else "", hours(p["minutes"]),
                p["shuttle_count"], money(p["shuttle_price"]), money(p["shuttle_count"]*p["shuttle_price"])))
        lines += ["", "Корт оплатили:"]
        lines += ["{} — {}".format(p["name"], money(p["amount"])) for p in payments] or ["Пока никто не указан."]
        missing = t["court_cost"] - sum(p["amount"] for p in payments)
        if missing:
            lines.append("Не распределено: {}".format(money(missing)))
        if t["status"] == "draft":
            buttons = [("Участники / часы / воланы", "members:{}:0".format(tid)),
                       ("Кто оплатил корт", "payers:{}:0".format(tid)),
                       ("Изменить стоимость корта", "cost:{}".format(tid)),
                       ("Изменить дату", "date:{}".format(tid)),
                       ("Изменить локацию", "location:{}".format(tid)),
                       ("Рассчитать", "preview:{}".format(tid))]
        else:
            lines += ["", "Статус: " + ("закрыта" if t["status"] == "closed" else "расчёт зафиксирован")]
            buttons = [("Расчёт и переводы", "settlements:{}:0".format(tid))]
        buttons.append(("История тренировок", "history:0"))
        self.send(uid, "\n".join(lines), buttons)

    def members(self, uid, tid, page):
        self.s.training(self.scope(uid), tid, draft=True)
        members = self.s.participants(tid)
        buttons = [(p["name"] + " · " + hours(p["minutes"]) + " ч", "person:{}:{}".format(tid, p["person_id"])) for p in members[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
        buttons += self.pages("members:{}".format(tid), page, len(members))
        buttons += [("Выбрать из сохранённых", "choose:{}:0".format(tid)),
                    ("Добавить нового участника", "add:{}:saved".format(tid)),
                    ("Добавить гостя", "add:{}:guest".format(tid)),
                    ("К тренировке", "view:{}".format(tid))]
        self.send(uid, "Участники: {}. Нажмите имя, чтобы изменить часы и воланы.".format(len(members)), buttons)

    def choose(self, uid, tid, page, payer=False):
        self.s.training(self.scope(uid), tid, draft=True)
        if payer:
            records = self.s.all("SELECT * FROM people WHERE owner=? AND (saved=1 OR id IN (SELECT person_id FROM participants WHERE training_id=?) OR id IN (SELECT person_id FROM court_payments WHERE training_id=?)) ORDER BY id", (self.scope(uid), tid, tid))
        else:
            records = self.s.all("SELECT * FROM people WHERE owner=? AND saved=1 AND id NOT IN (SELECT person_id FROM participants WHERE training_id=?) ORDER BY id", (self.scope(uid), tid))
        action = "courtperson" if payer else "select"
        prefix = "payers" if payer else "choose"
        buttons = [(p["name"], "{}:{}:{}".format(action, tid, p["id"])) for p in records[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
        buttons += self.pages("{}:{}".format(prefix, tid), page, len(records))
        if payer:
            buttons.append(("Добавить нового плательщика", "payeradd:{}".format(tid)))
        buttons.append(("К тренировке", "view:{}".format(tid)))
        self.send(uid, ("Кто оплатил корт? Выберите человека и введите его общую сумму.\n"
                        "Можно указать нескольких. Для исправления выберите имя снова; 0 удаляет его оплату.")
                  if payer else "Выберите участника. После добавления можно выбрать следующего.", buttons)

    def person_view(self, uid, tid, pid):
        self.s.training(self.scope(uid), tid, draft=True)
        p = self.s.one("SELECT m.*,p.name FROM participants m JOIN people p ON p.id=m.person_id WHERE training_id=? AND person_id=?", (tid, pid))
        if not p:
            raise ValidationError("Участник не входит в тренировку.")
        self.send(uid, "{}\nВремя: {} ч\nВоланы: {} × {} = {}\n\nВоланы указываются у человека, которому нужно возместить их стоимость.".format(
            p["name"], hours(p["minutes"]), p["shuttle_count"], money(p["shuttle_price"]), money(p["shuttle_count"]*p["shuttle_price"])),
            [("Изменить время", "hours:{}:{}".format(tid, pid)),
             ("Количество воланов", "count:{}:{}".format(tid, pid)),
             ("Цена одного волана", "price:{}:{}".format(tid, pid)),
             ("Убрать из тренировки", "remove:{}:{}".format(tid, pid)),
             ("К участникам", "members:{}:0".format(tid))])

    def report(self, uid, tid):
        t = self.s.training(self.scope(uid), tid)
        result = self.s.calculation(self.scope(uid), tid)
        names = {p["id"]: p["name"] for p in self.s.all("SELECT * FROM people WHERE owner=?", (self.scope(uid),))}
        lines = ["Расчёт #{} · {} · {}".format(tid, t["date"], t["location"]),
                 "Всего: {}. Распределено по времени участия.".format(money(result.total)),
                 "Округление до 0,01 ฿ с распределением остатка.", ""]
        for pid, balance in result.balances.items():
            outcome = "доплатить " + money(balance) if balance > 0 else "получить " + money(-balance) if balance < 0 else "расчёт закрыт"
            lines.append("{}: доля {} · {}".format(names[pid], money(result.shares.get(pid, 0)), outcome))
        return lines, result, names

    def preview(self, uid, tid):
        self.s.training(self.scope(uid), tid, draft=True)
        lines, result, names = self.report(uid, tid)
        lines += ["", "Предлагаемые переводы:"]
        lines += ["{} → {}: {}".format(names[tr.sender], names[tr.recipient], money(tr.amount)) for tr in result.transfers] or ["Никому ничего переводить не нужно."]
        self.send(uid, "\n".join(lines), [("Зафиксировать и отмечать оплаты", "lock:{}".format(tid)), ("Назад к редактированию", "view:{}".format(tid))])

    def settlements(self, uid, tid, page=0):
        t = self.s.training(self.scope(uid), tid)
        if t["status"] == "draft":
            raise ValidationError("Сначала рассчитайте и зафиксируйте тренировку.")
        lines, _, _ = self.report(uid, tid)
        records = self.s.transfers(tid)
        lines += ["", "Переводы (отметки организатора):"]
        for tr in records:
            lines.append("{} → {}: {} · оплачено {} · остаток {}".format(tr["sender_name"], tr["recipient_name"], money(tr["amount"]), money(tr["paid"]), money(tr["amount"]-tr["paid"])))
        if not records:
            lines.append("Переводы не нужны.")
        buttons = []
        if t["status"] == "settling":
            buttons = [("{} → {}".format(tr["sender_name"], tr["recipient_name"]), "transfer:{}".format(tr["id"])) for tr in records[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
            buttons += self.pages("settlements:{}".format(tid), page, len(records))
            buttons += [("Вернуть к редактированию", "unlock:{}".format(tid)), ("Закрыть тренировку", "close:{}".format(tid))]
        else:
            lines += ["", "Тренировка закрыта. Все переводы отмечены."]
            buttons += [("Открыть снова для исправления оплат", "reopen:{}".format(tid))]
        published = self.s.one("SELECT title FROM publications WHERE training_id=?", (tid,))
        buttons.append(("Обновить итог в группе" if published else "Опубликовать итог в группе", "publish:{}".format(tid)))
        if published:
            lines += ["", "Группа для итога: " + published["title"]]
        buttons += [("К тренировке", "view:{}".format(tid))]
        self.send(uid, "\n".join(lines), buttons)

    def transfer_view(self, uid, transfer_id):
        tr = self.s.transfer(self.scope(uid), transfer_id)
        remaining = tr["amount"] - tr["paid"]
        buttons = []
        if remaining:
            buttons += [("Отметить остаток оплаченным", "payfull:{}".format(transfer_id)),
                        ("Отметить частичную оплату", "paypart:{}".format(transfer_id))]
        if tr["paid"]:
            buttons.append(("Отменить последнюю отметку", "undo:{}".format(transfer_id)))
        buttons.append(("К расчёту", "settlements:{}:0".format(tr["training_id"])))
        self.send(uid, "{} → {}\nВсего: {}\nОплачено: {}\nОсталось: {}\n\nБот записывает факт оплаты со слов организатора. Деньги не переводит.".format(
            tr["sender_name"], tr["recipient_name"], money(tr["amount"]), money(tr["paid"]), money(remaining)), buttons)

    def action(self, uid, action):
        parts = action.split(":")
        cmd = parts[0]
        args = parts[1:]
        if cmd == "home":
            return self.home(uid)
        if cmd == "group":
            return self.group_help(uid)
        if cmd == "new":
            today = datetime.now(timezone(timedelta(hours=7))).strftime("%d.%m.%Y")
            return self.prompt(uid, "Введите дату тренировки: ДД.ММ.ГГГГ (например {}).\nМожно написать «сегодня».".format(today), "newdate")
        if cmd == "register":
            return self.prompt(uid, "Введите имя нового участника. Для одноимённых людей добавьте фамилию или пояснение.", "register")
        if cmd == "locregister":
            return self.prompt(uid, "Введите название локации.", "locregister")
        if cmd == "history":
            return self.history(uid, int(args[0]))
        if cmd in ("loc", "locadd", "locpage"):
            _, state = self.s.session(uid)
            if state.get("kind") != "location":
                raise ValidationError("Откройте выбор локации заново.")
            if cmd == "locpage":
                return self.locations(uid, state["date"], state["tid"], int(args[0]))
            if cmd == "locadd":
                return self.prompt(uid, "Введите название новой локации.", "newlocation", state["tid"], date=state["date"])
            loc = self.s.one("SELECT id FROM locations WHERE id=? AND owner=?", (int(args[0]), self.scope(uid)))
            if not loc:
                raise ValidationError("Локация не найдена.")
            return self.chosen_location(uid, state, loc[0])
        if not args or not args[0].isdigit():
            raise ValidationError("Неизвестная кнопка. Откройте /start.")
        number = int(args[0])
        if cmd in ("publish", "publishconfirm"):
            t = self.s.training(self.scope(uid), number)
            if t["status"] == "draft":
                raise ValidationError("Сначала зафиксируйте расчёт.")
            pub = self.s.one("SELECT * FROM publications WHERE training_id=?", (number,))
            group = pub or self.s.one("SELECT * FROM group_bindings WHERE owner=?", (self.scope(uid),))
            if not group:
                return self.group_help(uid)
            if cmd == "publish" and not pub:
                return self.send(uid, "Опубликовать итог тренировки #{} в группе «{}»?\nВ сообщении будут стоимость участия, имена и суммы переводов. Отметки оплат будут обновляться автоматически.".format(number, group["title"]),
                                 [("Опубликовать в «{}»".format(group["title"]), "publishconfirm:{}".format(number)), ("Назад", "settlements:{}:0".format(number))],
                                 {"kind": "publish", "tid": number, "chat_id": group["chat_id"], "thread_id": group["thread_id"], "title": group["title"]})
            if not pub:
                _, state = self.s.session(uid)
                if state.get("kind") != "publish" or state.get("tid") != number:
                    raise ValidationError("Откройте публикацию заново.")
                self.s.execute("INSERT INTO publications(training_id,chat_id,title,thread_id) VALUES (?,?,?,?)",
                               (number, state["chat_id"], state["title"], state["thread_id"]))
            queue_updates(self.s, self.scope(uid), force_tid=number, notify_uid=uid)
            return self.settlements(uid, number)
        if cmd == "view":
            return self.view(uid, number)
        if cmd == "members":
            return self.members(uid, number, int(args[1]))
        if cmd in ("choose", "payers"):
            return self.choose(uid, number, int(args[1]), cmd == "payers")
        if cmd in ("select", "person", "hours", "count", "price", "remove", "courtperson"):
            pid = int(args[1])
            self.s.training(self.scope(uid), number, draft=True)
            person = self.s.person_owned(self.scope(uid), pid)
            if cmd == "select":
                self.s.add_participant(self.scope(uid), number, pid)
                return self.choose(uid, number, 0)
            if cmd == "person":
                return self.person_view(uid, number, pid)
            if cmd == "remove":
                self.s.execute("DELETE FROM participants WHERE training_id=? AND person_id=?", (number, pid))
                return self.members(uid, number, 0)
            prompts = {"hours": "Введите время участия в часах, например 1,5.",
                       "count": "Сколько воланов потрачено? Введите целое число; 0 — ни одного.",
                       "price": "Введите стоимость одного волана в батах.",
                       "courtperson": "Сколько всего этот человек оплатил за корт? Новая сумма заменит прежнюю. 0 — убрать оплату."}
            return self.prompt(uid, person["name"] + "\n" + prompts[cmd], cmd, number, pid=pid)
        if cmd in ("add", "payeradd", "cost", "date", "location"):
            t = self.s.training(self.scope(uid), number, draft=True)
            if cmd == "location":
                return self.locations(uid, t["date"], number)
            if cmd == "add":
                return self.prompt(uid, "Введите имя участника. Для одноимённых людей добавьте пояснение.", "add", number, saved=args[1] == "saved")
            prompts = {"payeradd": "Введите имя плательщика. Он сохранится в справочнике; добавлять его в состав тренировки необязательно.",
                       "cost": "Введите полную стоимость корта в батах.", "date": "Введите дату: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."}
            return self.prompt(uid, prompts[cmd], cmd, number)
        if cmd == "preview":
            return self.preview(uid, number)
        if cmd == "lock":
            self.s.lock(self.scope(uid), number)
            return self.settlements(uid, number)
        if cmd == "settlements":
            return self.settlements(uid, number, int(args[1]))
        if cmd == "transfer":
            return self.transfer_view(uid, number)
        if cmd in ("payfull", "paypart", "undo"):
            tr = self.s.transfer(self.scope(uid), number)
            if cmd == "paypart":
                return self.prompt(uid, "Введите полученную сумму в батах. Остаток: {}.".format(money(tr["amount"]-tr["paid"])), "payment", tr["training_id"], transfer_id=number)
            if cmd == "payfull":
                self.s.pay(self.scope(uid), number, tr["amount"]-tr["paid"])
            else:
                self.s.undo_payment(self.scope(uid), number)
            return self.transfer_view(uid, number)
        if cmd == "unlock":
            self.s.unlock(self.scope(uid), number)
            return self.view(uid, number)
        if cmd == "close":
            self.s.close_training(self.scope(uid), number)
            return self.settlements(uid, number)
        if cmd == "reopen":
            t = self.s.training(self.scope(uid), number)
            if t["status"] != "closed":
                raise ValidationError("Тренировка не закрыта.")
            self.s.execute("UPDATE trainings SET status='settling' WHERE id=?", (number,))
            return self.settlements(uid, number)
        raise ValidationError("Неизвестная кнопка. Откройте /start.")

    @staticmethod
    def parse_date(text):
        if text.casefold() == "сегодня":
            return datetime.now(timezone(timedelta(hours=7))).date().isoformat()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValidationError("Введите существующую дату: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")

    def chosen_location(self, uid, state, location_id):
        if state["tid"]:
            self.s.training(self.scope(uid), state["tid"], draft=True)
            self.s.execute("UPDATE trainings SET location_id=? WHERE id=?", (location_id, state["tid"]))
            return self.view(uid, state["tid"])
        return self.prompt(uid, "Введите полную стоимость корта в батах.", "newcost", date=state["date"], location_id=location_id)

    def text(self, uid, text, state):
        command = text.split(" ")[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            return self.home(uid)
        if command == "/new":
            return self.action(uid, "new")
        if command == "/history":
            return self.history(uid, 0)
        if command == "/cancel":
            return self.view(uid, state["tid"]) if state.get("tid") else self.home(uid)
        kind = state.get("kind")
        tid = state.get("tid", 0)
        if kind == "newdate":
            return self.locations(uid, self.parse_date(text))
        if kind in ("newlocation", "locregister"):
            location_id = self.s.location(self.scope(uid), text)
            return self.chosen_location(uid, state, location_id) if kind == "newlocation" else self.home(uid)
        if kind == "newcost":
            cost = money_input(text)
            tid = self.s.execute("INSERT INTO trainings(owner,date,location_id,court_cost,created_by) VALUES (?,?,?,?,?)", (self.scope(uid), state["date"], state["location_id"], cost, uid)).lastrowid
            return self.members(uid, tid, 0)
        if kind == "register":
            self.s.person(self.scope(uid), text)
            return self.home(uid)
        if kind in ("add", "payeradd"):
            self.s.training(self.scope(uid), tid, draft=True)
            pid = self.s.person(self.scope(uid), text, state.get("saved", True))
            if kind == "payeradd":
                return self.prompt(uid, "Введите сумму, которую этот человек оплатил за корт.", "courtperson", tid, pid=pid)
            self.s.add_participant(self.scope(uid), tid, pid)
            return self.person_view(uid, tid, pid)
        if kind in ("hours", "count", "price"):
            field, parse = {"hours": ("minutes", hours_input), "count": ("shuttle_count", quantity_input), "price": ("shuttle_price", money_input)}[kind]
            value = parse(text)
            self.s.update_participant(self.scope(uid), tid, state["pid"], field, value)
            if kind == "count" and value:
                p = self.s.one("SELECT shuttle_price FROM participants WHERE training_id=? AND person_id=?", (tid, state["pid"]))
                if not p[0]:
                    return self.prompt(uid, "Теперь введите стоимость одного волана в батах.", "price", tid, pid=state["pid"])
            return self.person_view(uid, tid, state["pid"])
        if kind == "courtperson":
            self.s.set_court_payment(self.scope(uid), tid, state["pid"], money_input(text))
            return self.view(uid, tid)
        if kind in ("cost", "date"):
            self.s.training(self.scope(uid), tid, draft=True)
            if kind == "cost":
                cost = money_input(text)
                paid = self.s.one("SELECT COALESCE(SUM(amount),0) FROM court_payments WHERE training_id=?", (tid,))[0]
                if cost < paid:
                    raise ValidationError("Новая стоимость меньше уже указанных оплат. Сначала уменьшите оплаты корта.")
                self.s.execute("UPDATE trainings SET court_cost=? WHERE id=?", (cost, tid))
            else:
                self.s.execute("UPDATE trainings SET date=? WHERE id=?", (self.parse_date(text), tid))
            return self.view(uid, tid)
        if kind == "payment":
            self.s.pay(self.scope(uid), state["transfer_id"], money_input(text))
            return self.transfer_view(uid, state["transfer_id"])
        self.s.enqueue("sendMessage", {"chat_id": uid, "text": "Выберите действие в последнем меню или используйте /start."})
