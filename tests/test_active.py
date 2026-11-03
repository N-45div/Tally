from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject

from tally.airlock.active import find_active_content
from tests.invoices import make


def _rewrite(src, dst, change):
    w = PdfWriter(clone_from=PdfReader(str(src)))
    change(w)
    with open(dst, "wb") as f:
        w.write(f)
    return dst


def test_a_plain_invoice_does_nothing(tmp_path):
    assert find_active_content(str(make(tmp_path / "clean.pdf"))) == []


def test_javascript_that_runs_when_the_file_opens_is_held(tmp_path):
    clean = make(tmp_path / "clean.pdf")
    f = _rewrite(clean, tmp_path / "js.pdf", lambda w: w.add_js("app.alert('paid');"))
    found = find_active_content(str(f))
    assert any(x.severity == "hold" and "JavaScript" in x.what for x in found), found


def test_an_embedded_file_is_held(tmp_path):
    clean = make(tmp_path / "clean.pdf")
    f = _rewrite(clean, tmp_path / "attach.pdf", lambda w: w.add_attachment("remittance.exe", b"MZ\x90\x00"))
    found = find_active_content(str(f))
    assert any(x.severity == "hold" and "remittance.exe" in x.what for x in found), found


def test_a_launch_action_on_open_is_held(tmp_path):
    clean = make(tmp_path / "clean.pdf")

    def launch(w):
        action = DictionaryObject({NameObject("/S"): NameObject("/Launch"), NameObject("/F"): TextStringObject("cmd.exe")})
        w._root_object[NameObject("/OpenAction")] = w._add_object(action)

    found = find_active_content(str(_rewrite(clean, tmp_path / "launch.pdf", launch)))
    assert any(x.severity == "hold" and "launches" in x.what and x.where == "when the file is opened" for x in found), found


def test_a_website_link_is_information_not_a_reason_to_hold(tmp_path):
    clean = make(tmp_path / "clean.pdf")

    def link(w):
        w.add_uri(0, "https://northwind.example/pay", [50, 400, 300, 420])

    found = find_active_content(str(_rewrite(clean, tmp_path / "link.pdf", link)))
    assert [x.severity for x in found] == ["info"] and "northwind.example" in found[0].what, found


def test_an_encrypted_file_cannot_be_inspected_so_it_is_held(tmp_path):
    clean = make(tmp_path / "clean.pdf")
    f = _rewrite(clean, tmp_path / "locked.pdf", lambda w: w.encrypt(user_password="x", owner_password="y"))
    assert [x.severity for x in find_active_content(str(f))] == ["hold"]
