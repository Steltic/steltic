# AISC Design Examples — quick index for `steel_design_examples` queries

For **every member and connection you design**, pair your `engineering_standards_A360`
(spec) query with a `steel_design_examples` query (`collection="steel_design_examples"`)
for the matching worked example below, and **mirror its check sequence — the method, not
its numbers.** Example hits return inline. Phrase the example query with the AISC clause +
member type, e.g. `"E3 W-shape column compression flexural buckling Pn worked example"`.

## Members
- **W-shape beam, major-axis flexure (Ch. F2):** F.1-1A/1B continuously braced;
  F.1-2A/2B braced at third points; F.1-3A/3B braced at midspan (Lp/Lr, Cb, LTB).
- **W-shape beam shear (Ch. G2):** G.1A / G.1B.
- **W-shape column compression (Ch. E3):** E.1A pinned, E.1B intermediate bracing,
  E.1C/E.1D available-strength calc, E.4A/E.4B moment-frame column.
- **Beam-column, combined axial + flexure (Ch. H1):** H.1A/H.1B (braced frame),
  H.2 (§H2 method), H.4 (compression + flexure); use the H1-1a/H1-1b interaction.
- **HSS brace/column compression (Ch. E):** E.9 rectangular HSS (no slender elements),
  E.10 rectangular HSS with slender elements, E.11 pipe.
- **Brace in tension (Ch. D):** D.4 rectangular HSS, D.5 round HSS, D.1 W-shape tension.
- **HSS flexure (Ch. F6-F8):** F.6 square HSS compact, F.7A/B noncompact, F.8A/B slender.
- **Composite floor (Ch. I), only if specified:** I.1 composite beam, I.2 composite girder.

## Connections (Manual Part II + Spec Ch. J / K)
- **Simple shear tab / single-plate** (typical gravity beam-to-column or -girder):
  II.A-17A conventional single-plate, II.A-18 to a girder web, II.A-19A extended.
- **Double-angle shear connection:** II.A-1A all-bolted, II.A-2A bolted/welded, II.A-3 welded.
- **FR moment connection** (the E-W moment frame): II.B-1 bolted flange-plate,
  II.B-2 welded flange-plate, II.B-3 directly-welded flange (prequalified detailing → A358).
- **Brace / gusset connections (CBF):** II.C-1, II.C-2 truss/bracing support connections.
- **Column base plate:** J.6 base plate bearing on concrete; HSS column base → K.9.
- **Weld / bolt building blocks (Ch. J):** J.1/J.2 welds, J.3 bolts in tension + shear.

## Building-scale method
- **Moment-frame stability by the Direct Analysis Method:** Example **C.1A** — the building-
  level recipe that III-1 Step 2 follows.
- The whole worked four-story building (method + answer key) is the **III-1 reference above**.
