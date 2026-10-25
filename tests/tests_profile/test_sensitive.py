import unittest
from profile.sensitive import ALWAYS, counts, describe, redact, scan

SAMPLE = '''Директор Иванов, паспорт 45 08 123456 выдан ОВД. СНИЛС 112-233-445 95. ИНН 500100732259.
Тел. +7 (912) 345-67-89, ivanov@corp.ru. Карта 4111 1111 1111 1111, CVV: 123. Пароль: Qwerty123!
api_key = sk-abcdefghijklmnopqrstuv. ИНН компании 7707083893, ОГРН 1027700132195.
р/с 40702810900000012345 БИК 044525593.'''
VALUES = ['45 08 123456', '112-233-445 95', '500100732259', '345-67-89', 'ivanov@corp.ru',
          '4111 1111 1111 1111', 'Qwerty123!', 'sk-abcdefghijklmnopqrstuv', '40702810900000012345']


def kinds(text):
    return [f.kind for f in scan(text)]


class DetectorTests(unittest.TestCase):
    def test_sample_is_fully_masked_and_company_ids_stay(self):
        text = redact(SAMPLE)
        for value in VALUES:
            self.assertNotIn(value, text)
        self.assertIn('[паспорт]', text)
        self.assertIn('7707083893', text)      # ИНН юрлица — публичный
        self.assertIn('1027700132195', text)   # ОГРН
        self.assertEqual(counts(scan(SAMPLE))['card'], 1)

    def test_previews_never_hold_the_value(self):
        for finding in scan(SAMPLE):
            self.assertIn('•', finding.preview)
            self.assertFalse(any(v in finding.preview for v in VALUES), finding)

    def test_checksums_cut_false_positives(self):
        self.assertEqual(kinds('Итого 4111 1111 1111 1112 руб'), [])        # не Luhn
        self.assertEqual(kinds('Номер 4111111111111111 без контекста'), [])  # слитно и без слова «карта»
        self.assertEqual(kinds('card 4111111111111111.'), ['card'])
        self.assertEqual(kinds('ИНН 500100732258'), [])                     # неверная контрольная сумма
        self.assertEqual(kinds('СНИЛС 112-233-445 96'), [])

    def test_context_is_required_for_passport_and_accounts(self):
        self.assertEqual(kinds('Выручка 4508123456 тыс. руб.'), [])
        self.assertEqual(kinds('Серия и номер: 4508 123456'), ['passport'])
        self.assertEqual(kinds('Объём 40702810900000012345 т'), [])
        self.assertEqual(kinds('Расчётный счёт 40702810900000012345'), ['account'])

    def test_confidential_mark_hides_the_page_unless_kept(self):
        page = 'КОММЕРЧЕСКАЯ ТАЙНА. Маржа по контракту 31%, тел. 8 912 345 67 89'
        self.assertTrue(redact(page).startswith('[Страница скрыта'))
        kept = redact(page, ALWAYS)
        self.assertIn('Маржа по контракту 31%', kept)
        self.assertIn('8 912 345 67 89', kept)
        self.assertEqual(kinds('Политика конфиденциальности сайта'), [])

    def test_secrets_are_removed_even_when_kept(self):
        text = redact('Пароль: hunter22, тел. +7 912 345-67-89', hide=())
        self.assertNotIn('hunter22', text)
        self.assertIn('+7 912 345-67-89', text)

    def test_describe(self):
        self.assertEqual(describe({'phone': 2, 'passport': 1}), 'телефон ×2, паспорт')


if __name__ == '__main__':
    unittest.main()
