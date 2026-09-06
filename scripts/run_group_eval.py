"""Run the group evaluation from a config file.

    python scripts/run_group_eval.py --config eval/configs/group.json
    python scripts/run_group_eval.py --config eval/configs/group.json --quick

Same discipline as run_eval.py: every result records the git SHA, the engine
version and a hash of the config, so a number in the README traces to a commit
and can be re-run.

The claim being tested is a trade, not a win. Average-score will often take the
mean; what CommonGround has to earn is a better floor and a fairer spread at an
acceptable cost to that mean -- so the table reports both, and the per-kind
breakdown shows where each strategy earns or loses it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from commonground_engine import ENGINE_VERSION, dataset, group_evaluate  # noqa: E402
from commonground_engine import groups as groups_module  # noqa: E402
from commonground_engine import split as split_module  # noqa: E402
from commonground_engine.group import MODES, ModeConfig  # noqa: E402
from commonground_engine.recommenders import (  # noqa: E402
    ALSRecommender,
    HybridRecommender,
    ItemKNN,
)

RESULTS_DIR = REPO_ROOT / "eval" / "results"


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def build_model(config: dict) -> HybridRecommender:
    """The M3 hybrid, at the weights its own sweep selected.

    Deliberately the same scorer the individual evaluation reported on. If the
    group layer were given a different underlying model, a difference in the
    group results could not be attributed to the group layer.
    """
    knn = config.get("k_neighbours", 200)
    als_params = config.get("als", {})
    weights = config.get("hybrid_weights", {"item-knn": 0.15, "als": 0.35})

    components = []
    for name, weight in weights.items():
        if weight == 0:
            continue
        if name == "item-knn":
            components.append((ItemKNN(k_neighbours=knn), weight))
        elif name == "als":
            components.append(
                (
                    ALSRecommender(
                        factors=als_params.get("factors", 64),
                        regularization=als_params.get("regularization", 0.05),
                        iterations=als_params.get("iterations", 20),
                        alpha=als_params.get("alpha", 40.0),
                        seed=config.get("seed", 0),
                    ),
                    weight,
                )
            )
        else:
            raise ValueError(f"unknown hybrid component {name!r}")
    return HybridRecommender(components=components, name="hybrid-fitted")


def load_item_artists(data, dataset_path: Path) -> dict[int, str]:
    items_path = dataset_path / "items.json"
    if not items_path.exists():
        return {}
    items = json.loads(items_path.read_text())
    return {index: item["artist"] for index, item in enumerate(items)}


def modes_from_config(config: dict) -> dict[str, ModeConfig]:
    """Documented presets, overridden by any fitted values in the config."""
    modes = dict(MODES)
    for name, overrides in (config.get("mode_overrides") or {}).items():
        if name not in modes:
            raise ValueError(f"unknown mode {name!r}")
        modes[name] = modes[name].with_params(**overrides)
    return modes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--quick", action="store_true", help="fewer groups")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text())
    name = config.get("name", Path(args.config).stem)
    seed = config.get("seed", 0)

    data_path = REPO_ROOT / config["dataset"]
    if not data_path.exists():
        print(f"dataset {data_path} not found", file=sys.stderr)
        return 2

    print(f"config     {args.config}  (hash {config_hash(config)})")
    data = dataset.load(data_path)
    print(f"dataset    {data.n_users:,} users, {data.n_items:,} items, {len(data):,} interactions")

    split_config = config.get("split", {})
    the_split = split_module.leave_last_n_out(data, **split_config)
    print(f"split      {len(the_split.train):,} train, {len(the_split.test):,} users evaluated")

    n_groups = config.get("n_groups", 200)
    if args.quick:
        n_groups = min(n_groups, 40)
    built = groups_module.build_groups(
        the_split.train,
        the_split.evaluated_users,
        n_groups=n_groups,
        sizes=tuple(config.get("group_sizes", [3, 4, 5, 6])),
        seed=seed,
    )
    summary = groups_module.summarise(built)
    print(f"\ngroups     {len(built)} across {len(summary)} kinds")
    for kind, stats in summary.items():
        print(
            f"  {kind:14} {stats['groups']:>4} groups, mean size {stats['mean_size']:.1f}, "
            f"cohesion {stats['mean_cohesion']:.4f}"
        )

    model = build_model(config)
    print("\nfitting the underlying hybrid...")
    started = time.perf_counter()
    model.fit(the_split.train)
    print(f"  {time.perf_counter() - started:.1f}s")

    modes = modes_from_config(config)
    item_artists = load_item_artists(data, data_path)

    print(f"\nevaluating {len(modes) + 3} strategies on {len(built)} groups\n")
    started = time.perf_counter()
    results = group_evaluate.evaluate_groups(
        model,
        data,
        the_split,
        built,
        modes=modes,
        k=config.get("k", 20),
        per_member_candidates=config.get("per_member_candidates", 200),
        item_artists=item_artists,
        seed=seed,
    )
    total = time.perf_counter() - started

    print(group_evaluate.summary_table(results))

    # Paired bootstrap against the baseline this project claims to improve on.
    # Printed next to the table on purpose: a mean difference without an
    # interval invites reading noise as a result, which is exactly what happened
    # to the first version of the M4 write-up.
    comparisons = {}
    print("\nconsensus vs average-score, paired bootstrap over the same groups (95% CI):\n")
    print("| metric | difference | 95% CI | distinguishable from zero? |")
    print("| --- | --- | --- | --- |")
    for metric in (
        "held_mean",
        "held_served",
        "proxy_min",
        "veto_violations",
        "max_artist_share",
    ):
        try:
            comparison = group_evaluate.paired_difference(
                results, metric, "consensus", "average-score", seed=seed
            )
        except KeyError:
            continue
        comparisons[metric] = comparison
        verdict = "**yes**" if comparison["significant"] else "no"
        print(
            f"| {metric} | {comparison['mean_difference']:+.4f} | "
            f"[{comparison['ci_low']:+.4f}, {comparison['ci_high']:+.4f}] | {verdict} |"
        )

    print(f"\ntotal {total:.1f}s")

    record = {
        "name": name,
        "config": config,
        "config_hash": config_hash(config),
        "git_sha": git_sha(),
        "engine_version": ENGINE_VERSION,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": f"{platform.system()} {platform.machine()} python {platform.python_version()}",
        "dataset": {
            "path": config["dataset"],
            "users": data.n_users,
            "items": data.n_items,
            "interactions": len(data),
        },
        "groups": {"total": len(built), "by_kind": summary},
        "modes": {
            mode_name: {
                "alpha": mode.alpha,
                "tau_veto": mode.tau_veto,
                "lambda_div": mode.lambda_div,
                "lambda_rep": mode.lambda_rep,
                "lambda_nov": mode.lambda_nov,
                "lambda_fair": mode.lambda_fair,
            }
            for mode_name, mode in modes.items()
        },
        "k": config.get("k", 20),
        "total_seconds": round(total, 2),
        "comparisons": {
            "consensus_vs_average_score": comparisons,
            "method": (
                "Paired bootstrap over the same groups, 10,000 resamples, 95% "
                "percentile interval. An interval containing zero means the two "
                "strategies are not distinguishable on that metric at this sample size."
            ),
        },
        "results": [result.as_dict() for result in results],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"{name}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
