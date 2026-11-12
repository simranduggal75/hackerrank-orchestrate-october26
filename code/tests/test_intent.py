"""Intent classification from the request wording only (made-up sentences; no dataset text)."""

import unittest

from intent import classify_intent

AUTHORIZED = {
    "english_delete": "Please delete my account and every personal record you hold about me.",
    "english_erase": "I want all my data erased from your systems.",
    "close_and_remove": "Close my account and remove my details, thanks.",
    "already_deleted_check": "I deleted my account last month; please confirm nothing of mine is left.",
    "spanish": "Solicito que eliminen mi cuenta y todos mis datos personales.",
    "french_suppression": "Je demande la suppression de mon compte et de mes données.",
    "german": "Bitte löschen Sie mein Konto und alle meine Daten.",
    "portuguese": "Peço que apaguem a minha conta e os meus dados.",
    "japanese": "アカウントの削除をお願いします。",
    "retention_permission_is_not_a_limiter": "Delete my account. You may keep anonymous statistics if the law requires it.",
}
NOT_AUTHORIZED = {
    "access": "Could you send me a copy of the personal information you hold about me?",
    "correction": "My surname is misspelled in my profile; please correct it.",
    "marketing_only": "Please remove me from marketing lists only.",
    "unsubscribe": "I would like to unsubscribe from your newsletters.",
    "negated": "I am not asking for deletion, I only want to know what you store.",
    "keeps_account": "Do not close it; leave the account active, I still use the service.",
    "login_help": "I can't access my account, please reset the login.",
    "empty_of_content": "Hello, thank you for your help yesterday.",
}


class IntentTests(unittest.TestCase):
    def test_authorizing_wordings(self):
        for name, text in AUTHORIZED.items():
            self.assertTrue(classify_intent(text).authorized, name)

    def test_non_deletion_wordings(self):
        for name, text in NOT_AUTHORIZED.items():
            self.assertFalse(classify_intent(text).authorized, name)

    def test_a_limiter_wins_over_the_lexicon_in_the_same_sentence(self):
        result = classify_intent("Delete nothing: leave the account active and keep my order history in place.")
        self.assertFalse(result.authorized)
        self.assertTrue(result.blockers)

    def test_a_limiter_in_one_sentence_does_not_cancel_a_request_in_another(self):
        result = classify_intent("Please delete my account. Marketing emails are fine, I am not asking to change those.")
        self.assertTrue(result.authorized)

    def test_the_result_explains_itself(self):
        result = classify_intent("Please delete my account.")
        self.assertTrue(result.basis)
        self.assertTrue(result.affirmative)
        self.assertEqual(result.blockers, ())

    def test_classification_is_deterministic_and_case_insensitive(self):
        text = "PLEASE DELETE MY ACCOUNT"
        self.assertEqual(classify_intent(text), classify_intent(text))
        self.assertEqual(classify_intent(text).authorized, classify_intent(text.lower()).authorized)

    def test_empty_and_whitespace_text_do_not_authorize(self):
        for text in ("", "   ", "\n"):
            self.assertFalse(classify_intent(text).authorized)


if __name__ == "__main__":
    unittest.main()
