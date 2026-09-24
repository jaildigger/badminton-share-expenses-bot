import random
import unittest

from badminton.calculator import Participant, ValidationError, calculate, hours_input, money_input, quantity_input


class CalculationTests(unittest.TestCase):
    def test_reference_workbook_same_person_paid_everything(self):
        people = [Participant(i, shuttle_count=4 if i == 3 else 0, shuttle_price=9500 if i == 3 else 0) for i in range(1, 7)]
        result = calculate(35000, people, {3: 35000})
        self.assertEqual(result.total, 73000)
        self.assertEqual(result.shares, {1: 12167, 2: 12167, 3: 12167, 4: 12167, 5: 12166, 6: 12166})
        self.assertEqual(result.balances[3], -60833)
        self.assertEqual(sum(t.amount for t in result.transfers), 60833)
        self.assertTrue(all(t.recipient == 3 for t in result.transfers))

    def test_different_payers(self):
        people = [Participant(i, shuttle_count=4 if i == 3 else 0, shuttle_price=9500 if i == 3 else 0) for i in range(1, 7)]
        result = calculate(35000, people, {1: 35000})
        self.assertEqual(result.balances[1], -22833)
        self.assertEqual(result.balances[3], -25833)
        self.assertEqual(sum(t.amount for t in result.transfers), 48666)

    def test_different_time_and_prices(self):
        result = calculate(30000, [Participant(1, 60, 1, 5000), Participant(2, 120, 2, 7500)], {1: 10000, 2: 20000})
        self.assertEqual(result.total, 50000)
        self.assertEqual(result.shares, {1: 16667, 2: 33333})
        self.assertEqual(result.balances, {1: 1667, 2: -1667})

    def test_nonplaying_payer(self):
        result = calculate(10000, [Participant(1), Participant(2)], {3: 10000})
        self.assertEqual(result.balances, {1: 5000, 2: 5000, 3: -10000})

    def test_zero_cost_and_one_player(self):
        self.assertEqual(calculate(0, [Participant(1)], {}).transfers, [])
        self.assertEqual(calculate(500, [Participant(1, 120, 2, 50)], {1: 500}).balances, {1: 0})

    def test_rounding_is_order_independent(self):
        members = [Participant(3), Participant(1), Participant(2)]
        self.assertEqual(calculate(2, members, {1: 2}).shares, {1: 1, 2: 1, 3: 0})

    def test_invalid_calculation(self):
        cases = [(1, [], {1: 1}), (10, [Participant(1)], {}),
                 (10, [Participant(1)], {1: 11}), (0, [Participant(1, 0)], {}),
                 (0, [Participant(1), Participant(1)], {}), (0, [Participant(1, 120, 2, 0)], {})]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                calculate(*args)

    def test_random_conservation_and_transfers_settle_everyone(self):
        rng = random.Random(20260925)
        for _ in range(500):
            count = rng.randint(1, 40)
            people = [Participant(i, rng.randint(1, 240), rng.randint(0, 8), rng.randint(1, 10000)) for i in range(count)]
            court = rng.randint(0, 100000)
            first = rng.randint(0, court)
            result = calculate(court, people, {0: first, count+1: court-first})
            self.assertEqual(sum(result.shares.values()), result.total)
            self.assertEqual(sum(result.balances.values()), 0)
            balances = result.balances.copy()
            for tr in result.transfers:
                self.assertGreater(tr.amount, 0)
                self.assertNotEqual(tr.sender, tr.recipient)
                balances[tr.sender] -= tr.amount
                balances[tr.recipient] += tr.amount
            self.assertTrue(all(x == 0 for x in balances.values()))


class InputTests(unittest.TestCase):
    def test_russian_decimal(self):
        self.assertEqual(money_input("95,50"), 9550)
        self.assertEqual(hours_input("1,5"), 90)
        self.assertEqual(quantity_input("0"), 0)

    def test_invalid_numbers(self):
        for value in ("NaN", "Infinity", "-1", "1000001", "abc", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                money_input(value)
        for parse, value in ((money_input, "1.001"), (hours_input, "0"),
                             (hours_input, "25"), (hours_input, "0.001"), (quantity_input, "1.5")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse(value)
