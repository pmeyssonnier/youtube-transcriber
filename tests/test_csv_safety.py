from __future__ import annotations

import unittest

from app.people import names_csv


class CsvFormulaTests(unittest.TestCase):
    def test_formula_cells_are_neutralised(self) -> None:
        analysis = {
            "summary": [{"nom": "Jean Dupont", "fonction": "=cmd", "occurrences": 2, "niveaux": {"auto": 1}, "variantes": {"+Jean": 1}, "heures": ["00:01:00"]}],
            "unknown": [{"nom": '=HYPERLINK("http://x")', "occurrences": 1}, {"nom": "@SUM(A1)", "occurrences": 1}, {"nom": "Marie Martin", "occurrences": 1}],
        }
        lines = names_csv(analysis).lstrip("\ufeff").splitlines()
        self.assertTrue(any(line.startswith("\"'=HYPERLINK") for line in lines))
        self.assertIn("'@SUM(A1);;1", lines)
        self.assertIn("Marie Martin;;1", lines)
        self.assertIn(";'=cmd;", lines[1])


if __name__ == "__main__":
    unittest.main()
