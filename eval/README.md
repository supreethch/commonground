# Experiments

`eval/configs/*.yaml` — one file per experiment: dataset slice, split rule,
seeds, model parameters.
`eval/results/*.json` — one file per run: metrics, config hash, git SHA, timings.

`scripts/run_eval.py --config eval/configs/<name>.yaml` is the only way results
are produced, so every number can be traced to a commit and re-run. A result
whose config hash does not match its config file is stale.

Empty until M3. The protocol it will follow is in
[docs/evaluation.md](../docs/evaluation.md), written before any numbers exist so
the metrics and baselines cannot be chosen afterwards to flatter the outcome.
