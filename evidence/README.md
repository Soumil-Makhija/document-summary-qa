# Latest checkpoint — November 11, 2026

See [checkpoint-2026-11-11.md](checkpoint-2026-11-11.md) for the current tested increment and remaining work. The project remains in progress. New evaluation reports preserve both successful mechanical checks and failed/manual findings; they are development regressions, not accuracy benchmarks.

# Validation evidence

The November 9 build delivers model-free PDF import, source inspection, keyword search with physical-page links, and deletion through the browser, CLI, and local HTTP API.

- `browser-2026-11-09.json`: 13 real Chrome/HTTP workflow checks, without model inference. Source-file hashes identify the tested build; screenshots were inspected locally. `preview.png` is the synthetic workshop search screenshot used in the README.
- `installed-2026-11-09.json`: 6 checks against a freshly installed wheel, including packaged static files, CLI/HTTP search agreement, persisted pages, exact original bytes, and deletion.
- `model-review-2026-11-09.md`: manual comparison of genuine model outputs with the synthetic sources, including failures. The three referenced JSON reports are complete failed runs, not success claims or accuracy estimates.
- `live-initial.json` and `live-sandbox-blocked.json`: historical November 6 development checkpoints, retained separately from the completed November 9 runs.

Run `python -m unittest discover -s tests -v` and `node tools/test_ui_state.cjs` for deterministic regression checks. See the main README for fresh-wheel and real-model evaluation commands. The project remains in progress because its AI summaries and question answers do not yet meet the full regression criteria.
