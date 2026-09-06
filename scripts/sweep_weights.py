"""Grid-search hybrid weights on a validation split.

    python scripts/sweep_weights.py --config eval/configs/individual.json

docs/recommender.md promises the hybrid weights are fitted rather than hand-tuned
until the demo looks good. This is that fit, and the important part is *where* it
happens.

## The nested split, and why it is not optional

Fitting weights on the test split and then reporting scores from that same split
is the most common way an offline recommender result is silently inflated: the
weights have seen the answers. So the data is split twice:

    all interactions
      └─ leave-last-5-out ──▶ TEST        (touched only by run_eval.py)
           └─ leave-last-5-out ──▶ VALIDATION  (the weights are fitted here)
                └─ inner train

The sweep never sees the test split. The winning weights go into the config, and
run_eval.py then reports them against test data the search never touched.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "api"))

from commonground_engine import dataset, evaluate, split  # noqa: E402
from commonground_engine.recommenders import (  # noqa: E402
    ALSRecommender,
    ContentKNN,
    HybridRecommender,
    ItemKNN,
    PopularityRecommender,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "eval" / "results"

# Coarse on purpose. A fine grid over four weights on 1,400 validation users
# would take far longer and overfit the validation split, which is the same
# mistake one level down.
GRID = (0.0, 0.15, 0.35, 0.5, 0.65)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--max-users", type=int, default=400)
    parser.add_argument("--metric", default="ndcg@10")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text())
    data = dataset.load(REPO_ROOT / config["dataset"])
    seed = config.get("seed", 0)
    knn = config.get("k_neighbours", 200)
    als_params = config.get("als", {})

    outer = split.leave_last_n_out(data, **config.get("split", {}))
    inner = split.leave_last_n_out(outer.train, **config.get("split", {}))
    print(
        f"validation split: {len(inner.train):,} train interactions, "
        f"{len(inner.test):,} users\n"
        f"(the test split of {len(outer.test):,} users is untouched here)"
    )

    tags: dict[str, list[str]] = {}
    try:
        from run_eval import artist_tags_from_database

        tags = artist_tags_from_database()
    except Exception:  # noqa: BLE001 - features degrade to artist-only
        pass
    features = (
        evaluate.item_features_from_artists(data, tags) if data.item_artists is not None else None
    )

    def make(component: str):
        if component == "item-knn":
            return ItemKNN(k_neighbours=knn)
        if component == "als":
            return ALSRecommender(
                factors=als_params.get("factors", 64),
                regularization=als_params.get("regularization", 0.05),
                iterations=als_params.get("iterations", 20),
                alpha=als_params.get("alpha", 40.0),
                seed=seed,
            )
        if component == "content-knn":
            return ContentKNN(features=features, k_neighbours=knn)
        if component == "popularity":
            return PopularityRecommender()
        raise ValueError(component)

    names = ["item-knn", "als", "content-knn", "popularity"]

    # Components are fitted once and reused across every weight combination.
    # Refitting ALS inside the loop would dominate the runtime and measure
    # nothing extra -- the weights change, the models do not.
    print("\nfitting components once...")
    fitted = {}
    for name in names:
        if name == "content-knn" and features is None:
            continue
        started = time.perf_counter()
        fitted[name] = make(name).fit(inner.train)
        print(f"  {name:12} {time.perf_counter() - started:.1f}s")

    combinations = [
        weights for weights in itertools.product(GRID, repeat=len(fitted)) if sum(weights) > 0
    ]
    print(f"\n{len(combinations)} weight combinations on {args.max_users} users\n")

    rows = []
    started = time.perf_counter()
    for index, weights in enumerate(combinations, start=1):
        components = [
            (model, weight)
            for (name, model), weight in zip(fitted.items(), weights, strict=True)
            if weight > 0
        ]
        hybrid = HybridRecommender(components=components, name="sweep")
        # Already fitted; HybridRecommender.fit would refit each component.
        result = evaluate.evaluate(
            _AlreadyFitted(hybrid), inner, ks=(10,), max_users=args.max_users, seed=seed
        )
        rows.append(
            {
                "weights": dict(zip(fitted, weights, strict=True)),
                "metrics": {k: round(v, 6) for k, v in result.scores.items()},
            }
        )
        if index % 100 == 0:
            print(f"  {index}/{len(combinations)}  ({time.perf_counter() - started:.0f}s)")

    rows.sort(key=lambda row: -row["metrics"][args.metric])
    print(f"\ntop 8 by {args.metric}:\n")
    header = " | ".join(f"{name:>11}" for name in fitted)
    print(f"| {header} | {args.metric} | P@10 | coverage@10 |")
    print("| " + " | ".join("---" for _ in range(len(fitted) + 3)) + " |")
    for row in rows[:8]:
        cells = " | ".join(f"{row['weights'][name]:>11.2f}" for name in fitted)
        print(
            f"| {cells} | {row['metrics'][args.metric]:.4f} | "
            f"{row['metrics']['precision@10']:.4f} | {row['metrics']['coverage@10']:.4f} |"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "weight-sweep.json"
    out.write_text(
        json.dumps(
            {
                "selected_on": args.metric,
                "note": (
                    "Fitted on a validation split nested inside the training data. "
                    "The test split used by run_eval.py was not seen here."
                ),
                "grid": list(GRID),
                "validation_users": len(inner.test),
                "users_evaluated": args.max_users,
                "combinations_evaluated": len(rows),
                "best": rows[0],
                # Only the top slice is kept. The full grid is 600+ rows of
                # mostly-identical JSON, and re-running the script regenerates
                # it -- a repository is not a results warehouse.
                "top": rows[:25],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nbest: {rows[0]['weights']}")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


class _AlreadyFitted:
    """Adapter so evaluate() can score a hybrid whose components are fitted.

    evaluate() calls fit() as part of its contract -- that is what keeps every
    model measured by identical code -- so the sweep needs a way to say "these
    are already trained" without weakening that contract for everyone else.
    """

    def __init__(self, model):
        self._model = model
        self.name = model.name

    def fit(self, data):
        return self

    def score_all(self, user):
        return self._model.score_all(user)

    def recommend(self, user, k, exclude=None):
        return self._model.recommend(user, k, exclude)


if __name__ == "__main__":
    raise SystemExit(main())
