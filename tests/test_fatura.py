"""The FATURA benchmark's reading of the dataset's annotations, on strings copied from them (FATURA, CC BY 4.0)."""
from lab.fatura import correct, pick, truth


def test_labels_are_stripped_and_values_parsed():
    ann = {
        "SELLER_NAME": {"text": "Foster, Wells and Martin"}, "NUMBER": {"text": "INVOICE # 9Y2M7d-647"},
        "DATE": {"text": "Invoice Date: 16-Jan-2014"}, "DUE_DATE": {"text": "Due Date : 16-Aug-2021"},
        "PO_NUMBER": {"text": "PO Number :76"}, "SUB_TOTAL": {"text": "SUB_TOTAL : 502.83  $"},
        "TAX": {"text": "TAX:VAT (4.65%):  23.40 $"}, "TOTAL": {"text": "TOTAL : 1357.49 EUR"},
        "TABLE": [[{"bbox": [[3.0, 426.8], [543.0, 318.8]]}]], "LOGO": {"bbox": [[5, 751], [95, 661]]},
    }
    assert truth(ann) == {"vendor_name": "Foster, Wells and Martin", "invoice_number": "9Y2M7d-647", "invoice_date": "2014-01-16",
                          "due_date": "2021-08-16", "po_number": "76", "subtotal": 50283, "tax": 2340, "total": 135749, "currency": "EUR"}
    for number in ("INVOICE ID INV/47-24/571", "Invoice number INV64837157"):
        assert truth({"NUMBER": {"text": number}})["invoice_number"] in ("INV/47-24/571", "INV64837157")
    assert truth({"TOTAL": {"text": "BALANCE DUE : 505.77 USD"}}) == {"total": 50577, "currency": "USD"}
    assert truth({"TOTAL": {"text": "TOTAL : 171.45 $"}})["currency"] == "USD"


def test_a_field_the_layout_does_not_print_is_not_scored():
    assert truth({"OTHER": {"text": "anything"}, "INVOICE_INFO": []}) == {}


def test_what_counts_as_read_correctly():
    assert correct("invoice_number", "9Y2M7D-647", "9Y2M7d-647") and not correct("invoice_number", "9Y2M7d-64", "9Y2M7d-647")
    assert correct("invoice_date", "2014-01-16", "2014-01-16") and correct("invoice_date", "16-Jan-2014", "2014-01-16")
    assert not correct("invoice_date", "01/16/2014", "2014-01-16")  # not a format the reader is asked for
    assert correct("total", 1357.49, 135749) and not correct("total", 1357.5, 135749)
    assert correct("currency", "$", "USD") and correct("currency", "usd", "USD") and not correct("currency", "", "USD")
    assert correct("vendor_name", "Brooks LLC", "Brooks LLC") and correct("vendor_name", "BROOKS", "Brooks LLC")
    assert not correct("vendor_name", "", "Brooks LLC") and not correct("invoice_number", "", "")


def test_the_sample_takes_every_layout_on_both_backgrounds():
    s = pick(4)
    assert len(s) == 200 and {t for t, _, _ in s} == set(range(1, 51))
    assert sum(f == "images" for _, _, f in s) == sum(f == "colored_images" for _, _, f in s) == 100
