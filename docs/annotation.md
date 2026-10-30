# Annotation guide

The gold labels cover the four fields the PNCP API does not have. These rules exist so
that the same edital gets the same label on any day, from any annotator, and so that the
model is asked exactly what the annotator answered: the field descriptions in
`src/radar/schema.py` say the same thing.

## General rules

- **Only this PDF counts.** If the edital says "as defined in the Termo de Referência"
  and the terms of reference are not inside this PDF, the field is *not stated*.
  The model only sees this document, so the label must too.
- **Template text is not a rule.** Editais built on the federal AGU template keep
  generic clauses ("No item exclusivo para ME/EPP..."). A clause that only describes
  what would happen *if* something applied does not make it apply.
- **The edital prevails over its annexes** when both are in the PDF and disagree.
- **Record the page** that settles the answer, as printed by the tool (`p.13`), not
  the page number printed on the paper.
- When the answer is a judgement call, say why in the note.

## Fields

### Site visit (`site_visit`)

- **mandatory**: bidders must visit, or must present a certificate of visit, and cannot
  replace it with a declaration.
- **optional**: the visit is allowed (*facultativa*), or it can be replaced by a
  declaration that the bidder knows the site.
- **not mentioned**: no vistoria or visita técnica for bidders. A visit mentioned only as
  something the contractor does after signing does not count.

### Performance guarantee (`performance_guarantee_pct`)

- The percentage of the contract value required as *garantia de execução* or
  *garantia contratual* (5% → `5`).
- **none** when the edital says no guarantee is required, and also when it says nothing.
  The proposal guarantee (*garantia de proposta*) is a different thing and is ignored.

### Term in months (`contract_term_months`)

- For a price registration (SRP), the term of the *ata de registro de preços*.
  Otherwise, the initial term of the contract.
- Without extensions: "12 months, extendable up to 10 years" is `12`. One year is `12`.
- **not stated** when the term is only referred to another document.

### Consortium (`consortium_allowed`)

- **yes** when consortia may bid; **no** when the edital forbids them (*vedada a
  participação de consórcio*, or consortia listed among who cannot take part).
- **not mentioned** otherwise. A clause about companies "isoladamente ou em consórcio"
  that wrote the basic project is about conflicts of interest, not about whether
  consortia may bid; on its own it is *not mentioned*.

## Reviewing disagreements with the API

`radar review <run>` walks through every field where a model and a silver label
disagree and asks who is right, reading the edital. Each decision goes to
`annotations/overrides.jsonl`, including "the API was right", so it is not asked twice.

Unlike annotation, review shows the model's answer: it is a dispute between two
sources, and the reviewer needs both. That has a cost worth stating. Only disagreements
are reviewed, so a field where the model and the API are both wrong in the same way is
never looked at, and accuracy against silver labels is an upper bound. The gold fields
do not have this problem: they are annotated blind, for every notice in the set.
