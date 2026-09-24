"""Pure calculation in integer satang and minutes; no floating-point money."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Sequence


class ValidationError(ValueError):
    pass


def decimal_input(text: str) -> Decimal:
    try:
        value = Decimal(text.strip().replace(",", "."))
    except InvalidOperation:
        raise ValidationError("Введите число, например 120 или 120,50.")
    if not value.is_finite() or value < 0 or value > 1000000:
        raise ValidationError("Введите число от 0 до 1 000 000.")
    return value


def money_input(text: str) -> int:
    value = decimal_input(text) * 100
    if value != value.to_integral_value():
        raise ValidationError("У суммы может быть не больше двух знаков после запятой.")
    return int(value)


def hours_input(text: str) -> int:
    minutes = decimal_input(text) * 60
    if minutes <= 0 or minutes > 24 * 60 or minutes != minutes.to_integral_value():
        raise ValidationError("Введите время от 1 минуты до 24 часов, например 1,5 или 2.")
    return int(minutes)


def quantity_input(text: str) -> int:
    number = decimal_input(text)
    if number != number.to_integral_value() or number > 1000:
        raise ValidationError("Количество воланов — целое число от 0 до 1000.")
    return int(number)


def money(value: int) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    return "{}{}.{:02d} ฿".format(sign, value // 100, value % 100)


def hours(minutes: int) -> str:
    return str(Decimal(minutes) / 60).rstrip("0").rstrip(".") if minutes % 60 else str(minutes // 60)


@dataclass(frozen=True)
class Participant:
    person_id: int
    minutes: int = 120
    shuttle_count: int = 0
    shuttle_price: int = 0

    @property
    def shuttle_cost(self) -> int:
        return self.shuttle_count * self.shuttle_price


@dataclass(frozen=True)
class Transfer:
    sender: int
    recipient: int
    amount: int


@dataclass(frozen=True)
class Calculation:
    total: int
    shares: Dict[int, int]
    balances: Dict[int, int]
    transfers: List[Transfer]


def calculate(court_cost: int, participants: Sequence[Participant],
              court_payments: Mapping[int, int]) -> Calculation:
    if not participants:
        raise ValidationError("Добавьте хотя бы одного участника.")
    if len({p.person_id for p in participants}) != len(participants):
        raise ValidationError("Один участник указан дважды.")
    if court_cost < 0 or any(v < 0 for v in court_payments.values()):
        raise ValidationError("Стоимость и оплаты не могут быть отрицательными.")
    if sum(court_payments.values()) != court_cost:
        raise ValidationError("Оплаты корта должны в сумме равняться его стоимости: {}.".format(money(court_cost)))
    for p in participants:
        if p.minutes <= 0 or p.shuttle_count < 0 or p.shuttle_price < 0:
            raise ValidationError("Проверьте время и воланы участников.")
        if p.shuttle_count and not p.shuttle_price:
            raise ValidationError("Укажите цену потраченных воланов.")
    total = court_cost + sum(p.shuttle_cost for p in participants)
    minutes = sum(p.minutes for p in participants)
    # Largest remainders; person ID breaks ties deterministically.
    shares = {p.person_id: total * p.minutes // minutes for p in participants}
    order = sorted(participants, key=lambda p: (-(total * p.minutes % minutes), p.person_id))
    for p in order[:total - sum(shares.values())]:
        shares[p.person_id] += 1
    balances = dict(shares)
    for p in participants:
        balances[p.person_id] -= p.shuttle_cost
    for person_id, amount in court_payments.items():
        balances[person_id] = balances.get(person_id, 0) - amount
    creditors = [[pid, -v] for pid, v in sorted(balances.items()) if v < 0]
    debtors = [[pid, v] for pid, v in sorted(balances.items()) if v > 0]
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        transfers.append(Transfer(debtors[i][0], creditors[j][0], amount))
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    assert sum(shares.values()) == total and sum(balances.values()) == 0
    return Calculation(total, shares, balances, transfers)
