"""Print a real group playlist with its generated reasons.

    python scripts/demo_explanations.py --config eval/configs/group.json

Not a test -- the tests already assert clause/contribution correspondence. This
exists so a human can read the sentences and judge whether they are worth
showing to a user, which no assertion can decide.

Everything printed comes from the same code path the evaluation uses: the same
model, the same candidate construction, the same ranker. Nothing here is staged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from commonground_api.imports.matching import normalise  # noqa: E402
from commonground_engine import dataset, group_evaluate  # noqa: E402
from commonground_engine import groups as groups_module  # noqa: E402
from commonground_engine import split as split_module  # noqa: E402
from commonground_engine.explain import explain  # noqa: E402
from commonground_engine.group import select  # noqa: E402
from run_group_eval import build_model, load_item_artists, modes_from_config  # noqa: E402

# Stand-in display names. The engine never sees these; they exist so the printed
# sentences read like the product rather than like array indices.
NAMES = ["Alex", "Sam", "Rio", "Jules", "Nina", "Theo"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="eval/configs/group.json")
    parser.add_argument("--mode", default="consensus")
    parser.add_argument("--kind", default="adversarial", help="which group kind to show")
    parser.add_argument("--tracks", type=int, default=8)
    args = parser.parse_args(argv)

    config = json.loads((REPO_ROOT / args.config).read_text())
    data_path = REPO_ROOT / config["dataset"]
    data = dataset.load(data_path)
    seed = config.get("seed", 0)

    the_split = split_module.leave_last_n_out(data, **config.get("split", {}))
    built = groups_module.build_groups(
        the_split.train,
        the_split.evaluated_users,
        n_groups=40,
        sizes=tuple(config.get("group_sizes", [3, 4, 5])),
        seed=seed,
    )
    chosen = next(g for g in built if g.kind == args.kind)

    model = build_model(config)
    model.fit(the_split.train)

    item_artists = load_item_artists(data, data_path)
    popularity = the_split.train.item_popularity()
    seen = the_split.train.seen_by_user()

    members = [m for m in chosen.members if m in the_split.test]
    candidates = group_evaluate.build_candidates(
        model, members, seen, per_member=200, popularity=popularity
    )
    scores = group_evaluate.score_members(model, members, candidates)

    tags_path = data_path / "artist_tags.json"
    artist_tags: dict[str, list[str]] = {}
    if tags_path.exists():
        artist_tags = {
            key: value["genres"]
            for key, value in json.loads(tags_path.read_text())["artists"].items()
        }

    # Each member's genre vocabulary, from what they actually played.
    member_genres: list[set[str]] = []
    for member in members:
        genres: set[str] = set()
        for item in seen[member]:
            artist = item_artists.get(item)
            if artist:
                genres.update(artist_tags.get(normalise(artist), []))
        member_genres.append(genres)

    mode = modes_from_config(config)[args.mode]
    novelty = group_evaluate.group_module.rank_percentile(-popularity[candidates][None, :])[0]
    playlist = select(
        scores, mode, args.tracks, item_artists=item_artists, novelty=novelty, seed=seed
    )

    names = NAMES[: len(members)]
    print(
        f"Room of {len(members)} ({chosen.kind}, cohesion {chosen.cohesion:.4f}) — {args.mode} mode"
    )
    print(f"{len(candidates):,} candidates, {playlist.n_vetoed:,} vetoed\n")

    for track in playlist.tracks:
        label = data.item_labels[track.candidate_id] if data.item_labels else "?"
        artist = item_artists.get(track.candidate_id, "")
        genres = artist_tags.get(normalise(artist), []) if artist else []
        familiar = [track.candidate_id in seen[m] for m in members]

        result = explain(
            track,
            member_names=names,
            item_genres=genres,
            member_genres=member_genres,
            familiar=familiar,
            tau_veto=mode.tau_veto,
        )
        scores_text = "  ".join(
            f"{n}={s:.2f}" for n, s in zip(names, track.member_scores, strict=True)
        )
        print(f"{track.position + 1:>2}. {label[:66]}")
        print(f"    {result.sentence}")
        print(f"    {scores_text}")
        print(f"    terms: {', '.join(f'{k}={v:+.3f}' for k, v in track.contributions.items())}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
