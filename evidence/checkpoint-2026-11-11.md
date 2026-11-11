# November 11 tested checkpoint

The user clarified that each day should deliver one small-to-medium tested push, with large projects split across days to conserve Codex usage. This checkpoint does not claim the entire PDF Desk project is complete.

Implemented extractive page summaries, immutable question clauses with shared original context, source-role filtering that does not discard facts merely because they share a heading, and the local Qwen3 4B Instruct default. Public model metadata is recorded in the local daily-run report; the app never downloads a model automatically.

The original 16 question cases pass. The final summary-only run covers all three synthetic PDFs and all 12 physical pages. Manual source comparison confirms the workshop refund policy, 18-day retention/export, usual repair tiers and 23-day rotation; fictional research sample/allocation/results/limitations; and Harborline renewal, 37-day archive/save procedure, return tiers, negative guarantee and 11-minute expiry. Hostile directive values are absent. Heading/footer noise remains on some pages.

Shared-context regressions pass the three original compound questions and five synthetic inherited-scope cases. The targeted Harborline rerun passes one of two cases: an ambiguous missing-fee clause is still incorrectly marked supported. The quoted text is authentic, but the completeness decision is too strong. Existing manifests and expectations are unchanged. Repeated fixtures are development regressions, not untouched accuracy benchmarks.

Next daily piece: improve conservative handling of ambiguous/unspecified values, run only relevant regressions, and verify the actual AI browser workflow. Preserve in-progress status until the remaining acceptance work is validated. No new model comparisons or broad repeated evaluations are needed for that piece.
