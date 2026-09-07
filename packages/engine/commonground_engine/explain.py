"""Plain-English reasons, derived from the arithmetic that produced the ranking.

The rule that makes this worth reading: **a clause may only appear if the term
it describes actually moved the score.** Nothing here recomputes a similarity or
guesses at a motive; it renders `SelectedTrack.contributions`, which is the same
dictionary the ranker summed to pick the track. A test asserts the
correspondence in both directions.

That is the difference between an explanation and a caption. A caption can say
"because it's similar to Alex's favourites" about a track chosen for entirely
different reasons; this cannot, because "similar to Alex's favourites" is only
emitted when a named term with a non-zero value says so.

Group facts -- how many members have a genre, whose taste it is closest to --
are counted exactly from the inputs, not inferred from the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .group import SelectedTrack

# Terms the ranker can produce, and whether a clause is worth showing for each.
# A term absent from a track's contributions was not applied to that track.
# How close to the veto threshold counts as "only just". Above this the track
# cleared comfortably and saying so on every row is noise.
NARROW_MARGIN = 0.12

POSITIVE_TERMS = {"group_score", "novelty_bonus", "fairness_bonus", "popularity"}
NEGATIVE_TERMS = {"diversity_penalty", "repetition_penalty"}


@dataclass
class Explanation:
    """A rendered reason plus the evidence it was built from."""

    sentence: str
    clauses: list[str] = field(default_factory=list)
    # Which contribution key each clause came from, so a test (and a curious
    # reader) can check the mapping rather than trusting the prose.
    sources: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def _fmt_members(count: int, total: int) -> tuple[str, str]:
    """Subject and its verb, so the clause agrees grammatically.

    "everyone likes" but "3 members like" -- returning the verb alongside the
    subject is cheaper than a template per case, and an earlier version that
    hardcoded "like" produced "everyone like pop rock" in real output.
    """
    if total == 1:
        # A room of one. "1 member likes indie rock" is how a system talks about
        # a person; "you like indie rock" is how a person is talked to.
        return "you", "like"
    if count == total:
        return "everyone", "likes"
    if count == 1:
        return "1 member", "likes"
    return f"{count} members", "like"


def group_facts(
    track: SelectedTrack,
    *,
    member_names: list[str],
    item_genres: list[str] | None = None,
    member_genres: list[set[str]] | None = None,
    familiar: list[bool] | None = None,
    tau_veto: float = 0.35,
    genre_frequency: dict[str, float] | None = None,
) -> dict:
    """Exact counts about this track and this group.

    Everything here is computed by counting the inputs. None of it is inferred
    from the score, so a fact and a contribution can be cross-checked against
    each other -- which is what the explanation test does.
    """
    scores = track.member_scores
    facts: dict = {
        "n_members": len(member_names),
        "best_member": member_names[int(np.argmax(scores))] if len(scores) else None,
        "worst_member": member_names[int(np.argmin(scores))] if len(scores) else None,
        "min_score": float(scores.min()) if len(scores) else 0.0,
        "clears_veto_by": float(scores.min() - tau_veto) if len(scores) else 0.0,
    }

    if item_genres and member_genres is not None:
        shared = {}
        for genre in item_genres:
            count = sum(1 for member in member_genres if genre in member)
            if count:
                shared[genre] = count
        if shared:
            # Most members first, then the *rarest* genre among those tied.
            #
            # Without the rarity tiebreak every reason in a rock-heavy
            # catalogue reads "everyone likes rock", which is true and tells the
            # reader nothing: a genre two thirds of the catalogue carries
            # distinguishes no track from any other. "everyone likes shoegaze"
            # is the same sentence carrying actual information.
            frequency = genre_frequency or {}
            top = max(
                shared.items(),
                key=lambda kv: (kv[1], -frequency.get(kv[0], 0.0), kv[0]),
            )
            facts["top_genre"] = top[0]
            facts["top_genre_members"] = top[1]
            facts["shared_genres"] = shared

    if familiar is not None:
        facts["known_by"] = int(sum(familiar))
        facts["new_to_everyone"] = not any(familiar)

    return facts


def explain(
    track: SelectedTrack,
    *,
    member_names: list[str],
    item_genres: list[str] | None = None,
    member_genres: list[set[str]] | None = None,
    familiar: list[bool] | None = None,
    tau_veto: float = 0.35,
    max_clauses: int = 3,
    genre_frequency: dict[str, float] | None = None,
) -> Explanation:
    """Render one track's reason.

    Clauses are ordered by how much their term actually moved the score, so the
    sentence leads with the reason that mattered most rather than with whichever
    template reads best.
    """
    facts = group_facts(
        track,
        member_names=member_names,
        item_genres=item_genres,
        member_genres=member_genres,
        familiar=familiar,
        tau_veto=tau_veto,
        genre_frequency=genre_frequency,
    )
    contributions = track.contributions
    n_members = max(len(member_names), 1)

    # (magnitude, clause, source term) -- magnitude decides the order.
    candidates: list[tuple[float, str, str]] = []

    if "group_score" in contributions:
        magnitude = abs(contributions["group_score"])
        if facts.get("top_genre_members"):
            who, verb = _fmt_members(facts["top_genre_members"], n_members)
            candidates.append((magnitude, f"{who} {verb} {facts['top_genre']}", "group_score"))
        elif facts.get("best_member") and len(member_names) > 1:
            candidates.append(
                (
                    magnitude,
                    f"it's closest to {facts['best_member']}'s taste",
                    "group_score",
                )
            )
        elif len(member_names) == 1:
            # With one member every track is trivially "closest to" them, so the
            # clause carries no information. Say what the ranking actually did.
            candidates.append((magnitude, "it scores well against your profile", "group_score"))

    if contributions.get("novelty_bonus"):
        if facts.get("new_to_everyone"):
            # "new to everyone" reads as a group fact; in a room of one the
            # honest phrasing is simply "new to you".
            clause = (
                "it introduces an artist new to you"
                if len(member_names) == 1
                else "it introduces an artist new to everyone"
            )
        else:
            clause = "it's less familiar than the rest"
        candidates.append((abs(contributions["novelty_bonus"]), clause, "novelty_bonus"))

    if contributions.get("fairness_bonus"):
        # The member the *ranker* served, not the lowest scorer on this track.
        # Those differ: the bonus goes to whoever has the least cumulative
        # satisfaction so far, so naming the per-track minimum would describe a
        # quantity that did not move the score.
        served = track.served_member
        # Not in a solo room: "it's your turn to be served" describes a rotation
        # between people, and with one member there is nobody to rotate with.
        if served is not None and served < len(member_names) and len(member_names) > 1:
            candidates.append(
                (
                    abs(contributions["fairness_bonus"]),
                    f"it's {member_names[served]}'s turn to be served",
                    "fairness_bonus",
                )
            )

    if contributions.get("popularity"):
        candidates.append((abs(contributions["popularity"]), "it's widely played", "popularity"))

    # Penalties are stated too. A reason that only ever lists what helped is a
    # sales pitch; saying a track was kept *despite* a repeat is more useful.
    if contributions.get("repetition_penalty"):
        candidates.append(
            (
                abs(contributions["repetition_penalty"]),
                "it repeats an artist heard recently",
                "repetition_penalty",
            )
        )
    if contributions.get("diversity_penalty"):
        candidates.append(
            (
                abs(contributions["diversity_penalty"]),
                "it's close to something already on the list",
                "diversity_penalty",
            )
        )

    candidates.sort(key=lambda row: (-row[0], row[1]))
    kept = candidates[:max_clauses]
    clauses = [clause for _, clause, _ in kept]
    sources = [source for _, _, source in kept]

    # The veto note is a statement about the hard filter, not a scored term, so
    # it is appended rather than competing for a clause slot -- and only when it
    # says something.
    #
    # Every surviving track cleared the veto by definition, so noting it on all
    # of them added forty-five identical characters to every row and made a
    # varied playlist read as boilerplate. It appears only when the margin is
    # slim: "this one nearly did not make it" is worth knowing, "this one passed
    # comfortably, like the other nineteen" is not.
    veto_note = ""
    margin = facts["min_score"] - tau_veto
    if len(member_names) > 1 and len(track.member_scores) > 1 and 0 <= margin < NARROW_MARGIN:
        veto_note = ", and it only just clears everyone's threshold"

    if not clauses:
        sentence = "Recommended for this group."
    elif len(clauses) == 1:
        sentence = f"Recommended because {clauses[0]}{veto_note}."
    else:
        body = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"
        sentence = f"Recommended because {body}{veto_note}."
    sentence = sentence.replace(", and it only just clears", " — it only just clears")

    return Explanation(sentence=sentence, clauses=clauses, sources=sources, facts=facts)
