from __future__ import annotations

import io
import zipfile

import pytest

from radar import archive
from radar.archive import Edital, find_edital

PDF = b"%PDF-1.4 the edital"


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buffer.getvalue()


def test_a_pdf_is_the_edital_itself():
    assert find_edital("Edital 49.2026.pdf", PDF) == Edital(PDF, "Edital 49.2026.pdf")


# File names taken from the ZIPs in the first sample of Rio de Janeiro tenders.
@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (
            [
                "Edital e Anexos/Anexo I - Termo de Referência.pdf",
                "Edital e Anexos/Anexo IV – Planilha de Custos.xlsx",  # noqa: RUF001 (real name)
                "Edital e Anexos/Edital 468.2026 e Anexos.pdf",
            ],
            "Edital 468.2026 e Anexos.pdf",
        ),
        (
            [
                "1. Edital 407.2025.pdf",
                "2. TERMO DE REFERÊNCIA.pdf",
                "4. Anexo II ao edital - Modelo de proposta de preços.pdf",
            ],
            "1. Edital 407.2025.pdf",
        ),
        (
            [
                "001_EDITAL_765720_000123_2026_1.pdf",
                "004_0_Anexo_II_do_Edital_Termo_de_Ciencia-Concordancia.pdf",
                "005_1_Anexo_V_do_Edital_MOD_PROP_LICIT.pdf",
            ],
            "001_EDITAL_765720_000123_2026_1.pdf",
        ),
        (
            [
                "SEI_65947490_RCC_3.0___Termo_de_Referencia___SRP_PPS.pdf",
                "SEI_66021190_CONJUR___Edital_Pregao_Bens___RCC_30_edited.pdf",
            ],
            "SEI_66021190_CONJUR___Edital_Pregao_Bens___RCC_30_edited.pdf",
        ),
        (
            ["3. Apêndice do Anexo I ao Edital.pdf", "Edital.pdf"],
            "Edital.pdf",
        ),
    ],
)
def test_picks_the_edital_among_the_annexes(names, expected):
    found = find_edital("Edital.zip", make_zip({name: PDF for name in names}))
    assert found == Edital(PDF, f"Edital.zip/{expected}")


def test_pregao_in_the_name_also_marks_the_edital():
    found = find_edital(
        "PE 135-2026.zip",
        make_zip(
            {
                "Pregão Eletronico Proc 867-2026 Aquisição de playground.pdf": PDF,
                "Anexo II - Proc 867-2026 Aquisição de playground.pdf": PDF,
                "ANEXO III - MODELO DE PROPOSTA - PREGÃO ELETRÔNICO Nº 071-2026.pdf": PDF,
            }
        ),
    )
    assert isinstance(found, Edital)
    assert found.title.endswith("/Pregão Eletronico Proc 867-2026 Aquisição de playground.pdf")


def test_a_single_pdf_that_is_not_an_annex_is_the_edital():
    found = find_edital("editais.zip", make_zip({"2026.0008 - SERVIÇOS GRAFICOS.pdf": PDF}))
    assert found == Edital(PDF, "editais.zip/2026.0008 - SERVIÇOS GRAFICOS.pdf")

    only_annex = make_zip({"002_anexo I_termo de referencia_SEI_00255797.pdf": PDF})
    assert find_edital("editais.zip", only_annex) == "no edital inside the ZIP"


def test_an_edital_in_word_inside_the_zip_is_reported_as_such():
    upload = make_zip(
        {
            "Edital - PE 071-26 - Aquisição de Pneus.doc": b"\xd0\xcf\x11\xe0",
            "Anexo I - TR - PE 071-26.pdf": PDF,
        }
    )
    assert find_edital("editais.zip", upload) == "edital inside the ZIP is a Word document"


def test_prefers_the_complete_edital_over_the_draft_and_the_short_one():
    found = find_edital(
        "EDITAL.zip",
        make_zip(
            {
                "1. Minuta Edital 26 2026.pdf": PDF + b" draft" * 100,
                "Edital 26 2026.pdf": PDF,
                "Edital 26 2026_COMPLETO.pdf": PDF + b" with every annex",
            }
        ),
    )
    assert isinstance(found, Edital)
    assert found.title == "EDITAL.zip/Edital 26 2026_COMPLETO.pdf"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"7z\xbc\xaf'\x1c\x00\x03", "edital is not a PDF or a ZIP"),
        (b"PK\x03\x04 truncated", "edital is a corrupt ZIP"),
        (make_zip({"Termo de Referencia.pdf": PDF}), "no edital inside the ZIP"),
        (make_zip({"Edital.pdf": b"<html>not found</html>"}), "edital inside the ZIP is not a PDF"),
    ],
)
def test_reports_why_no_edital_was_found(data, reason):
    assert find_edital("upload", data) == reason


def test_opens_a_zip_inside_a_zip_once():
    inner = make_zip({"Edital - PE 071-26.pdf": PDF})
    found = find_edital("editais.zip", make_zip({"Edital - PE 071-26.zip": inner}))
    assert found == Edital(PDF, "editais.zip/Edital - PE 071-26.zip/Edital - PE 071-26.pdf")

    twice = make_zip({"a.zip": make_zip({"b.zip": inner})})
    assert find_edital("editais.zip", twice) == "no edital inside the ZIP"


def test_word_documents_are_reported_as_such():
    docx = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
    assert find_edital("EDITAL", docx) == "edital is a Word document"


def test_refuses_a_member_larger_than_the_cap(monkeypatch):
    monkeypatch.setattr(archive, "MAX_PDF_BYTES", 10)
    assert find_edital("Edital.zip", make_zip({"Edital.pdf": PDF})) == (
        "edital inside the ZIP is too large"
    )


def test_restores_accents_of_names_written_in_cp850():
    # What zipfile hands back for a name written by Windows without the UTF-8 flag.
    info = zipfile.ZipInfo("Edital  Contratação 842026/Edital.pdf".encode("cp850").decode("cp437"))
    info.flag_bits = 0
    assert archive._name(info) == "Edital.pdf"

    info = zipfile.ZipInfo("Contratação 842026.pdf".encode("cp850").decode("cp437"))
    info.flag_bits = 0
    assert archive._name(info) == "Contratação 842026.pdf"


def test_keeps_names_that_were_not_decoded_as_cp437():
    # Found in the wild: no UTF-8 flag, yet the name is already correct.
    info = zipfile.ZipInfo("Licitação.pdf")
    info.flag_bits = 0
    assert archive._name(info) == "Licitação.pdf"
