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
   CHROMATISATION), but one set must use one spelling (rule 21).
6. Brand and trade names, shop abbreviations (QTY, NOS, CSK, THRU, SWG, TYP,
   REF, A/R), standards (ASTM, MIL, IPC, AMS, IS) and part numbers are not
   misspellings.
7. A missing title-block field is never dropped. The drawing-type
   exemptions (for example, an electrical schematic needs no weight) are
   already applied by the code; any missing field you are shown is still
   needed in some cases, so report it and let the engineer decide. A value
   given as SEE NOTE or SEE TABLE is complete if that note or table exists.
8. On a sheet read by OCR, never make an item **must** unless it was
   confirmed by re-reading. Otherwise it is **look** and says that OCR may
   have misread it.
9. A note that names an item number must name the right kind of item for
   what the note says (an adhesive for bonding, wire for tying). A clear
   mismatch is **must**; a doubtful one is **look**.
10. Word every finding for a busy engineer: what is wrong, where, and what
    it should be, in at most 30 words. No apologies, no hedging beyond one
    "check" when unsure.
11. A drawing number with "E" (for example AB-CD 10-E-05) is an external
    drawing. Model files for machining (STEP, DXF, RAR) are named without
    the "E-": USE FILE AB-CD 10-01.STEP is correct for drawing AB-CD 10-E-01.
12. Values such as 135±10 g are value ± tolerance. They are correct. The code
    has already checked the ± on the image.
13. Resins and adhesives (epoxy, Araldite, RTV) are used for wetting, potting
    and bonding. A note that wets or bonds with a resin fits that item.
14. A note on an assembly may name a part that sits in one of its
    sub-assemblies. Before reporting a part as missing, look at the
    sub-assembly parts in the sheet context. If it is there, do not report it.
15. A note must use the exact BOM name of the part (CLAMP-B, not "clamp").
    A different name is **look**, and the finding gives the exact BOM name.
16. BOM rows that refer to different drawings are different parts, even if
    their names are alike (PCB-A and PCB ASSEMBLY). Never report them as
    duplicates or inconsistent.
17. A BOM name must match the title of the drawing it refers to. The sheet
    context says whether it matches. If it matches, do not report it; if it
    does not, it is **look**.
18. A BOM row may hold a multi-part system (a paint system: primer, top
    coat; a two-part adhesive). That is normal.
19. SEE TABLE means the BOM table or a specification table. Specification
    tables are found mainly on installation (INS) drawings.
20. Dates are written DD/MM/YYYY. Today's date is given in the sheet context;
    only a date after today is in the future. Check that the dates agree:
    the title-block date with the current revision in the revision table,
    and across the drawings of one product.
21. The same term spelt two ways in the set (CHROMATISATION / CHROMATIZATION)
    is **must**: QA always flags it.

