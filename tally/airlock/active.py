"""Parts of a PDF that do something when it is opened, rather than show something.

An invoice is a page of text and numbers; it has no reason to run JavaScript, launch a program, carry another file
inside it, or send form data somewhere. Each of those is reported, with where in the file it sits. Links to websites
are listed too, as information: an invoice may well have one, and a person should see where it goes.

Only the PDF's own structure is read; nothing found here is ever run or followed.
"""
from __future__ import annotations

from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

# Action types that make a viewer do something, and what each does in words.
RISKY_ACTIONS = {
    "/JavaScript": "runs JavaScript",
    "/Launch": "launches a program or file",
    "/SubmitForm": "sends form data to a server",
    "/ImportData": "imports data from another file",
    "/GoToR": "opens another PDF file",
    "/GoToE": "opens a file embedded in this one",
    "/Rendition": "plays media",
    "/RichMediaExecute": "runs rich media",
    "/Hide": "hides or shows parts of the page",
}


@dataclass
class Finding:
    severity: str  # "hold": a reason to stop the invoice; "info": something a person should see
    what: str
    where: str


def _obj(x):
    return x.get_object() if isinstance(x, IndirectObject) else x


def _actions(action, where: str, seen: set[int]):
    """An action and every action chained after it with /Next."""
    action = _obj(action)
    if isinstance(action, ArrayObject):
        for a in action:
            yield from _actions(a, where, seen)
        return
    if not isinstance(action, DictionaryObject) or id(action) in seen:
        return
    seen.add(id(action))
    yield action, where
    if "/Next" in action:
        yield from _actions(action["/Next"], where, seen)


def _name_tree_values(node, seen: set[int]):
    node = _obj(node)
    if not isinstance(node, DictionaryObject) or id(node) in seen:
        return
    seen.add(id(node))
    names = _obj(node.get("/Names"))
    if isinstance(names, ArrayObject):
        for i in range(1, len(names), 2):
            yield names[i - 1], _obj(names[i])
    for kid in _obj(node.get("/Kids")) or []:
        yield from _name_tree_values(kid, seen)


def _classify(action: DictionaryObject, where: str, out: list[Finding]) -> None:
    kind = str(action.get("/S", ""))
    if kind in RISKY_ACTIONS:
        out.append(Finding("hold", f"{RISKY_ACTIONS[kind]} ({kind[1:]})", where))
    elif kind == "/URI":
        out.append(Finding("info", f"links to {str(_obj(action.get('/URI', '')))[:200]}", where))


def find_active_content(path: str, max_pages: int | None = None) -> list[Finding]:
    out: list[Finding] = []
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        return [Finding("hold", "the file is encrypted, so it cannot be inspected", "document")]
    root = _obj(reader.trailer["/Root"])
    seen: set[int] = set()

    if "/OpenAction" in root:
        for a, w in _actions(root["/OpenAction"], "when the file is opened", seen):
            _classify(a, w, out)
    for trigger, a in (_obj(root.get("/AA")) or {}).items():
        for act, w in _actions(a, f"document event {trigger}", seen):
            _classify(act, w, out)

    names = _obj(root.get("/Names")) or {}
    for name, value in _name_tree_values(names.get("/JavaScript"), set()):
        out.append(Finding("hold", f"runs JavaScript (named script {str(name)[:60]!r})", "document scripts"))
    for name, value in _name_tree_values(names.get("/EmbeddedFiles"), set()):
        out.append(Finding("hold", f"carries an embedded file {str(name)[:80]!r}", "attachments"))

    acro = _obj(root.get("/AcroForm"))
    if isinstance(acro, DictionaryObject) and "/XFA" in acro:
        out.append(Finding("hold", "has an XFA form, which can run scripts", "form"))

    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    for n, page in enumerate(pages, start=1):
        for trigger, a in (_obj(page.get("/AA")) or {}).items():
            for act, w in _actions(a, f"page {n} event {trigger}", seen):
                _classify(act, w, out)
        for annot in _obj(page.get("/Annots")) or []:
            annot = _obj(annot)
            if not isinstance(annot, DictionaryObject):
                continue
            sub = str(annot.get("/Subtype", ""))
            if sub == "/FileAttachment":
                out.append(Finding("hold", "carries a file attached to the page", f"page {n}"))
            if "/A" in annot:
                for act, w in _actions(annot["/A"], f"page {n} {sub[1:] or 'annotation'}", seen):
                    _classify(act, w, out)
            for trigger, a in (_obj(annot.get("/AA")) or {}).items():
                for act, w in _actions(a, f"page {n} {sub[1:] or 'annotation'} event {trigger}", seen):
                    _classify(act, w, out)
    return out
