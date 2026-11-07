from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reona.gates import GateCheck, GateReport, sanitize_output


class S7SanitizerTests(unittest.TestCase):
    @staticmethod
    def secret_cases() -> tuple[tuple[str, str, str], ...]:
        pem_body = "A" * 48 + "+/" * 16
        return (
            ("ghp_", "ghp_" + "Ab3" * 12, "Ab3" * 4),
            ("github_pat_", "github_pat_" + "Cd4" * 12, "Cd4" * 4),
            ("xoxb-", "xoxb-1234567890-synthetic-value", "synthetic"),
            ("AKIA", "AKIA" + "A1B2C3D4E5F6G7H8", "A1B2C3D4"),
            ("sk-", "sk-" + "Ef5" * 12, "Ef5" * 4),
            ("PEM full", "-----BEGIN RSA PRIVATE KEY-----\n" + pem_body
             + "\n-----END RSA PRIVATE KEY-----", pem_body[:16]),
            ("PEM without END", "-----BEGIN PRIVATE KEY-----\n" + pem_body, pem_body[:16]),
            ("Bearer", "Bearer " + "Gh6" * 12, "Gh6" * 4),
            ("password", "password=" + "Ij7" * 12, "Ij7" * 4),
            ("JWT", "eyJ4Ijoi" + "SYNTHETIC" + ".eyJ5Ijoi" + "VALUE" + "." + "Z" * 24,
             "SYNTHETIC"),
        )

    def feedback_tail(self, secret: str, *, crosses_boundary: bool) -> str:
        padding_before = "L" * 100
        tail_padding_size = (1546 if crosses_boundary else 1526) - len(secret.encode("utf-8"))
        self.assertGreaterEqual(tail_padding_size, 0)
        output = padding_before + secret + "R" * tail_padding_size
        report = GateReport((GateCheck("synthetic", False, 1, stdout_tail=output),))
        tail = report.feedback[0]["output_tail"]
        self.assertIsInstance(tail, str)
        assert isinstance(tail, str)
        return tail

    def test_synthetic_secret_formats_are_redacted_before_tail_truncation(self) -> None:
        for name, secret, significant_part in self.secret_cases():
            with self.subTest(format=name):
                self.assertNotIn(secret, sanitize_output("observed " + secret + " finished"))
                for crosses_boundary in (True, False):
                    with self.subTest(crosses_boundary=crosses_boundary):
                        tail = self.feedback_tail(secret, crosses_boundary=crosses_boundary)
                        self.assertNotIn(secret, tail)
                        self.assertNotIn(significant_part, tail)


if __name__ == "__main__":
    unittest.main()
