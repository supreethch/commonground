"""Run an offline evaluation from a config file.

    python scripts/run_eval.py --config eval/configs/individual.json
    python scripts/run_eval.py --config eval/configs/individual.json --quick

The only way results are produced. Every result records the git SHA and a hash
of the config that made it, so a number in the README can be traced to a commit
and re-run -- and a result whose config hash no longer matches its config file is
stale by definition rather than by argument.

JSON rather than YAML for configs: it needs no dependency, and a config format
that cannot be parsed by the standard library is a bad trade for quoting style.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "api"))

from commonground_engine import (  # noqa: E402
    ENGINE_VERSION,  # noqa: E402
    dataset,
    evaluate,
    split,
)
from commonground_engine.recommenders import (  # noqa: E402
    ALSRecommender,
    ContentKNN,
    HybridRecommender,
    ItemKNN,
    PopularityRecommender,
    RandomRecommender,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
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


def artist_tags_from_database() -> dict[str, list[str]]:
    """Genre tags per artist, from the catalogue built by build_catalog.py.

    Optional: with no database the content model falls back to artist identity
    alone, which every item has. The evaluation reports which of the two it
    used, because content-model numbers are not comparable across that line.
    """
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        return {}
    try:
        import psycopg
    except ImportError:
        return {}

    tags: dict[str, list[str]] = {}
    try:
        with psycopg.connect(url, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.name, t.name
                FROM artists a
                JOIN artist_tags at ON at.artist_id = a.id
                JOIN tags t ON t.id = at.tag_id
                WHERE t.is_genre
                """
            )
            for artist, tag in cur.fetchall():
                tags.setdefault(artist.casefold(), []).append(tag)
    except Exception as exc:  # noqa: BLE001 - absence is a fallback, not a failure
        print(f"  (no genre tags: {type(exc).__name__}); content model uses artists only")
        return {}
    return tags


def build_models(config: dict, features) -> dict:
    seed = config.get("seed", 0)
    knn = config.get("k_neighbours", 200)
    als_params = config.get("als", {})

    def als() -> ALSRecommender:
        return ALSRecommender(
            factors=als_params.get("factors", 64),
            regularization=als_params.get("regularization", 0.05),
            iterations=als_params.get("iterations", 20),
            alpha=als_params.get("alpha", 40.0),
            seed=seed,
        )

    models: dict = {
        "random": RandomRecommender(seed=seed),
        "popularity": PopularityRecommender(),
        "item-knn": ItemKNN(k_neighbours=knn),
        "als": als(),
    }
    if features is not None:
        models["content-knn"] = ContentKNN(features=features, k_neighbours=knn)

    for name, weights in config.get("hybrids", {}).items():
        components = []
        for component, weight in weights.items():
            if weight == 0:
                continue
            if component == "popularity":
                components.append((PopularityRecommender(), weight))
            elif component == "item-knn":
                components.append((ItemKNN(k_neighbours=knn), weight))
            elif component == "als":
                components.append((als(), weight))
            elif component == "content-knn":
                if features is None:
                    continue
                components.append((ContentKNN(features=features, k_neighbours=knn), weight))
            else:
                raise ValueError(f"unknown hybrid component {component!r}")
        if components:
            models[name] = HybridRecommender(components=components, name=name)

    return models


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--quick", action="store_true", help="evaluate fewer users")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text())
    name = config.get("name", Path(args.config).stem)

    data_path = REPO_ROOT / config["dataset"]
    if not data_path.exists():
        print(
            f"dataset {data_path} not found -- run scripts/build_dataset.py first",
            file=sys.stderr,
        )
        return 2

    print(f"config     {args.config}  (hash {config_hash(config)})")
    data = dataset.load(data_path)
    print(
        f"dataset    {data.n_users:,} users, {data.n_items:,} items, "
        f"{len(data):,} interactions, density {data.density:.4%}"
    )

    split_config = config.get("split", {})
    the_split = split.leave_last_n_out(
        data,
        n=split_config.get("n", 5),
        min_train_items=split_config.get("min_train_items", 5),
    )
    print(f"split      {len(the_split.train):,} train, {len(the_split.test):,} users evaluated")

    tags = artist_tags_from_database()
    features = None
    tagged_items = 0
    if data.item_artists is not None:
        features = evaluate.item_features_from_artists(data, tags)
        tagged_items = sum(1 for a in data.item_artists if a.casefold() in tags)
        print(
            f"features   {features.shape[1]:,} features; "
            f"{tagged_items:,}/{data.n_items:,} items have genre tags "
            f"({tagged_items / max(data.n_items, 1):.1%}), all have an artist"
        )

    models = build_models(config, features)
    max_users = config.get("max_eval_users")
    if args.quick:
        max_users = min(max_users or 10**9, 200)

    print(f"\nevaluating {len(models)} models on {max_users or len(the_split.test)} users\n")
    started = time.perf_counter()
    results = evaluate.compare(
        models,
        the_split,
        ks=tuple(config.get("ks", [5, 10, 20])),
        max_users=max_users,
        seed=config.get("seed", 0),
    )
    total = time.perf_counter() - started

    print(evaluate.summary_table(results, ks=tuple(config.get("report_ks", [10]))))
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
            "density": data.density,
            "items_with_genre_tags": tagged_items,
        },
        "split": {
            "train_interactions": len(the_split.train),
            "users_evaluated": len(the_split.test),
            **split_config,
        },
        "total_seconds": round(total, 2),
        "results": [result.as_dict() for result in results],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"{name}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
