"""Fit the mode parameters on validation groups.

    python scripts/sweep_modes.py --config eval/configs/group.json

## The nested split, again

Groups are built from the *training* split's own held-out data, so the test
groups run_group_eval.py reports on are never seen here. Same discipline as
M3's weight sweep: a parameter fitted on the split it is then scored against
has seen the answers.

## Why this is not a single-objective search

Fitting every mode on held-out mean would drive all three to the same place --
alpha=1, tau=0, no penalties, which is exactly the average-score baseline. The
search would "discover" that the way to maximise the mean is to stop caring
about the floor, delete the features the modes exist for, and report a win.

So each mode is fitted on the objective it actually exists to serve:

    consensus      balance: mean of (held_mean rank, proxy_min rank)
    discovery      novelty, with held_mean required to stay within 75% of best
    fair_rotation  proxy_min, the least-satisfied member

and the full alpha/tau grid is written out, so the trade-off curve can be read
rather than taken on trust.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from commonground_engine import dataset, group_evaluate  # noqa: E402
from commonground_engine import groups as groups_module  # noqa: E402
from commonground_engine import split as split_module  # noqa: E402
from commonground_engine.group import MODES, ModeConfig  # noqa: E402
from run_group_eval import build_model, config_hash, load_item_artists  # noqa: E402

RESULTS_DIR = REPO_ROOT / "eval" / "results"

ALPHA_GRID = (0.0, 0.3, 0.5, 0.7, 1.0)
TAU_GRID = (0.0, 0.25, 0.35, 0.45)
FAIR_GRID = (0.0, 0.25, 0.45, 0.7)
NOV_GRID = (0.0, 0.15, 0.35, 0.6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--n-groups", type=int, default=80)
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text())
    seed = config.get("seed", 0)
    data_path = REPO_ROOT / config["dataset"]
    data = dataset.load(data_path)

    outer = split_module.leave_last_n_out(data, **config.get("split", {}))
    inner = split_module.leave_last_n_out(outer.train, **config.get("split", {}))
    print(
        f"validation: {len(inner.train):,} train interactions, {len(inner.test):,} users\n"
        f"(the {len(outer.test):,} test users run_group_eval.py reports on are untouched)"
    )

    validation_groups = groups_module.build_groups(
        inner.train,
        inner.evaluated_users,
        n_groups=args.n_groups,
        sizes=tuple(config.get("group_sizes", [3, 4, 5, 6])),
        seed=seed + 1,
    )
    print(f"{len(validation_groups)} validation groups")

    model = build_model(config)
    print("\nfitting the underlying hybrid on the inner split...")
    started = time.perf_counter()
    model.fit(inner.train)
    print(f"  {time.perf_counter() - started:.1f}s")

    item_artists = load_item_artists(data, data_path)

    # One combined grid, evaluated once, then read three different ways. Running
    # three separate searches would triple the cost for identical arithmetic.
    combinations = [
        (alpha, tau, fair, nov)
        for alpha, tau, fair, nov in itertools.product(ALPHA_GRID, TAU_GRID, FAIR_GRID, NOV_GRID)
    ]
    print(f"\n{len(combinations)} parameter combinations on {len(validation_groups)} groups\n")

    rows = []
    started = time.perf_counter()
    for index, (alpha, tau, fair, nov) in enumerate(combinations, start=1):
        candidate = ModeConfig(
            name="sweep",
            alpha=alpha,
            tau_veto=tau,
            lambda_div=0.15,
            lambda_rep=0.25,
            lambda_nov=nov,
            lambda_fair=fair,
        )
        results = group_evaluate.evaluate_groups(
            model,
            data,
            inner,
            validation_groups,
            modes={"sweep": candidate},
            k=config.get("k", 20),
            per_member_candidates=config.get("per_member_candidates", 200),
            item_artists=item_artists,
            seed=seed,
            include_baselines=False,
        )
        rows.append(
            {
                "params": {
                    "alpha": alpha,
                    "tau_veto": tau,
                    "lambda_fair": fair,
                    "lambda_nov": nov,
                },
                "metrics": {k: round(v, 6) for k, v in results[0].overall.items()},
            }
        )
        if index % 40 == 0:
            print(f"  {index}/{len(combinations)}  ({time.perf_counter() - started:.0f}s)")

    # --- select per mode, on the objective each mode exists for --------------
    held_means = np.array([r["metrics"]["held_mean"] for r in rows])
    proxy_mins = np.array([r["metrics"]["proxy_min"] for r in rows])
    novelties = np.array([r["metrics"].get("novelty", 0.0) for r in rows])

    def normalise(values: np.ndarray) -> np.ndarray:
        span = values.max() - values.min()
        return (values - values.min()) / span if span > 0 else np.zeros_like(values)

    balance = (normalise(held_means) + normalise(proxy_mins)) / 2
    consensus_index = int(np.argmax(balance))
    fair_index = int(np.argmax(proxy_mins))

    # Discovery may spend accuracy on novelty, but not without limit.
    floor = 0.75 * held_means.max()
    eligible = np.flatnonzero(held_means >= floor)
    discovery_index = (
        int(eligible[np.argmax(novelties[eligible])])
        if len(eligible)
        else int(np.argmax(novelties))
    )

    selection = {
        "consensus": rows[consensus_index],
        "discovery": rows[discovery_index],
        "fair_rotation": rows[fair_index],
    }

    print("\nfitted parameters (validation):\n")
    print("| mode | alpha | tau | l_fair | l_nov | held_mean | proxy_min | novelty |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for mode_name, row in selection.items():
        p, m = row["params"], row["metrics"]
        print(
            f"| {mode_name} | {p['alpha']:.2f} | {p['tau_veto']:.2f} | {p['lambda_fair']:.2f} | "
            f"{p['lambda_nov']:.2f} | {m['held_mean']:.4f} | {m['proxy_min']:.4f} | "
            f"{m.get('novelty', 0):.2f} |"
        )

    print("\ndocumented defaults, for comparison:\n")
    for mode_name, mode in MODES.items():
        print(
            f"  {mode_name:14} alpha={mode.alpha:.2f} tau={mode.tau_veto:.2f} "
            f"l_fair={mode.lambda_fair:.2f} l_nov={mode.lambda_nov:.2f}"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "mode-sweep.json"
    out.write_text(
        json.dumps(
            {
                "config_hash": config_hash(config),
                "note": (
                    "Fitted on validation groups built from a split nested inside "
                    "the training data. The test groups were not seen here. Each "
                    "mode is selected on the objective it exists for, because a "
                    "single held-out-mean objective would collapse all three onto "
                    "the average-score baseline."
                ),
                "objectives": {
                    "consensus": "mean of normalised held_mean and proxy_min",
                    "discovery": "novelty subject to held_mean >= 75% of best",
                    "fair_rotation": "proxy_min",
                },
                "grid": {
                    "alpha": list(ALPHA_GRID),
                    "tau_veto": list(TAU_GRID),
                    "lambda_fair": list(FAIR_GRID),
                    "lambda_nov": list(NOV_GRID),
                },
                "validation_groups": len(validation_groups),
                "combinations_evaluated": len(rows),
                "selected": {name: row for name, row in selection.items()},
                "documented_defaults": {
                    name: {
                        "alpha": mode.alpha,
                        "tau_veto": mode.tau_veto,
                        "lambda_fair": mode.lambda_fair,
                        "lambda_nov": mode.lambda_nov,
                    }
                    for name, mode in MODES.items()
                },
                # The full curve, so the trade-off can be read rather than trusted.
                "all": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
