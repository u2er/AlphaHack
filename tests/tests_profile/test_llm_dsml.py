import json
import unittest
from profile.onboarding import OnboardingAction
from utils.llm import Completion, LLMError, dsml_to_json, generate_structured, output_schema

SCHEMA = output_schema(OnboardingAction)
D = '｜｜DSML｜｜'

# Ответы deepseek-flash из трассировок онбординга: три разные формы одного вызова read_file.
TOOL_AS_PARAMETER = (
    f'<{D} calls> <{D} invoke name="tool" string="true">read_file</{D} parameter> '
    f'<{D} parameter name="note" string="true">Читаю кредитный портфель.</{D} parameter> '
    f'<{D} parameter name="query" string="true"></{D} parameter> '
    f'<{D} parameter name="file_id" string="true">f2</{D} parameter> '
    f'<{D} parameter name="page" string="false">1</{D} parameter> '
    f'<{D} parameter name="question" string="false">null</{D} parameter> '
    f'<{D} parameter name="draft" string="false">null</{D} parameter> </{D} invoke> </{D} calls>')
TOOL_AS_INVOKE = (
    f'<{D} calls> <{D} invoke name="read_file"> '
    f'<{D} parameter name="query" string="true"></{D} parameter> '
    f'<{D} parameter name="file_id" string="true">f2</{D} parameter> '
    f'<{D} parameter name="page" string="false">2</{D} parameter> '
    f'<{D} parameter name="question" string="false">null</{D} parameter> '
    f'<{D} parameter name="draft" string="false">null</{D} parameter> '
    f'<{D} parameter name="note" string="true">Дочитываю кредитный портфель.</{D} parameter> </{D} invoke> </{D} calls>')
NO_STRING_FLAGS = (
    f'<{D} invoke name="read_file"> <{D} parameter name="file_id">f2</{D} parameter> '
    f'<{D} parameter name="page">2</{D} parameter> <{D} parameter name="query"></{D} parameter> '
    f'<{D} parameter name="question" string="false">null</{D} parameter> '
    f'<{D} parameter name="draft" string="false">null</{D} parameter> '
    f'<{D} parameter name="note" string="true">Дочитываю.</{D} parameter> </{D} invoke> </{D} calls>')


def action(**fields):
    return json.dumps(dict(tool='read_file', note='Читаю', query='', file_id='f1', page=1, question=None, draft=None) | fields)


class LLM:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def complete_structured(self, system, payload, model):
        self.calls.append(payload)
        return Completion(self.answers.pop(0), {})


class DsmlTests(unittest.TestCase):
    def test_each_dsml_form_becomes_a_valid_action(self):
        for text, page in ((TOOL_AS_PARAMETER, 1), (TOOL_AS_INVOKE, 2), (NO_STRING_FLAGS, 2)):
            with self.subTest(page=page):
                result = OnboardingAction.model_validate_json(dsml_to_json(text, SCHEMA), strict=True)
                self.assertEqual((result.tool, result.file_id, result.page, result.question), ('read_file', 'f2', page, None))

    def test_not_dsml_gives_nothing(self):
        self.assertIsNone(dsml_to_json('Просто текст без разметки', SCHEMA))

    def test_dsml_answer_is_accepted_without_a_repair(self):
        llm, attempts = LLM([TOOL_AS_INVOKE]), []
        result = generate_structured(llm, 'system', {}, OnboardingAction, on_attempt=attempts.append)
        self.assertEqual((result.tool, result.page), ('read_file', 2))
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(attempts[0].get('recovered'), 'dsml')

    def test_non_json_is_retried_without_echoing_it(self):
        llm = LLM(['<tool_call>read_file</tool_call>', action()])
        generate_structured(llm, 'system', {}, OnboardingAction)
        repair = llm.calls[1]['json_repair']
        self.assertNotIn('previous_response', repair)
        self.assertEqual(repair['validation_errors'][0]['type'], 'json_invalid')
        self.assertIn('hint', repair)

    def test_invalid_json_still_gets_previous_response(self):
        llm = LLM([action(tool='open_file'), action()])
        generate_structured(llm, 'system', {}, OnboardingAction)
        self.assertEqual(json.loads(llm.calls[1]['json_repair']['previous_response'])['tool'], 'open_file')

    def test_three_non_json_answers_still_fail(self):
        with self.assertRaises(LLMError):
            generate_structured(LLM(['не json'] * 3), 'system', {}, OnboardingAction)


if __name__ == '__main__':
    unittest.main()
