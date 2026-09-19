import os
from pathlib import Path
import tempfile
import unittest

import openpyxl

from kpi_crawler.errors import KPIDictionaryError
from kpi_crawler.retrieval.kpi_dictionary import REQUIRED_COLUMNS, load_kpi_dictionary

FULL_HEADER = [
    "KPI_Code", "Domain", "Parameter", "Sub_Parameter", "Variable_Name", "Definition",
    "Unit", "Data_Type", "Formula", "Primary_Source", "Benchmark_Direction", "Priority_12M",
]


def _write_workbook(path: Path, header: list[str], rows: list[list], sheet_name: str = "KPI_Dictionary") -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class KPIDictionaryLoadingTests(unittest.TestCase):
    def test_loads_all_rows_with_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            _write_workbook(
                path,
                FULL_HEADER,
                [
                    ["A01", "ACA", "Academic Portfolio", "Programme Portfolio", "UG programme count",
                     "Number of undergraduate programmes", "count", "integer", None,
                     "annual report", "Higher", "P2"],
                    ["A02", "ACA", "Academic Portfolio", "Programme Portfolio", "PG programme count",
                     "Number of postgraduate programmes", "count", "integer", None,
                     "annual report", "Higher", "P2"],
                ],
            )
            definitions = load_kpi_dictionary(path)
            self.assertEqual(len(definitions), 2)
            self.assertEqual(definitions[0].kpi_code, "A01")
            self.assertEqual(definitions[0].variable_name, "UG programme count")
            self.assertEqual(definitions[0].embedding_text, "UG programme count: Number of undergraduate programmes")
            self.assertEqual(definitions[0].source_row["KPI_Code"], "A01")

    def test_missing_file_raises_kpi_dictionary_error(self):
        with self.assertRaises(KPIDictionaryError):
            load_kpi_dictionary(Path("/nonexistent/does-not-exist.xlsx"))

    def test_unreadable_file_raises_kpi_dictionary_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-workbook.xlsx"
            path.write_text("this is not a real xlsx file", encoding="utf-8")
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_missing_sheet_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            _write_workbook(path, FULL_HEADER, [["A01", *[None] * (len(FULL_HEADER) - 1)]], sheet_name="Other_Sheet")
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_missing_required_column_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            header_without_definition = [c for c in FULL_HEADER if c != "Definition"]
            _write_workbook(path, header_without_definition, [["A01", "ACA", "P", "SP", "Name"]])
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_row_missing_identity_value_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            _write_workbook(
                path,
                FULL_HEADER,
                [["A01", "ACA", "Academic Portfolio", "Programme Portfolio", "UG programme count", None,
                  "count", "integer", None, "annual report", "Higher", "P2"]],
            )
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_duplicate_kpi_code_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            row = ["A01", "ACA", "Academic Portfolio", "Programme Portfolio", "UG programme count",
                   "Number of undergraduate programmes", "count", "integer", None,
                   "annual report", "Higher", "P2"]
            _write_workbook(path, FULL_HEADER, [row, list(row)])
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_empty_sheet_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dictionary.xlsx"
            _write_workbook(path, FULL_HEADER, [])
            with self.assertRaises(KPIDictionaryError):
                load_kpi_dictionary(path)

    def test_required_columns_are_identity_name_description(self):
        self.assertEqual(REQUIRED_COLUMNS, ("KPI_Code", "Variable_Name", "Definition"))


@unittest.skipUnless(
    os.environ.get("KPI_DICTIONARY_PATH"), "KPI_DICTIONARY_PATH is required for the real-fixture count test"
)
class RealKPIDictionaryFixtureTests(unittest.TestCase):
    def test_real_dictionary_loads_229_kpis(self):
        path = Path(os.environ["KPI_DICTIONARY_PATH"])
        definitions = load_kpi_dictionary(path)
        self.assertEqual(len(definitions), 229)
        codes = {d.kpi_code for d in definitions}
        self.assertEqual(len(codes), 229)
        for definition in definitions:
            self.assertTrue(definition.variable_name)
            self.assertTrue(definition.definition)


if __name__ == "__main__":
    unittest.main()
