import pypdfium2 as pdfium
import pytest

from tally.airlock.hidden import find_hidden_text
from tests.invoices import HIDDEN, INJECTION, VISIBLE, make


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_finds_each_way_of_hiding_an_instruction(tmp_path, name):
    draw, reason = HIDDEN[name]
    pdf = pdfium.PdfDocument(str(make(tmp_path / f"{name}.pdf", draw)))
    runs = find_hidden_text(pdf)
    assert runs, f"{name}: nothing found"
    # The whole instruction is caught, in one run, for the right reason.
    assert any(INJECTION.replace(" ", "") in r.text.replace(" ", "") and reason in r.reason for r in runs), runs
    # And nothing of the visible invoice around it is flagged.
    assert all("Northwind" not in r.text and "TOTAL" not in r.text for r in runs), runs


@pytest.mark.parametrize("name", sorted(VISIBLE))
def test_leaves_what_a_person_can_see_alone(tmp_path, name):
    pdf = pdfium.PdfDocument(str(make(tmp_path / f"{name}.pdf", VISIBLE[name])))
    assert find_hidden_text(pdf) == []
