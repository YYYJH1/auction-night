"""Seed commitments for a season: draw the deal seeds, publish their hashes, reveal, check.

Every random draw of a night (lots, private cards, budgets, missions, tie keys) comes from the deal
seed. A season has one deck per seed, and a deck is played once for each rotation (see
``docs/contracts.md``). The seeds are committed before the season and revealed after it:

1. ``new`` draws a secret seed for every deck with :mod:`secrets` (256 bits, 64 hex characters). It
   writes the seeds to a *private file* that only its owner can read, and the SHA-256 commitment of
   each seed to ``commitments.json``, which is published before the first night.
2. ``plan`` writes the configuration of every night of the season from a template. A configuration
   names the environment variable that holds the seed of its deck (``night.seed_env``) and carries
   the commitment of the deck. The seed itself is never written into it.
3. ``env`` prints the ``export`` line that puts the seed of one deck into the environment, and a
   night is played with ``auction-night run``.
4. After the last night, ``reveal`` opens every commitment with the seed in the private file and
   writes ``seeds.json``.
5. ``verify`` (and ``tools/check_seeds.py`` in CI) checks that every revealed seed opens its
   commitment, that every night carries the commitment published for its deck, and that the cards in
   its log are the ones that the open dealer (:func:`auction_night.engine.deal.deal`) draws from the
   revealed seed.

What the commitment covers
--------------------------
The commitment to a seed is the SHA-256 digest of its UTF-8 text, written ``sha256:<hex>``
(:func:`auction_night.contracts.events.seed_commitment`). It fixes the random draws of the
environment. What the models answered is recorded by the cassette of ``calls.jsonl``.

The three documents
-------------------
Each one is JSON with a ``schema`` field, written with two-space indentation and a final newline.

``commitments.json`` (``auction-night/season-commitments@1``, public)
    ``season``, ``algorithm`` (``sha256``), ``created_utc``, ``generator`` (program name and
    version, and the version of the random streams that the dealer uses) and ``decks``: a list of
    ``{"deck", "commitment"}``.
``seeds.json`` (``auction-night/season-seeds@1``, public after the season)
    ``season``, ``revealed_utc`` and ``decks``: a list of ``{"deck", "seed"}``.
the private file (``auction-night/season-seeds-private@1``, secret)
    ``season``, ``created_utc``, ``generator``, a ``notice``, and ``decks``: a list of
    ``{"deck", "seed", "commitment"}``. It is created with mode 0600, never replaced, and never
    inside a git work tree.

Night files and what verify reads from them
-------------------------------------------
A night is a run directory (``events.jsonl`` and ``config.yaml``, as ``auction-night run`` writes
them, and ``replay.json`` if the night was exported) or a ``replay.json`` file. The commitment is
read from the ``seed_commit`` event of the log, from ``night.deal_seed_commitment`` of the
configuration and from ``manifest.seed_commitment`` of the replay, and every file that has one must
carry the commitment published for the deck. The cards are recomputed with the configuration and
the revealed seed and compared with the ``deal_private`` events of the log (and with ``reveal`` and
``catalog`` of the replay).
"""

import contextlib
import json
import os
import re
import secrets
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Literal

import yaml
from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from auction_night import __version__
from auction_night.contracts.config import (
    Ident,
    NightConfig,
    config_hash,
    find_placeholders,
    load_config,
)
from auction_night.contracts.events import (
    AnyEvent,
    DealPrivateData,
    DealPrivateEvent,
    HammerEvent,
    Name,
    NightStartEvent,
    SeedCommitEvent,
    SeedText,
    Sha256,
    parse_event,
    seed_commitment,
    validate_log,
)
from auction_night.contracts.mechanism import ContractModel
from auction_night.contracts.replay import Replay
from auction_night.engine.deal import Deal, deal
from auction_night.engine.rng import RNG_VERSION
from auction_night.season import CONFIG_FILE, EVENTS_FILE

__all__ = [
    "COMMITMENTS_FILE",
    "ENV_PREFIX",
    "MIN_SEED_LENGTH",
    "PRIVATE_SCHEMA",
    "PRIVATE_SUFFIX",
    "REPLAY_FILE",
    "REVEAL_FILE",
    "Check",
    "Commitments",
    "NightFiles",
    "PlannedNight",
    "PrivateSeeds",
    "Report",
    "Revealed",
    "SeedsError",
    "SeedsMismatch",
    "check_config",
    "check_reveal",
    "deck_ids",
    "discover_nights",
    "export_lines",
    "load_night",
    "new_season",
    "now_stamp",
    "parse_stamp",
    "plan_season",
    "read_commitments",
    "read_private",
    "read_revealed",
    "reveal_season",
    "seed_env_name",
    "verify",
    "verify_night",
    "verify_season_dir",
    "work_tree_of",
    "write_new_season",
    "write_planned",
    "write_private",
    "write_public",
]

COMMITMENTS_FILE: Final = "commitments.json"
REVEAL_FILE: Final = "seeds.json"
REPLAY_FILE: Final = "replay.json"
PRIVATE_SUFFIX: Final = ".seeds-private.json"  # the name that .gitignore and the checks know
PRIVATE_SCHEMA: Final = "auction-night/season-seeds-private@1"
COMMITMENTS_SCHEMA: Final = "auction-night/season-commitments@1"
REVEAL_SCHEMA: Final = "auction-night/season-seeds@1"
PRIVATE_NOTICE: Final = (
    "SECRET until the season is over. Do not commit, share or paste this file. "
    "It holds the only copy of the deal seeds."
)
ENV_PREFIX: Final = "AUCTION_NIGHT_SEED_"
SEED_BYTES: Final = 32  # 256 bits, written as 64 hex characters
MIN_SEED_LENGTH: Final = 32  # characters; a shorter seed can be guessed from its commitment
MAX_DECKS: Final = 999
_SHOWN: Final = 3  # problems listed in one message
_FIXED: Final = ("seat", "codename")  # what stays at a seat while the players rotate

_STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
Stamp = Annotated[str, StringConstraints(pattern=_STAMP)]
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SeedsError(ValueError):
    """A file or an argument cannot be used. The message is for the person at the command line
    and never holds a seed."""


class SeedsMismatch(SeedsError):
    """A check failed: a seed does not open its commitment, or a file disagrees with the
    commitments that were published."""


# ------------------------------------------------------------------------------ documents


class Generator(ContractModel):
    """Which program drew the seeds, and which random streams its dealer uses."""

    name: Name
    version: Name
    rng: Name


class _Document(ContractModel):
    """A JSON document with a ``schema`` field (``schema_id`` in code, like the replay)."""

    model_config = ConfigDict(populate_by_name=True)
    SCHEMA: ClassVar[str]

    schema_id: str = Field(alias="schema")

    @field_validator("schema_id")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        if value != cls.SCHEMA:
            raise ValueError(f"this is not an {cls.SCHEMA} document")
        return value


class DeckCommitment(ContractModel):
    """The commitment published for one deck."""

    deck: Ident
    commitment: Sha256


class Commitments(_Document):
    """``commitments.json``: published before the first night of the season."""

    SCHEMA: ClassVar[str] = COMMITMENTS_SCHEMA

    season: Ident
    algorithm: Literal["sha256"] = "sha256"
    created_utc: Stamp
    generator: Generator
    decks: Annotated[list[DeckCommitment], Field(min_length=1, max_length=MAX_DECKS)]

    @model_validator(mode="after")
    def _distinct(self) -> "Commitments":
        ids = [entry.deck for entry in self.decks]
        if len(set(ids)) != len(ids):
            raise ValueError("a deck appears twice")
        values = [entry.commitment for entry in self.decks]
        if len(set(values)) != len(values):
            raise ValueError("two decks share a commitment")
        return self

    @property
    def by_deck(self) -> dict[str, str]:
        """The commitment of every deck, in the order of the file."""
        return {entry.deck: entry.commitment for entry in self.decks}


class DeckSeed(ContractModel):
    """The revealed seed of one deck."""

    deck: Ident
    seed: SeedText = Field(repr=False)


class Revealed(_Document):
    """``seeds.json``: published after the last night of the season."""

    SCHEMA: ClassVar[str] = REVEAL_SCHEMA

    season: Ident
    revealed_utc: Stamp
    decks: Annotated[list[DeckSeed], Field(min_length=1, max_length=MAX_DECKS)]

    @model_validator(mode="after")
    def _distinct(self) -> "Revealed":
        ids = [entry.deck for entry in self.decks]
        if len(set(ids)) != len(ids):
            raise ValueError("a deck appears twice")
        return self

    @property
    def by_deck(self) -> dict[str, str]:
        """The seed of every deck, in the order of the file."""
        return {entry.deck: entry.seed for entry in self.decks}


class PrivateDeck(ContractModel):
    """A deck of the private file. The seed is kept out of every repr."""

    deck: Ident
    seed: SeedText = Field(repr=False)
    commitment: Sha256


class PrivateSeeds(_Document):
    """The private file: the deal seeds of a season, secret until it is over."""

    SCHEMA: ClassVar[str] = PRIVATE_SCHEMA

    season: Ident
    created_utc: Stamp
    generator: Generator
    notice: str
    decks: Annotated[list[PrivateDeck], Field(min_length=1, max_length=MAX_DECKS)]

    @model_validator(mode="after")
    def _consistent(self) -> "PrivateSeeds":
        ids = [entry.deck for entry in self.decks]
        if len(set(ids)) != len(ids):
            raise ValueError("a deck appears twice")
        if len({entry.seed for entry in self.decks}) != len(self.decks):
            raise ValueError("two decks share a seed")
        for entry in self.decks:
            if seed_commitment(entry.seed) != entry.commitment:
                raise ValueError(f"deck {entry.deck}: the seed does not match its commitment")
        return self

    @property
    def by_deck(self) -> dict[str, str]:
        """The seed of every deck, in the order of the file."""
        return {entry.deck: entry.seed for entry in self.decks}


# ------------------------------------------------------------------------------- helpers


def now_stamp() -> str:
    """The current time as ``2026-11-01T12:00:00Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: str) -> str:
    """``text`` if it is a UTC time stamp like ``2026-11-01T12:00:00Z`` (a fraction is allowed)."""
    if not re.fullmatch(_STAMP[1:-1], text):
        raise SeedsError("a time stamp looks like 2026-11-01T12:00:00Z (UTC)")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise SeedsError("that is not a real date and time") from None
    return text


def _summary(exc: ValidationError) -> str:
    """The first problems of a validation error. The input values are left out."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'document'}: {e['msg']}" for e in errors]
    shown = "; ".join(parts[:_SHOWN])
    return shown + (f" (and {len(parts) - _SHOWN} more)" if len(parts) > _SHOWN else "")


def _short(commitment: str) -> str:
    """``sha256:`` and the first twelve hex digits of a commitment."""
    return commitment[:19] + "…"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f"duplicate key {key!r}")
        found[key] = value
    return found


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in JSON here")


def _read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SeedsError(f"cannot read {what} {path}: {exc}") from None


def _parse[M: ContractModel](model: type[M], text: str, path: Path, what: str) -> M:
    """A document from JSON text. Duplicate keys and values that are not JSON are refused."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except ValueError as exc:
        raise SeedsError(f"{what} {path} is not valid JSON: {exc}") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SeedsError(f"{what} {path} cannot be used: {_summary(exc)}") from None


def _render(document: ContractModel) -> str:
    """The text of a document: its fields in their order, two-space indentation, final newline."""
    data = document.model_dump(mode="json", by_alias=True)
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def work_tree_of(path: Path) -> Path | None:
    """The git work tree that contains ``path`` (which need not exist yet), or None.

    A folder counts when its ``.git`` looks like a repository to git (it holds HEAD, objects and
    refs) or is a file that points to one, as in a worktree or a submodule. An empty ``.git``
    folder does not count.
    """
    for folder in (path, *path.parents):
        marker = folder / ".git"
        try:
            if marker.is_dir():
                if all((marker / part).exists() for part in ("HEAD", "objects", "refs")):
                    return folder
            elif marker.is_file() and marker.read_text(
                encoding="utf-8", errors="replace"
            ).startswith("gitdir:"):
                return folder
        except OSError:
            continue
    return None


# ----------------------------------------------------------------------------------- new


def deck_ids(count: int) -> list[str]:
    """``d01``, ``d02``, ... for ``count`` decks (three digits from 100 decks on)."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_DECKS:
        raise SeedsError(f"the number of decks is a whole number from 1 to {MAX_DECKS}")
    width = max(2, len(str(count)))
    return [f"d{number:0{width}d}" for number in range(1, count + 1)]


def fresh_seed() -> str:
    """A new secret seed: 256 random bits as 64 hex characters."""
    return secrets.token_hex(SEED_BYTES)


def new_season(
    season: str,
    decks: Sequence[str],
    *,
    created_utc: str,
    draw: Callable[[], str] = fresh_seed,
) -> tuple[PrivateSeeds, Commitments]:
    """Draw a seed for each deck. Returns the private file and the commitments to publish.

    ``draw`` makes one seed and is replaced only by tests.
    """
    if len(set(decks)) != len(decks):
        raise SeedsError("a deck id appears twice")
    seeds: dict[str, str] = {}
    for deck in decks:
        seed = draw()
        if seed in seeds.values():
            raise SeedsError("two decks drew the same seed")
        seeds[deck] = seed
    generator = Generator(name="auction-night", version=__version__, rng=RNG_VERSION)
    try:
        private = PrivateSeeds(
            schema_id=PRIVATE_SCHEMA,
            season=season,
            created_utc=created_utc,
            generator=generator,
            notice=PRIVATE_NOTICE,
            decks=[
                PrivateDeck(deck=deck, seed=seed, commitment=seed_commitment(seed))
                for deck, seed in seeds.items()
            ],
        )
        public = Commitments(
            schema_id=COMMITMENTS_SCHEMA,
            season=season,
            created_utc=created_utc,
            generator=generator,
            decks=[
                DeckCommitment(deck=deck, commitment=seed_commitment(seed))
                for deck, seed in seeds.items()
            ],
        )
    except ValidationError as exc:
        raise SeedsError(_summary(exc)) from None
    return private, public


def write_private(path: Path, private: PrivateSeeds) -> Path:
    """Write the private file: new, readable by its owner only, and outside every git work tree.

    The file is created exclusively and never replaced, because it holds the only copy of the
    seeds. If writing fails, the half-written file is removed. Returns the resolved path.
    """
    target = path.resolve()
    tree = work_tree_of(target)
    if tree is not None:
        raise SeedsError(
            f"{path} is inside the git work tree {tree.name!r}: the private seeds must stay "
            "outside every repository"
        )
    if os.path.lexists(path) or os.path.lexists(target):  # a link counts as a file
        raise SeedsError(
            f"{path} already exists. It may hold the only copy of the seeds of a season, so it "
            "is never replaced: move it away if you want new seeds"
        )
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise SeedsError(f"cannot create {path}: {exc}") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)  # whatever the umask said
            handle.write(_render(private).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            target.unlink()
        raise SeedsError(f"cannot write {path}: {exc}") from None
    return target


def write_new_season(
    private: PrivateSeeds,
    public: Commitments,
    *,
    private_out: Path,
    out: Path,
    overwrite: bool = False,
) -> Path:
    """Write the two files of a new season: the private file, then the commitments.

    The commitments file is checked first, so that no private file is made for commitments that
    would be refused. If the commitments still cannot be written, the private file is removed
    again: nothing has been published, so no seed is in use. Returns the path of the private file.
    """
    if out.exists() and not overwrite:
        raise SeedsError(f"{out} already exists (use --overwrite to replace it)")
    written = write_private(private_out, private)
    try:
        write_public(out, public, overwrite=overwrite)
    except SeedsError:
        with contextlib.suppress(OSError):
            written.unlink()
        raise
    return written


def read_private(path: Path) -> PrivateSeeds:
    """Read the private file. A file that other users can read is refused, as ssh does for keys."""
    if not path.is_file():
        raise SeedsError(f"private seed file not found: {path}")
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise SeedsError(
                f"{path} can be read by other users (mode {mode:03o}): run chmod 600 on it first"
            )
    return _parse(PrivateSeeds, _read_text(path, "private seed file"), path, "private seed file")


def write_public(path: Path, document: ContractModel, *, overwrite: bool = False) -> None:
    """Write a public document next to nothing else: new, or replaced when ``overwrite`` is set."""
    if path.exists() and not overwrite:
        raise SeedsError(f"{path} already exists (use --overwrite to replace it)")
    scratch = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(_render(document), encoding="utf-8", newline="\n")
        os.replace(scratch, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            scratch.unlink()
        raise SeedsError(f"cannot write {path}: {exc}") from None


def read_commitments(path: Path) -> Commitments:
    """Read ``commitments.json``."""
    if not path.is_file():
        raise SeedsError(f"commitments file not found: {path}")
    return _parse(Commitments, _read_text(path, "commitments file"), path, "commitments file")


def read_revealed(path: Path) -> Revealed:
    """Read ``seeds.json``."""
    if not path.is_file():
        raise SeedsError(f"seeds file not found: {path}")
    return _parse(Revealed, _read_text(path, "seeds file"), path, "seeds file")


# ---------------------------------------------------------------------------------- plan


@dataclass(frozen=True, slots=True)
class PlannedNight:
    """One night of the season: its configuration file, and the variable that holds the seed."""

    night: str
    deck: str
    rotation: int
    seed_env: str
    filename: str
    text: str


def seed_env_name(season: str, deck: str) -> str:
    """The environment variable that holds the seed of a deck: ``AUCTION_NIGHT_SEED_S1_D03`` for
    season ``s1`` and deck ``d03``."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"{ENV_PREFIX}{season}_{deck}").upper()
    if not _ENV_NAME.fullmatch(name):
        raise SeedsError(
            f"season {season!r} and deck {deck!r} give a variable name that is too long"
        )
    return name


def _players(seats: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The model seats of a template in seat order. A person cannot sit in a season."""
    for seat in seats:
        if seat.get("kind") == "human":
            raise SeedsError("the template has a human seat: nights with a person are not ranked")
    return sorted((s for s in seats if s.get("kind") in ("llm", "mock")), key=lambda s: s["seat"])


def plan_season(
    template: Path,
    commitments: Commitments,
    *,
    decks: Sequence[str] | None = None,
    rotations: int | None = None,
) -> tuple[list[PlannedNight], list[str]]:
    """The configuration of every night of a rotation season, and notes for the person.

    ``template`` is an ordinary night configuration that validates by itself. For each deck and
    each rotation ``r`` it is changed in these places only: ``night.id`` (``<season>-<deck>-r<r>``),
    ``night.seed`` (removed), ``night.seed_env`` and ``night.deal_seed_commitment``, the ``season``
    section (id, ``rotation`` format, the decks with their commitments, this night's deck and
    rotation) and the players of the model seats. The player at the i-th model seat in rotation r
    is the one that the template puts at model seat ``(i + r) mod n``, so over n rotations every
    player sits at every seat once. Seat numbers and code names stay where they are, and so do the
    bots. Nothing in the result depends on the seeds, which ``plan`` never sees.

    ``decks`` limits the plan to some decks (default: all) and ``rotations`` to the first
    rotations (default: one per model seat).
    """
    try:
        load_config(template)  # validates, and rejects duplicate keys
        raw = yaml.safe_load(template.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        lines = str(exc).splitlines()
        raise SeedsError(f"cannot read the template {template}: {' '.join(lines[:3])}") from None
    notes: list[str] = []
    if "seed" in raw["night"]:
        notes.append("the inline night.seed of the template is not copied into the plan")
    players = _players(raw["seats"])
    if not players:
        raise SeedsError("the template has no model seat to rotate")
    count = len(players)
    wanted = count if rotations is None else rotations
    if isinstance(wanted, bool) or not 1 <= wanted <= count:
        raise SeedsError(f"rotations is a whole number from 1 to {count} (the model seats)")
    published = commitments.by_deck
    chosen = list(published) if decks is None else list(dict.fromkeys(decks))
    unknown = [deck for deck in chosen if deck not in published]
    if unknown:
        raise SeedsError(f"not in the commitments of season {commitments.season}: {unknown[0]}")
    names = {deck: seed_env_name(commitments.season, deck) for deck in published}
    if len(set(names.values())) != len(names):
        raise SeedsError("two decks give the same variable name: rename a deck")

    seat_numbers = [seat["seat"] for seat in players]
    kept_season = {
        key: value
        for key, value in (raw.get("season") or {}).items()
        if key not in ("id", "format", "decks", "deck", "rotation")
    }
    planned: list[PlannedNight] = []
    for deck in chosen:
        for rotation in range(wanted):
            night_id = f"{commitments.season}-{deck}-r{rotation}"
            sitting = {
                number: {
                    k: v for k, v in players[(i + rotation) % count].items() if k not in _FIXED
                }
                for i, number in enumerate(seat_numbers)
            }
            seats = [
                {"seat": seat["seat"], "codename": seat["codename"], **sitting[seat["seat"]]}
                if seat["seat"] in sitting
                else dict(seat)
                for seat in raw["seats"]
            ]
            night = {
                "id": night_id,
                "seed_env": names[deck],
                "deal_seed_commitment": published[deck],
                **{
                    key: value
                    for key, value in raw["night"].items()
                    if key not in ("id", "seed", "seed_env", "deal_seed_commitment")
                },
            }
            season_section = {
                "id": commitments.season,
                "format": "rotation",
                "decks": [
                    {"id": entry.deck, "seed_commitment": entry.commitment}
                    for entry in commitments.decks
                ],
                "deck": deck,
                "rotation": rotation,
                **kept_season,
            }
            config = {**raw, "night": night, "season": season_section, "seats": seats}
            planned.append(_planned(config, night_id, deck, rotation, names[deck], notes))
    return planned, notes


def _planned(
    config: dict[str, Any], night_id: str, deck: str, rotation: int, env: str, notes: list[str]
) -> PlannedNight:
    try:
        checked = NightConfig.model_validate(config)
    except ValidationError as exc:
        raise SeedsError(
            f"night {night_id} is not a valid configuration: {_summary(exc)}"
        ) from None
    assert checked.night.seed is None and checked.night.seed_env == env
    left = find_placeholders(checked)
    if left:
        note = f"placeholders to replace before a run: {', '.join(left[:_SHOWN])}"
        if note not in notes:
            notes.append(note)
    season = config["season"]["id"]
    header = (
        f"# Night {night_id}: deck {deck}, rotation {rotation} of season {season}.\n"
        "# Written by `auction-night seeds plan`. The deal seed is not in this file. It is read\n"
        f"# from the environment variable {env} and must open night.deal_seed_commitment.\n"
    )
    body = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    if yaml.safe_load(body) != config:
        raise SeedsError(f"night {night_id}: the configuration does not survive being written")
    return PlannedNight(night_id, deck, rotation, env, f"{night_id}.yaml", header + body)


def write_planned(
    directory: Path, nights: Sequence[PlannedNight], *, overwrite: bool
) -> list[Path]:
    """Write the configurations. A file that already holds the same text is left as it is, and
    one that differs is refused unless ``overwrite`` is set. Nothing is written when one is
    refused. Returns the paths of the files that were written."""
    pending = []
    for night in nights:
        target = directory / night.filename
        if target.exists():
            if target.read_text(encoding="utf-8") == night.text:
                continue
            if not overwrite:
                raise SeedsError(f"{target} already exists and differs (use --overwrite)")
        pending.append((target, night.text))
    written = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for target, text in pending:
            scratch = target.with_name(target.name + ".part")
            scratch.write_text(text, encoding="utf-8", newline="\n")
            os.replace(scratch, target)
            written.append(target)
    except OSError as exc:
        raise SeedsError(f"cannot write the plan to {directory}: {exc}") from None
    return written


def export_lines(private: PrivateSeeds, decks: Iterable[str]) -> list[str]:
    """``export NAME=seed`` for each deck, to be run by a shell before ``auction-night run``."""
    seeds = private.by_deck
    lines = []
    for deck in decks:
        if deck not in seeds:
            raise SeedsError(f"the private file has no deck {deck!r}")
        name = seed_env_name(private.season, deck)
        lines.append(f"export {name}={shlex.quote(seeds[deck])}")
    return lines


# -------------------------------------------------------------------------------- reveal


def reveal_season(
    private: PrivateSeeds, commitments: Commitments, *, revealed_utc: str
) -> Revealed:
    """Open every commitment with the seed in the private file. Raises :class:`SeedsMismatch`
    when the files are not of the same season, differ in their decks, or a seed does not open its
    commitment. The reveal is complete or it is not made."""
    if private.season != commitments.season:
        raise SeedsMismatch(
            f"the private file is of season {private.season!r} and the commitments of "
            f"{commitments.season!r}"
        )
    published, seeds = commitments.by_deck, private.by_deck
    missing = [deck for deck in published if deck not in seeds]
    extra = [deck for deck in seeds if deck not in published]
    if missing or extra:
        raise SeedsMismatch(
            "the decks differ: "
            + (f"no seed for {', '.join(missing)}" if missing else "")
            + ("; " if missing and extra else "")
            + (f"not committed: {', '.join(extra)}" if extra else "")
        )
    for deck, commitment in published.items():
        if seed_commitment(seeds[deck]) != commitment:
            raise SeedsMismatch(f"the seed of deck {deck} does not open {_short(commitment)}")
    return Revealed(
        schema_id=REVEAL_SCHEMA,
        season=commitments.season,
        revealed_utc=revealed_utc,
        decks=[DeckSeed(deck=deck, seed=seeds[deck]) for deck in published],
    )


# -------------------------------------------------------------------------------- reports


@dataclass(frozen=True, slots=True)
class Check:
    """One result: ``ok``, ``fail``, ``skip`` (not checkable with what was given) or ``note``."""

    status: Literal["ok", "fail", "skip", "note"]
    text: str


@dataclass(slots=True)
class Report:
    """The results of a verification, in groups with a heading each."""

    groups: list[tuple[str, list[Check]]] = field(default_factory=list)

    def add(self, heading: str, checks: Sequence[Check]) -> None:
        """Append a group of results."""
        self.groups.append((heading, list(checks)))

    def count(self, status: str) -> int:
        """How many results have this status."""
        return sum(1 for _, checks in self.groups for c in checks if c.status == status)

    @property
    def failed(self) -> bool:
        """True when a check failed."""
        return self.count("fail") > 0

    def lines(self) -> list[str]:
        """The report as text."""
        tags = {"ok": "ok  ", "fail": "FAIL", "skip": "skip", "note": "note"}
        out = []
        for heading, checks in self.groups:
            out.append(heading)
            out += [f"  {tags[c.status]}  {c.text}" for c in checks]
        out.append(self.summary())
        return out

    def summary(self) -> str:
        """The last line: the numbers of ok, failed and skipped checks."""
        total = sum(self.count(s) for s in ("ok", "fail", "skip"))
        noun = "check" if total == 1 else "checks"
        return (
            f"{total} {noun}: {self.count('ok')} ok, {self.count('fail')} failed, "
            f"{self.count('skip')} skipped"
        )


def _ok(text: str) -> Check:
    return Check("ok", text)


def _fail(text: str) -> Check:
    return Check("fail", text)


def check_reveal(commitments: Commitments, revealed: Revealed) -> list[Check]:
    """Every revealed seed must open the commitment published for its deck."""
    if revealed.season != commitments.season:
        return [
            _fail(
                f"the seeds are of season {revealed.season!r} and the commitments of "
                f"{commitments.season!r}"
            )
        ]
    seeds = revealed.by_deck
    published = commitments.by_deck
    checks = []
    for deck, commitment in published.items():
        seed = seeds.get(deck)
        if seed is None:
            checks.append(_fail(f"deck {deck}: committed, but its seed is not revealed"))
        elif seed_commitment(seed) != commitment:
            checks.append(
                _fail(f"deck {deck}: the revealed seed does not open {_short(commitment)}")
            )
        elif len(seed) < MIN_SEED_LENGTH:
            checks.append(
                _fail(
                    f"deck {deck}: the seed has {len(seed)} characters, so its commitment could "
                    f"have been guessed (seeds from `seeds new` have {2 * SEED_BYTES})"
                )
            )
        else:
            checks.append(_ok(f"deck {deck}: the revealed seed opens {_short(commitment)}"))
    checks += [
        _fail(f"deck {deck}: a seed is revealed for a deck that was never committed")
        for deck in seeds
        if deck not in published
    ]
    return checks


# ------------------------------------------------------------------------------- nights


@dataclass(frozen=True, slots=True)
class NightFiles:
    """The files of one night, as far as they were given."""

    label: str
    events: tuple[AnyEvent, ...] | None = None
    config: NightConfig | None = None
    replay: Replay | None = None

    def night_ids(self) -> dict[str, str]:
        """The night id that each file names."""
        found = {}
        if self.events is not None:
            start = self.events[0]
            assert isinstance(start, NightStartEvent)
            found[EVENTS_FILE] = start.data.night_id
        if self.config is not None:
            found[CONFIG_FILE] = self.config.night.id
        if self.replay is not None:
            found[REPLAY_FILE] = self.replay.night.id
        return found


def _read_events(path: Path) -> tuple[AnyEvent, ...]:
    text = _read_text(path, "event log")
    events = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            events.append(parse_event(line))
        except ValueError:
            raise SeedsError(f"{path}: line {number} is not a valid event") from None
    problems = validate_log(events)
    if problems:
        raise SeedsError(f"{path}: {problems[0]}")
    return tuple(events)


def _read_config(path: Path) -> NightConfig:
    try:
        return load_config(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        lines = str(exc).splitlines()
        raise SeedsError(f"cannot read {path}: {' '.join(lines[:3])}") from None


def _read_replay(path: Path) -> Replay:
    try:
        return Replay.model_validate_json(_read_text(path, "replay"))
    except ValidationError as exc:
        raise SeedsError(f"replay {path} cannot be used: {_summary(exc)}") from None


def load_night(path: Path) -> NightFiles:
    """Read a night: a run directory (``events.jsonl``, ``config.yaml``, ``replay.json``, each
    if present, and at least one of the log and the replay) or a replay file, which is read
    together with the ``config.yaml`` beside it."""
    if path.is_dir():
        events_path, replay_path = path / EVENTS_FILE, path / REPLAY_FILE
        if not events_path.is_file() and not replay_path.is_file():
            raise SeedsError(f"{path} holds neither {EVENTS_FILE} nor {REPLAY_FILE}")
        config_path = path / CONFIG_FILE
        return NightFiles(
            label=str(path),
            events=_read_events(events_path) if events_path.is_file() else None,
            config=_read_config(config_path) if config_path.is_file() else None,
            replay=_read_replay(replay_path) if replay_path.is_file() else None,
        )
    if not path.is_file():
        raise SeedsError(f"not a file or directory: {path}")
    config_path = path.with_name(CONFIG_FILE)
    return NightFiles(
        label=str(path),
        config=_read_config(config_path) if config_path.is_file() else None,
        replay=_read_replay(path),
    )


def discover_nights(paths: Sequence[Path]) -> list[Path]:
    """The nights behind ``paths``. A file is a replay. A directory that holds ``events.jsonl``
    or ``replay.json`` is one night, and any other directory holds the nights below it."""
    found: dict[Path, Path] = {}
    for given in paths:
        if given.is_file() or (given / EVENTS_FILE).is_file() or (given / REPLAY_FILE).is_file():
            found.setdefault(given.resolve(), given)
            continue
        if not given.is_dir():
            raise SeedsError(f"not a file or directory: {given}")
        below = {log.parent for log in given.rglob(EVENTS_FILE)}
        below |= {replay.parent for replay in given.rglob(REPLAY_FILE)}
        if not below:
            raise SeedsError(f"no night in or under {given}")
        for night in below:
            found.setdefault(night.resolve(), night)
    return sorted(found.values(), key=str)


@dataclass(frozen=True, slots=True)
class _Claim:
    """The commitment, deck and season that one file of a night names."""

    source: str
    commitment: str | None
    deck: str | None
    season: str | None
    placeholder: bool = False


def _claims(files: NightFiles) -> list[_Claim]:
    claims = []
    if files.events is not None:
        commits = [
            e.data
            for e in files.events
            if isinstance(e, SeedCommitEvent) and e.data.phase == "commit"
        ]
        first = commits[0] if commits else None
        claims.append(
            _Claim(
                EVENTS_FILE,
                None if first is None else first.commitment,
                None if first is None else first.deck_id,
                None,
            )
        )
    if files.config is not None:
        night, season = files.config.night, files.config.season
        written = night.deal_seed_commitment
        real = written is not None and written.startswith("sha256:")
        claims.append(
            _Claim(
                CONFIG_FILE,
                written if real else None,
                season.deck,
                season.id,
                placeholder=written is not None and not real,
            )
        )
    if files.replay is not None:
        manifest, night_info = files.replay.manifest, files.replay.night
        claims.append(
            _Claim(REPLAY_FILE, manifest.seed_commitment, night_info.deck_id, night_info.season_id)
        )
    return claims


def _lines_of(problems: Sequence[str]) -> str:
    shown = "; ".join(problems[:_SHOWN])
    return shown + (f" (and {len(problems) - _SHOWN} more)" if len(problems) > _SHOWN else "")


def _card_difference(logged: DealPrivateData, dealt: DealPrivateData) -> str | None:
    """How a private card of the log differs from the one the dealer draws, or None."""
    if logged.budget != dealt.budget:
        return f"the budget is {logged.budget} in the log and {dealt.budget} from the seed"
    for name, a, b in (
        ("value", logged.values, dealt.values),
        ("signal", logged.signals, dealt.signals),
    ):
        for lot in sorted(set(a) | set(b)):
            if a.get(lot) != b.get(lot):
                return (
                    f"the {name} of lot {lot} is {a.get(lot)} in the log "
                    f"and {b.get(lot)} from the seed"
                )
    if logged.mission != dealt.mission:
        return "the mission differs from the one drawn from the seed"
    return None


def _lot_differences(logged: Sequence[Any], dealt: Deal) -> list[str]:
    """Where the lots of the public catalog differ from the schedule that the seed gives."""
    if len(logged) != len(dealt.lots):
        return [f"{len(logged)} lots in the file and {len(dealt.lots)} from the seed"]
    problems = []
    for entry, lot in zip(logged, dealt.lots, strict=True):
        for name in ("format", "tier", "tag", "reserve", "mechanism"):
            a, b = getattr(entry, name), getattr(lot, name)
            if a != b:
                problems.append(
                    f"lot {lot.lot}: the {name} is {a} in the file and {b} from the seed"
                )
    return problems


def _log_problems(dealt: Deal, events: Sequence[AnyEvent]) -> list[str]:
    start = events[0]
    assert isinstance(start, NightStartEvent)
    problems = _lot_differences(start.data.lots, dealt)
    logged = {e.data.seat: e.data for e in events if isinstance(e, DealPrivateEvent)}
    cards = {card.seat: card for card in dealt.cards()}
    if logged.keys() != cards.keys():
        problems.append(
            f"private cards for seats {sorted(logged)} in the log and {sorted(cards)} from the seed"
        )
    for seat in sorted(cards.keys() & logged.keys()):
        why = _card_difference(logged[seat], cards[seat])
        if why:
            problems.append(f"seat {seat}: {why}")
    for event in events:
        if isinstance(event, HammerEvent) and event.lot is not None:
            expected = (
                dealt.lots[event.lot - 1].blind_value if event.lot <= len(dealt.lots) else None
            )
            if event.data.true_value != expected:
                problems.append(f"lot {event.lot}: the true value at the hammer differs")
    return problems


def _replay_problems(dealt: Deal, replay: Replay) -> list[str]:
    problems = _lot_differences(replay.catalog, dealt)
    cards = dealt.cards()
    reveal = replay.reveal
    expected = {
        "budgets": {card.seat: card.budget for card in cards},
        "values": {card.seat: dict(card.values) for card in cards},
        "signals": {card.seat: dict(card.signals) for card in cards if card.signals},
        "true values": {
            lot.lot: lot.blind_value for lot in dealt.lots if lot.blind_value is not None
        },
    }
    shown = {
        "budgets": dict(reveal.budgets),
        "values": {seat: dict(v) for seat, v in reveal.values.items()},
        "signals": {seat: dict(v) for seat, v in reveal.signals.items() if v},
        "true values": dict(reveal.true_values),
    }
    problems += [
        f"reveal.{name} differ from the seed" for name in expected if shown[name] != expected[name]
    ]
    return problems


def verify_night(
    files: NightFiles,
    commitments: Commitments,
    revealed: Revealed | None = None,
    *,
    strict: bool = False,
) -> list[Check]:
    """Check one night against the published commitments and, when the seeds are revealed,
    against the cards that the dealer draws from the seed of its deck.

    With ``strict`` a check that cannot be made for lack of a file counts as a failure.
    """
    checks: list[Check] = []
    published = commitments.by_deck

    def lacking(text: str) -> None:
        checks.append(_fail(text + " (--strict)") if strict else Check("skip", text))

    ids = files.night_ids()
    if len(set(ids.values())) > 1:
        shown = ", ".join(f"{source} is of {night}" for source, night in ids.items())
        return [_fail(f"the files are not of one night: {shown}")]
    claims = _claims(files)
    decks = sorted({c.deck for c in claims if c.deck is not None})
    if len(decks) > 1:
        return [_fail(f"the files name different decks: {', '.join(decks)}")]
    deck = decks[0] if decks else None
    if deck is None:
        found = {c.commitment for c in claims if c.commitment is not None}
        matching = [name for name, value in published.items() if value in found]
        if len(matching) != 1:
            return [_fail("it names no deck, and its commitment is not one of the published ones")]
        deck = matching[0]
        checks.append(Check("note", f"it names no deck: matched deck {deck} by its commitment"))
    expected = published.get(deck)
    if expected is None:
        return [_fail(f"deck {deck} is not in the commitments of season {commitments.season}")]

    for claim in claims:
        if claim.placeholder:
            checks.append(_fail(f"{claim.source} still holds a placeholder, not the commitment"))
        elif claim.commitment is None:
            checks.append(_fail(f"{claim.source} carries no seed commitment"))
        elif claim.commitment != expected:
            checks.append(
                _fail(
                    f"{claim.source} carries {_short(claim.commitment)}, but {_short(expected)} "
                    f"was published for deck {deck}"
                )
            )
        else:
            checks.append(
                _ok(f"{claim.source} carries {_short(expected)}, published for deck {deck}")
            )
        if claim.season is not None and claim.season != commitments.season:
            checks.append(
                _fail(
                    f"{claim.source} is of season {claim.season!r}, not of {commitments.season!r}"
                )
            )

    seed = None if revealed is None else revealed.by_deck.get(deck)
    checks += _seed_checks(files, expected, seed)
    checks += _card_checks(files, seed, revealed is not None, lacking)
    return checks


def _seed_checks(files: NightFiles, expected: str, seed: str | None) -> list[Check]:
    """Seeds that a night file carries itself must open the published commitment."""
    checks = []
    carried: list[tuple[str, str]] = []
    if files.events is not None:
        carried += [
            (EVENTS_FILE, e.data.seed)
            for e in files.events
            if isinstance(e, SeedCommitEvent) and e.data.seed is not None
        ]
    if files.replay is not None and files.replay.manifest.revealed_seed is not None:
        carried.append((REPLAY_FILE, files.replay.manifest.revealed_seed))
    for source, value in carried:
        if seed_commitment(value) != expected:
            checks.append(_fail(f"the seed revealed in {source} does not open {_short(expected)}"))
        else:
            checks.append(_ok(f"the seed revealed in {source} opens {_short(expected)}"))
    if files.config is not None and files.config.night.seed is not None:
        if seed is None:
            checks.append(
                _fail(f"{CONFIG_FILE} holds the deal seed while the season is not revealed")
            )
        elif files.config.night.seed != seed:
            checks.append(
                _fail(f"the seed inside {CONFIG_FILE} is not the revealed seed of the deck")
            )
    return checks


def _card_checks(
    files: NightFiles, seed: str | None, have_seeds: bool, lacking: Callable[[str], None]
) -> list[Check]:
    """Recompute the cards from the seed and compare them with the log and the replay."""
    checks: list[Check] = []
    if seed is None:
        lacking(
            "cards not recomputed: the seed of the deck is not revealed"
            if not have_seeds
            else "cards not recomputed: no seed was given for the deck"
        )
        return checks
    if files.config is None:
        lacking(f"cards not recomputed: no {CONFIG_FILE} beside the night")
        return checks
    config = files.config
    if files.replay is not None and files.replay.manifest.config_hash != config_hash(config):
        checks.append(
            _fail(f"{CONFIG_FILE} is not the configuration that {REPLAY_FILE} was made with")
        )
        return checks
    try:
        dealt = deal(config, seed=seed)
    except ValueError as exc:
        checks.append(_fail(f"the dealer refuses {CONFIG_FILE}: {exc}"))
        return checks
    size = f"{dealt.n_seats} seats, {len(dealt.lots)} lots"
    sources = []
    if files.events is not None:
        sources.append((EVENTS_FILE, _log_problems(dealt, files.events)))
    if files.replay is not None:
        sources.append((REPLAY_FILE, _replay_problems(dealt, files.replay)))
    for source, problems in sources:
        if problems:
            checks.append(
                _fail(f"{source} differs from the cards of the seed: {_lines_of(problems)}")
            )
        else:
            checks.append(_ok(f"{source} matches the cards recomputed from the seed ({size})"))
    return checks


# ------------------------------------------------------------------------------ the season


def _deck_summary(
    commitments: Commitments, nights: Sequence[tuple[NightFiles, str | None]]
) -> list[Check]:
    """How many nights each committed deck has among the nights given (information only)."""
    by_deck: dict[str, list[int | None]] = {deck: [] for deck in commitments.by_deck}
    for files, deck in nights:
        if deck in by_deck:
            rotation = None
            if files.config is not None:
                rotation = files.config.season.rotation
            elif files.replay is not None:
                rotation = files.replay.night.rotation
            by_deck[deck].append(rotation)
    checks = []
    for deck, rotations in by_deck.items():
        if not rotations:
            checks.append(Check("note", f"deck {deck}: committed, and no night of it was given"))
        else:
            seen = ", ".join(str(r) for r in sorted(r for r in rotations if r is not None))
            noun = "night" if len(rotations) == 1 else "nights"
            text = f"deck {deck}: {len(rotations)} {noun} given (rotations {seen or 'unknown'})"
            checks.append(Check("note", text))
    return checks


def _deck_of(files: NightFiles) -> str | None:
    decks = {c.deck for c in _claims(files) if c.deck is not None}
    return next(iter(decks)) if len(decks) == 1 else None


def verify(
    commitments: Commitments,
    revealed: Revealed | None,
    nights: Sequence[Path] = (),
    *,
    strict: bool = False,
) -> Report:
    """Verify a season: the commitments, the reveal if there is one, and the nights given."""
    report = Report()
    count = len(commitments.decks)
    if revealed is None:
        head = f"season {commitments.season}: {count} decks committed, not revealed yet"
        report.add(head, [_ok(f"{COMMITMENTS_FILE} is well formed")])
    else:
        report.add(
            f"season {commitments.season}: {count} decks committed and revealed",
            check_reveal(commitments, revealed),
        )
    loaded = [load_night(path) for path in nights]
    for files in loaded:
        label = next(iter(files.night_ids().values()), "?")
        report.add(
            f"night {label} ({files.label})",
            verify_night(files, commitments, revealed, strict=strict),
        )
    if loaded:
        report.add("decks", _deck_summary(commitments, [(f, _deck_of(f)) for f in loaded]))
    return report


def check_config(
    config: NightConfig, commitments: Commitments, revealed: Revealed | None
) -> list[Check]:
    """Check a planned configuration: its season, its deck, its commitment, and no inline seed."""
    night, season = config.night, config.season
    published = commitments.by_deck
    if season.id != commitments.season:
        return [_fail(f"it is of season {season.id!r}, not of {commitments.season!r}")]
    if season.deck not in published:
        return [_fail(f"deck {season.deck!r} is not in the commitments")]
    checks = []
    written = night.deal_seed_commitment
    if written != published[season.deck]:
        checks.append(
            _fail(f"night.deal_seed_commitment is not the one published for deck {season.deck}")
        )
    else:
        checks.append(_ok(f"it carries {_short(written)}, published for deck {season.deck}"))
    if night.seed is not None:
        known = None if revealed is None else revealed.by_deck.get(season.deck)
        if known is None or night.seed != known:
            checks.append(_fail("night.seed holds a deal seed in a file before the reveal"))
    return checks


def _looks_like_config(path: Path) -> NightConfig | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict) or not {"night", "seats"} <= raw.keys():
        return None
    return _read_config(path)


def verify_season_dir(season_dir: Path) -> Report:
    """Verify a published season directory (``seasons/<name>/``) as CI does.

    It holds ``commitments.json``, ``seeds.json`` once the season is over, the night directories
    below it and the planned configurations. Without ``seeds.json`` the commitments and the
    commitment of every night are checked, and the cards cannot be. With it, every night must
    also bring what is needed to recompute its cards.
    """
    commitments = read_commitments(season_dir / COMMITMENTS_FILE)
    reveal_path = season_dir / REVEAL_FILE
    revealed = read_revealed(reveal_path) if reveal_path.is_file() else None
    below = {log.parent for log in season_dir.rglob(EVENTS_FILE)}
    below |= {replay.parent for replay in season_dir.rglob(REPLAY_FILE)}
    nights = sorted(below, key=str)
    report = verify(commitments, revealed, nights, strict=revealed is not None)
    inside = {night.resolve() for night in nights}
    plans = []
    for path in sorted([*season_dir.rglob("*.yaml"), *season_dir.rglob("*.yml")]):
        if path.parent.resolve() in inside:
            continue  # the configuration of a night is checked with the night
        config = _looks_like_config(path)
        if config is not None:
            plans.append((path.relative_to(season_dir), config))
    for relative, config in plans:
        report.add(
            f"configuration {relative.as_posix()}", check_config(config, commitments, revealed)
        )
    return report
