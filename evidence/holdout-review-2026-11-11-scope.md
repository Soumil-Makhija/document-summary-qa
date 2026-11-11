# Targeted shared-context regression review - November 11, 2026

The final shared-context candidate **failed one of two affected frozen compound cases**: 27/30 mechanical assertions passed. This is a targeted regression on previously exposed synthetic material, not a complete new evaluation or untouched benchmark. The complete 3B first pass and complete 4B baseline remain unchanged in their separate reports.

Codex compared both public answers, every selected quotation, physical-page link, and model diagnostic trace with the original rendered fixture and the unchanged manifest. The default-model browser test has not been started while final summary validation and this QA regression remain unresolved.

- Model: local `qwen3:4b-instruct`.
- Run: 2026-11-11T17:27:44.876871+00:00 to 2026-11-11T17:29:56.271511+00:00 (131.406 seconds).
- Selection code SHA-256 captured before inference: `15deeb2b2fbc4769676c4fc800312df9b4d9adc8e09cf3b02f26bec4b9a49668`.
- Runner selection: `--skip-summary --case compound-save-and-retention --case compound-one-missing`.
- Summary generation was deliberately not rerun; the summary algorithm was outside this targeted change.

| Frozen case | Result | Source comparison |
| --- | --- | --- |
| `compound-save-and-retention` | Pass | Correctly returns the Records > Save copy > PNG procedure and 37-calendar-day archive lifetime, both from physical page 2. The second clause resolves its pronoun using the original question context. Quotes and links are exact. |
| `compound-one-missing` | **Fail against frozen contract** | Returns `supported=true` with the 37-day archive fact and the page-4 statement `This note sets no replacement charges or tax rates.` The frozen case expects abstention because no missing-cable replacement amount is specified. The final contextual call instead treats the no-specified-charge sentence as affirmative support for the fee question. |

The three failed assertions are `support-decision`, `unsupported-has-no-evidence`, and `explicit-abstention`. Every returned quote and original-page URL remains authentic, so this is a support/completeness decision failure against the frozen expectation rather than fabricated text or broken citation provenance.

## Wording ambiguity

The phrase "This note sets no replacement charges" is linguistically ambiguous: it can describe an absence of specified fees, or be interpreted as a policy imposing no charge. The frozen manifest intentionally treats the missing-cable amount as unstated and was not changed after observing the result. This ambiguity limits how strongly this one case supports a general claim of hallucination. The public response supplies only the quoted source sentence, without inventing a numeric amount, but its positive support decision does not meet the frozen full-question expectation. The ambiguity is disclosed rather than removing the failing case or silently reclassifying it as passed.

The diagnostic trace makes the changed decision explicit: it moves from "no replacement charge specified" to "the fact that there is no replacement charge is directly provided." The complete pre-context 4B baseline abstained on the same frozen case. Thus the targeted report preserves a changed outcome after the context payload change; it does not establish a universally deterministic effect on other documents or phrasings.

## Preserved artifacts

| Artifact | SHA-256 |
| --- | --- |
| Frozen manifest | `70c69e3dcd355c2384ab6b51e411d355de671a983a388ff20bb8e344f174728c` |
| Raw targeted report | `da8a770859c23a22fd4d30480dd9e8d170d43038aa0330d1a8f818dc188f808d` |

No prompts, cases, expected decisions, check conditions or application code were edited by the evaluator in response to this targeted result. The report retains the failing genuine output and trace.
