from fab.importer.types import RawSheetData, RawSmtWorkbook
from fab.importer.sqlite_writer import write_raw_smt_workbook
from fab.importer.types import RawSheetData, RawSmtWorkbook

__all__ = ["Importer", "RawSheetData", "RawSmtWorkbook", "write_raw_smt_workbook"]

def __getattr__(name):
    if name == "Importer":
        from fab.importer.importer import Importer
        return Importer

    raise AttributeError(name)