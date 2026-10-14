# QC policy

The model reads this document before it decides what to report, how serious
it is, and how to word it. Edit it in plain English to change the agent's
behaviour; no code changes are needed. Keep each rule short and concrete.

## Purpose
Catch, before the first print, the mistakes QA would otherwise find in the
literal parts of a drawing: names, spelling, references, notes, title block,
BOM. Views and geometry are not checked.

## Severity
- **must**: a reader of the drawing would make, buy or assemble the wrong
  thing, or could not find a referenced drawing or file. Wrong or misspelt
  drawing numbers, references to items that do not exist, missing component
  names, missing quantity, a missing mandatory title-block field, a real
  misspelling of a technical word.
- **look**: inconsistent but understandable. The same word spelt two ways in
  one product, wording differences, things a person should confirm (jig
  numbers, drawings not supplied in the folder).
- **drop**: correct as drawn, or not worth a reviewer's time.

## Rules
1. A drawing number must be written exactly as the drawing is numbered:
   same prefix, same category letter, same leading zeros. Any difference is
   **must**, even if a person would guess the right drawing.
2. A BOM line for a standard or bought-out part (screws, washers, adhesives,
   wire, connectors, vendor part numbers) needs no drawing. Never report it
   as missing.
3. A reference to a drawing that is simply not in the folder is **look**:
   the set may be incomplete. Group them per sheet.
4. Jig, mould, fixture and template numbers are listed for the person to
   confirm (**look**), never reported as errors.
5. British and American spelling are both correct (FIBRE, ALUMINIUM,
   CHROMATISATION), but one product should use one spelling: **look**.
6. Brand and trade names, shop abbreviations (QTY, NOS, CSK, THRU, SWG, TYP,
   REF, A/R), standards (ASTM, MIL, IPC, AMS, IS) and part numbers are not
   misspellings.
7. A schematic needs no material, weight or scale. An installation drawing
   needs no material. A drawing that gives a value as SEE NOTE or SEE TABLE is
   complete if that note or table exists.
8. On a sheet read by OCR, never make an item **must** unless it was
   confirmed by re-reading. Otherwise it is **look** and says that OCR may
   have misread it.
9. A note that names an item number must name the right kind of item for
   what the note says (an adhesive for bonding, wire for tying). A clear
   mismatch is **must**; a doubtful one is **look**.
10. Word every finding for a busy engineer: what is wrong, where, and what
    it should be, in at most 30 words. No apologies, no hedging beyond one
    "check" when unsure.
