"""Command line interface: ``auction-night <command>``.

* ``run`` plays a night and writes its log and its cassette. ``replay`` plays it again from the
  cassette alone and checks the log byte for byte.
* ``play`` takes a seat at a night from the terminal. ``simulate`` plays nights with rule bots only
  and writes nothing. ``estimate`` prices a night before it runs.
* ``season`` builds the leaderboard from finished nights. ``export`` turns a finished night into
  ``replay.json``, the file the viewer plays, behind the content gate, and ``export --clear``
  releases the findings that a person has read. ``show`` calls the models of the show layer (host,
  judges, content classifier) on a finished night and records them, which ``export`` needs when
  one of these roles is a model.
* ``seeds`` is a group for the seed commitments of a season: ``seeds new`` draws the secret seeds
  and the commitments to publish, ``seeds plan`` writes the configuration of every night,
  ``seeds env`` prints the line that gives a night its seed, ``seeds reveal`` opens the commitments
  after the season and ``seeds verify`` checks them against the nights.

``run`` and ``play`` save the configuration file in the directory of the night as ``config.yaml``,
byte for byte. ``replay`` and ``export`` read it from there unless ``--config`` names another one,
and ``season`` reads it from each night directory.

Exit status of every command: 0 for success, 2 for a usage error or a file it cannot use. ``replay``
and ``export`` also exit with 1 when they refuse the night: the log or a recording does not match,
or (``export``) the content gate stops it. ``seeds reveal`` and ``seeds verify`` exit with 1 when a
seed does not open its commitment or a check fails. ``show`` exits with 1 when a model of the show
layer gets no answer, when the cost cap stops it or when the night does not replay, and then it
writes no file.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import typer

app = typer.Typer(
    name="auction-night",
    help="AI Auction Night: textbook auctions played by language models.",
    no_args_is_help=True,
    add_completion=False,
)

REPLAY_FILE = "replay.json"  # what `export` writes, in the run directory unless told otherwise


@app.command()
def run(
    config: Path = typer.Option(..., "--config", "-c", help="The night configuration (YAML)."),
    out: Path = typer.Option(
        ..., "--out", "-o", help="Directory for the event log (events.jsonl) and the cassette."
    ),
    prices: Path | None = typer.Option(
        None, "--prices", help="The price list. Default: the one the configuration names."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace the files of an earlier run in --out."
    ),
) -> None:
    """Run a night: seat the models and bots, play the lots, write the event log.

    The event log goes to events.jsonl and every call to a model goes to calls.jsonl.

    The configuration file is copied next to them as config.yaml, byte for byte.

    `auction-night replay`, `export` and `season` find it there and need no --config.

    `auction-night replay` plays the night again from them, without a network.

    Seats of kind mock are played by scripted stand-ins that call no API.

    A night that reaches its cost cap is played to the end with the models silent, and flagged.
    """
    import yaml

    from auction_night.llm.accounting import PriceTable
    from auction_night.llm.nightrun import (
        CALLS_FILE,
        CONFIG_FILE,
        EVENTS_FILE,
        NightRunError,
        describe,
        load_night_config,
        run_directory,
    )

    try:
        night_config = load_night_config(config)
        price_path = prices
        if price_path is None and night_config.limits.prices_file:
            price_path = _find_prices(night_config.limits.prices_file, config)
            if price_path is None:
                raise NightRunError(f"price list not found: {night_config.limits.prices_file}")
        if price_path is not None and not price_path.is_file():
            raise NightRunError(f"price list not found: {price_path}")
        try:
            table = PriceTable.from_file(price_path) if price_path else None
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise NightRunError(
                f"cannot read {price_path}:\n" + "\n".join(str(exc).splitlines()[:12])
            ) from None
        result = run_directory(
            night_config, out, prices=table, overwrite=overwrite, config_file=config
        )
    except NightRunError as exc:
        typer.echo(f"auction-night run: {exc}", err=True)
        raise typer.Exit(code=2) from None
    for line in describe(result):
        typer.echo(line)
    typer.echo(f"saved the configuration as {out / CONFIG_FILE}")
    typer.echo(f"wrote {out / EVENTS_FILE} and {out / CALLS_FILE}")


@app.command()
def replay(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="The night configuration (YAML) the night was run with. Default: config.yaml in "
        "the --from directory, where `run` and `play` save it.",
    ),
    source: Path = typer.Option(
        ..., "--from", help="The directory of the run (events.jsonl and calls.jsonl)."
    ),
) -> None:
    """Replay a recorded night from its cassette, with no API calls.

    The night is played again from calls.jsonl and its log is compared with events.jsonl.

    Without --config the configuration is the config.yaml in the --from directory.

    Exit status 0: the same bytes. 1: the log differs, or the cassette no longer fits the night.

    Exit status 2: a file cannot be read. The deal seed is needed again, as for the run.
    """
    from auction_night.llm.cassette import CassetteError
    from auction_night.llm.nightrun import (
        NightRunError,
        config_path_for,
        load_night_config,
        replay_directory,
    )

    try:
        night_config = load_night_config(config_path_for(source, config))
        report = replay_directory(night_config, source)
    except NightRunError as exc:
        typer.echo(f"auction-night replay: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except CassetteError as exc:
        typer.echo(f"auction-night replay: the cassette does not fit this night: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not report.matches:
        typer.echo(f"auction-night replay: the replay differs: {report.difference}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"night {night_config.night.id}: replay matches, {report.events} events")


@app.command()
def play(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="The night configuration (YAML). You take the place of one of its model seats.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="Directory for the event log, the cassette and the record of what you type.",
    ),
    human_seat: int | None = typer.Option(
        None,
        "--human-seat",
        min=1,
        help="The seat you take: an llm or mock seat of the configuration. Not needed when the "
        "configuration has a seat of kind human.",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=1,
        help="Seconds you have for each phase. A phase you leave unanswered counts as an "
        "abstention. Default: limits.human_timeout_s of the configuration, else no limit. "
        "The timeout of the model calls is separate.",
    ),
    prices: Path | None = typer.Option(
        None, "--prices", help="The price list. Default: the one the configuration names."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace the files of an earlier run in --out."
    ),
) -> None:
    """Take a seat at a night from the terminal.

    You sit in place of one model seat. You are shown the text that seat's model would be given,
    word for word, and nothing more: no other seat's cards, no engine state, no reference values.
    You answer with numbers and keys. An entry the rules do not allow is refused on the spot with
    the reason and asked for again, at no cost.

    The other seats play as configured (models, mock models, rule bots). A night with a person is
    not ranked.

    The event log goes to events.jsonl, every call to a model to calls.jsonl and everything you
    type to human.jsonl. The configuration file is copied next to them as config.yaml, byte for
    byte. `auction-night replay` plays the night again from them, without a network and without
    asking you anything.
    """
    import yaml

    from auction_night.llm.accounting import PriceTable
    from auction_night.llm.nightrun import (
        CALLS_FILE,
        CONFIG_FILE,
        EVENTS_FILE,
        HUMAN_FILE,
        NightRunError,
        describe,
        load_night_config,
        play_directory,
    )
    from auction_night.llm.terminal import RichPresenter, open_input

    try:
        night_config = load_night_config(config)
        price_path = prices
        if price_path is None and night_config.limits.prices_file:
            price_path = _find_prices(night_config.limits.prices_file, config)
            if price_path is None:
                raise NightRunError(f"price list not found: {night_config.limits.prices_file}")
        if price_path is not None and not price_path.is_file():
            raise NightRunError(f"price list not found: {price_path}")
        try:
            table = PriceTable.from_file(price_path) if price_path else None
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise NightRunError(
                f"cannot read {price_path}:\n" + "\n".join(str(exc).splitlines()[:12])
            ) from None
        result = play_directory(
            night_config,
            out,
            source=open_input(),
            presenter=RichPresenter(),
            seat=human_seat,
            timeout_s=timeout,
            prices=table,
            overwrite=overwrite,
            config_file=config,
        )
    except NightRunError as exc:
        typer.echo(f"auction-night play: {exc}", err=True)
        raise typer.Exit(code=2) from None
    for line in describe(result):
        if not line.startswith("  seat "):  # the scores were shown in the account of the night
            typer.echo(line)
    typer.echo(f"saved the configuration as {out / CONFIG_FILE}")
    typer.echo(f"wrote {out / EVENTS_FILE}, {out / CALLS_FILE} and {out / HUMAN_FILE}")


@app.command()
def simulate(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="The night configuration (YAML). Its model seats are played by rule bots.",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        "-p",
        help="A named setup instead of a configuration: s1-c300 or s2. Both play 3000 nights at "
        "budget scale 300, with different seeds.",
    ),
    nights: int | None = typer.Option(
        None, "--nights", "-n", min=1, help="Nights to play. Default: 3000 for a preset, else 1000."
    ),
    seed: str | None = typer.Option(
        None, "--seed", help="Text the seeds of the nights are made from. Default: the preset's."
    ),
    jobs: int = typer.Option(
        1, "--jobs", "-j", min=1, help="Worker processes. The result does not depend on it."
    ),
    chase: bool = typer.Option(
        True, "--chase/--no-chase", help="Whether the mission holders go after their mission."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON, not as tables."),
) -> None:
    """Simulate nights with rule-based bots only: no model, nothing written.

    The model seats of the configuration are played by rule bots of three
    styles that go after their mission: E bids the textbook bid, O 5/4 of it
    and S 7/10 of it. The bot seats play as configured: the economist is P
    when it paces and E when it does not, and the truthful bot is T. The
    report gives the average surplus of each style, how often a seat spent
    nine tenths of its budget, the paired differences P-E and P-S (the surplus
    of P minus that of E, and that of P minus that of S, within a night), and
    how often each kind of mission was completed. Intervals are 95% intervals
    over nights.
    """
    import yaml

    from auction_night.contracts.config import load_config
    from auction_night.simulate import (
        DEFAULT_NIGHTS,
        DEFAULT_SEED,
        SimulationError,
        format_report,
        get_preset,
        preset_config,
        run_simulation,
    )

    def fail(message: str) -> NoReturn:
        typer.echo(f"auction-night simulate: {message}", err=True)
        raise typer.Exit(code=2)

    if (config is None) == (preset is None):
        fail("give exactly one of --config and --preset")
    try:
        if preset is not None:
            chosen = get_preset(preset)
            night_config = preset_config(chosen.c)
            source, default_nights, default_seed = chosen.name, chosen.nights, chosen.seed
        else:
            assert config is not None
            if not config.is_file():
                fail(f"configuration not found: {config}")
            try:
                night_config = load_config(config)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                fail(f"cannot read {config}:\n" + "\n".join(str(exc).splitlines()[:12]))
            source, default_nights, default_seed = (
                night_config.night.id,
                DEFAULT_NIGHTS,
                DEFAULT_SEED,
            )
        report = run_simulation(
            night_config,
            nights=default_nights if nights is None else nights,
            seed=default_seed if seed is None else seed,
            jobs=jobs,
            chase=chase,
            source=source,
        )
    except SimulationError as exc:
        fail(str(exc))
    typer.echo(report.to_json() if as_json else format_report(report))


def _fail(message: str) -> NoReturn:
    """Print an error on stderr and exit with status 2."""
    typer.echo(f"auction-night estimate: {message}", err=True)
    raise typer.Exit(code=2)


def _find_prices(name: str, config_path: Path) -> Path | None:
    """The price list that a configuration names.

    A relative name is looked for in the working directory, then in the folder of the
    configuration and in the folder above it (``configs/night.yaml`` naming
    ``configs/prices.yaml`` works from anywhere).
    """
    named = Path(name)
    if named.is_absolute():
        return named if named.is_file() else None
    for base in (Path.cwd(), config_path.parent, config_path.parent.parent):
        if (base / named).is_file():
            return named if base == Path.cwd() else base / named
    return None


@app.command()
def estimate(
    config: Path = typer.Option(..., "--config", "-c", help="The night configuration (YAML)."),
    prices: Path | None = typer.Option(
        None, "--prices", help="The price list. Default: the one the configuration names."
    ),
    nights: int = typer.Option(
        1, "--nights", min=1, help="Multiply the totals over this many nights."
    ),
    thinking_high: int = typer.Option(
        1000, "--thinking-high", min=0, help="Thinking tokens per call in the high estimate."
    ),
    token_factor_high: float = typer.Option(
        1.3, "--token-factor-high", min=1.0, help="Token count factor of Anthropic models, high."
    ),
) -> None:
    """Estimate the token and dollar cost of a night or season before running it.

    Calls and tokens come from the configuration, prices from the dated price list.

    The model seats are priced, and so are the host, the judges and the classifier that are models.

    The host is called for each lot and the recap, a judge once or twice, the classifier per batch.

    A model that has no price is listed as such. No price is ever assumed.
    """
    import yaml

    from auction_night.contracts.config import load_config
    from auction_night.llm.accounting import (
        EstimateAssumptions,
        PriceTable,
        estimate_night,
        format_estimate,
    )

    if not config.is_file():
        _fail(f"configuration not found: {config}")
    try:
        night_config = load_config(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        lines = str(exc).splitlines()
        _fail(f"cannot read {config}:\n" + "\n".join(lines[:12]))

    price_path = prices
    if price_path is None and night_config.limits.prices_file:
        price_path = _find_prices(night_config.limits.prices_file, config)
        if price_path is None:
            _fail(f"price list not found: {night_config.limits.prices_file}")
    if price_path is not None and not price_path.is_file():
        _fail(f"price list not found: {price_path}")
    try:
        table = PriceTable.from_file(price_path) if price_path else PriceTable()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _fail(f"cannot read {price_path}:\n" + "\n".join(str(exc).splitlines()[:12]))

    assumptions = EstimateAssumptions(
        thinking_tokens_high=thinking_high,
        anthropic_token_factor_high=Decimal(str(token_factor_high)),
    )
    result = estimate_night(night_config, table, assumptions=assumptions, nights=nights)
    typer.echo(format_estimate(result))


@app.command()
def season(
    dirs: list[Path] = typer.Argument(
        ...,
        metavar="DIR...",
        help="Night directories (each holds events.jsonl) or season directories that hold them.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the leaderboard as JSON, not as tables."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Also write the JSON to this file (the text of leaderboard.json)."
    ),
    resamples: int | None = typer.Option(
        None, "--resamples", min=1, help="Bootstrap resamples. Default: 10000."
    ),
    seed: str | None = typer.Option(
        None,
        "--seed",
        help="Text the resamples are drawn from. Default: season-leaderboard. The same logs, "
        "seed and number of resamples always give the same board.",
    ),
) -> None:
    """Build the season leaderboard from finished nights, with rank intervals.

    Each night directory holds events.jsonl, as `auction-night run` writes it.

    Next to it is config.yaml, the configuration of the night, which `run` and `play` save there.

    The log itself names neither the models nor the season, the deck or the rotation.

    Without config.yaml the models are read from calls.jsonl and the season is single-night.

    The main board ranks entries by mean surplus per night. Mission bonus is not included.

    Bots are ranked with the models and marked as bots.

    The rank is a 95% interval over resamples of the decks (rotation season) or of the nights.

    The footer says how large a difference between models must be to be told apart.

    A night does not count if its night_end says budget_aborted, if a person was at the table,
    if it was played in variety mode, or if its log has no night_end.

    Such a night is listed as left out, with its reason: budget_aborted, human, variety or
    no_night_end.

    The same logs, seed and number of resamples always give the same board.

    tools/check_leaderboard.py uses that to check a published leaderboard.json.
    """
    from auction_night.season import (
        DEFAULT_RESAMPLES,
        DEFAULT_SEED,
        SeasonError,
        leaderboard_for,
        leaderboard_json,
        leaderboard_text,
    )

    try:
        board = leaderboard_for(
            dirs,
            resamples=DEFAULT_RESAMPLES if resamples is None else resamples,
            seed=DEFAULT_SEED if seed is None else seed,
        )
        text = leaderboard_json(board)
        if out is not None:
            try:
                out.write_text(text, encoding="utf-8", newline="\n")
            except OSError as exc:
                raise SeasonError(f"cannot write {out}: {exc}") from None
    except SeasonError as exc:
        typer.echo(f"auction-night season: {exc}", err=True)
        raise typer.Exit(code=2) from None
    if as_json:
        typer.echo(text, nl=False)
    else:
        typer.echo(leaderboard_text(board))
        if out is not None:
            typer.echo(f"wrote {out}")


def _checkout() -> Path | None:
    """The git checkout that this code runs from, or None for an installed copy.

    The manifest of an export names the commit of the code that made it. An installed copy has no
    commit of its own, and a repository that merely contains the environment is not its checkout.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "pyproject.toml").is_file() else None


@app.command()
def export(
    run_dir: Path = typer.Argument(
        ...,
        metavar="RUN_DIR",
        help="The directory of the run: events.jsonl and calls.jsonl, and human.jsonl for a "
        "night with a person.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="The night configuration (YAML). Default: config.yaml in RUN_DIR.",
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="The file to write. Default: replay.json in RUN_DIR."
    ),
    host_calls: Path | None = typer.Option(
        None,
        "--host-calls",
        help="The recording of the offline host. Default: host_calls.jsonl in RUN_DIR. Only a "
        "host that is a model needs one.",
    ),
    judge_calls: Path | None = typer.Option(
        None,
        "--judge-calls",
        help="The recording of the model judges. Default: judge_calls.jsonl in RUN_DIR. Only "
        "judges that are models need one.",
    ),
    scan_calls: Path | None = typer.Option(
        None,
        "--scan-calls",
        help="The recording of the content classifier. Default: scan_calls.jsonl in RUN_DIR. "
        "Only a classifier that is a model needs one.",
    ),
    wordlist: Path | None = typer.Option(
        None,
        "--wordlist",
        help="A word list for the content gate, besides the one the configuration names. One "
        "term per line, # starts a comment, re: marks a regular expression.",
    ),
    clear: Path | None = typer.Option(
        None,
        "--clear",
        help="A TOML file of the findings of the content gate that a person has read and decided "
        "to publish, one clear table for each, with path, category and reason. The export goes "
        "on when every finding is listed.",
    ),
    reveal_seed: bool = typer.Option(
        False,
        "--reveal-seed",
        help="Publish the deal seed in the manifest. It must open the seed commitment.",
    ),
    created_utc: str | None = typer.Option(
        None,
        "--created-utc",
        help="The time stamp for the manifest. Default: the time of the last recorded call, so "
        "that the same directory always exports to the same bytes.",
    ),
    no_git: bool = typer.Option(
        False, "--no-git", help="Leave the git commit of this code out of the manifest."
    ),
) -> None:
    """Export a recorded night as replay.json, the file the viewer plays.

    RUN_DIR is a directory that `auction-night run` or `play` wrote.

    The night is played again from its cassette, with no model and no network.

    Its log must equal events.jsonl byte for byte, or the night is refused.

    Then the fact cards, the offline host and the judges are run, and the replay is assembled.

    Every public text in the replay goes through the content gate before the file is written.

    A finding that a person has read and decided to publish is released with --clear FILE.

    FILE is TOML with one clear table for each finding, with path, category and reason.

    The path is written as the report of the gate writes it, for example host.per_lot[0].text.

    The manifest of the replay lists the findings that were cleared, with the reason given.

    Two kinds of finding of the classifier are released by rule, and need no FILE.

    One is a game word (a mission kind or a tag) in an extraction of a judge.

    The other is a real_person finding that names only code names of the night.

    The manifest lists them apart, in auto_cleared_findings, and this command prints them.

    The configuration is --config, else config.yaml in RUN_DIR.

    A deal seed that lives in an environment variable (seed_env) must be set, as for the run.

    A host, judge or content classifier that is a model is read from its recording, never called.

    Exit status 0: the file is written.

    Exit status 1: the night is refused and no file is written.

    Either the log or a recording does not match, or the log breaks a premise of the viewer.

    Or the content gate found something that FILE does not clear, and the places are listed.

    Exit status 2: a usage error, or a file that cannot be read.

    That includes a --clear file that is wrong or lists a finding that the scan did not report.
    """
    from pydantic import TypeAdapter, ValidationError

    from auction_night.contracts.config import SeedResolutionError
    from auction_night.contracts.events import Name
    from auction_night.llm.cassette import CassetteError
    from auction_night.llm.nightrun import NightRunError, config_path_for, load_night_config
    from auction_night.show.clearance import Clearance, ClearanceError, load_clearances
    from auction_night.show.content_scan import ContentScanError, ContentScanRejected
    from auction_night.show.replay_export import (
        ExportError,
        GitInfo,
        RecordingNotFound,
        export_directory,
        git_info,
    )

    def refuse(status: int, message: str) -> NoReturn:
        typer.echo(f"auction-night export: {message}", err=True)
        raise typer.Exit(code=status) from None

    if not run_dir.is_dir():
        refuse(2, f"not a directory: {run_dir}")
    if created_utc is not None:
        try:
            TypeAdapter(Name).validate_python(created_utc)
        except ValidationError:
            refuse(2, "--created-utc is one line of 1 to 64 characters")
    clearances: tuple[Clearance, ...] = ()
    if clear is not None:
        try:
            clearances = load_clearances(clear)
        except ClearanceError as exc:
            refuse(2, str(exc))
    try:
        config_path = config_path_for(run_dir, config)
        night_config = load_night_config(config_path)
        night_config.night.resolve_seed()
    except (NightRunError, SeedResolutionError) as exc:
        refuse(2, str(exc))
    checkout = _checkout()
    try:
        done = export_directory(
            night_config,
            run_dir,
            host_calls=host_calls,
            judge_calls=judge_calls,
            scan_calls=scan_calls,
            wordlist=wordlist,
            search_dirs=(config_path.parent, config_path.parent.parent),
            git=GitInfo() if no_git or checkout is None else git_info(checkout),
            created_utc=created_utc,
            reveal_seed=reveal_seed,
            clearances=clearances,
        )
    except ContentScanRejected as exc:
        refuse(1, "\n".join(exc.report.lines()))
    except (ContentScanError, ClearanceError, RecordingNotFound) as exc:
        refuse(2, str(exc))
    except CassetteError as exc:
        refuse(1, f"a recording does not fit this night: {exc}")
    except (ExportError, NightRunError) as exc:
        refuse(1, str(exc))
    except ValidationError as exc:
        refuse(1, "the replay is not valid:\n" + "\n".join(str(exc).splitlines()[:12]))
    target = out if out is not None else run_dir / REPLAY_FILE
    scratch = target.with_name(target.name + ".part")
    try:
        scratch.write_text(done.text, encoding="utf-8", newline="\n")
        os.replace(scratch, target)
    except OSError as exc:
        scratch.unlink(missing_ok=True)
        refuse(2, f"cannot write {target}: {exc}")
    size = len(done.text.encode("utf-8"))
    typer.echo(
        f"wrote {target} ({size} bytes, {len(done.replay.timeline)} lots); {done.report.summary()}"
    )
    for record in done.replay.manifest.auto_cleared_findings:
        typer.echo(f"  cleared by rule: {record.path} ({record.category}) {record.reason}")


@app.command()
def show(
    run_dir: Path = typer.Argument(
        ...,
        metavar="RUN_DIR",
        help="The directory of the run: events.jsonl and calls.jsonl, and human.jsonl for a "
        "night with a person.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="The night configuration (YAML). Default: config.yaml in RUN_DIR.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace the recordings of an earlier `show` in RUN_DIR."
    ),
    prices: Path | None = typer.Option(
        None, "--prices", help="The price list. Default: the one the configuration names."
    ),
) -> None:
    """Call the models of the show layer on a finished night, and record what they say.

    `run` and `play` record only the models in the seats (calls.jsonl).

    The host, the two judges and the content classifier work after the night.

    A role that is a model (kind: llm) is read by `export` from a recording, and never called there.

    The recordings are host_calls.jsonl, judge_calls.jsonl and scan_calls.jsonl in RUN_DIR.

    This command calls those roles and writes those files, so that `export RUN_DIR` needs no option.

    The night is played again from calls.jsonl, so the seats call nothing.

    A role that is a mock runs on the spot, as in an export, and leaves no file.

    The configuration is --config, else config.yaml in RUN_DIR. The keys come from the environment.

    The calls are priced from the price list: --prices, else the file the configuration names.

    limits.max_cost_per_night_usd also caps the show layer, which is held to it on its own.

    The command stops at the cap. A cap needs a price for every model that is called.

    A recording that is already there is refused unless --overwrite is given.

    The files are written only when every call got an answer and `export` can read them back.

    Exit status 0: the files are written, or no role is a model.

    A content scan that finds something does not change that: `export` refuses such a night.

    Exit status 1: a role got no answer, the cost cap stopped the calls, or the night differs.

    No file is written then.

    Exit status 2: a usage error, a recording in the way, a missing key or price, or a bad file.
    """
    import yaml
    from pydantic import ValidationError

    from auction_night.contracts.config import SeedResolutionError
    from auction_night.llm.accounting import PriceTable
    from auction_night.llm.cassette import CassetteError
    from auction_night.llm.nightrun import NightRunError, config_path_for, load_night_config
    from auction_night.show.content_scan import ContentScanError
    from auction_night.show.replay_export import ExportError
    from auction_night.show.showrun import ShowFailed, ShowSetupError, describe_show, record_show

    def refuse(status: int, message: str) -> NoReturn:
        typer.echo(f"auction-night show: {message}", err=True)
        raise typer.Exit(code=status) from None

    if not run_dir.is_dir():
        refuse(2, f"not a directory: {run_dir}")
    try:
        config_path = config_path_for(run_dir, config)
        night_config = load_night_config(config_path)
        night_config.night.resolve_seed()
        price_path = prices
        if price_path is None and night_config.limits.prices_file:
            price_path = _find_prices(night_config.limits.prices_file, config_path)
            if price_path is None:
                raise NightRunError(f"price list not found: {night_config.limits.prices_file}")
        if price_path is not None and not price_path.is_file():
            raise NightRunError(f"price list not found: {price_path}")
        try:
            table = PriceTable.from_file(price_path) if price_path else PriceTable()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise NightRunError(
                f"cannot read {price_path}:\n" + "\n".join(str(exc).splitlines()[:12])
            ) from None
        done = record_show(
            night_config,
            run_dir,
            prices=table,
            overwrite=overwrite,
            search_dirs=(config_path.parent, config_path.parent.parent),
        )
    except (NightRunError, SeedResolutionError, ShowSetupError, ContentScanError) as exc:
        refuse(2, str(exc))
    except ShowFailed as exc:
        refuse(1, str(exc))
    except CassetteError as exc:
        refuse(1, f"a recording does not fit this night: {exc}")
    except ExportError as exc:
        refuse(1, str(exc))
    except ValidationError as exc:
        refuse(1, "the replay is not valid:\n" + "\n".join(str(exc).splitlines()[:12]))
    for line in describe_show(done):
        typer.echo(line)
    if done.files:
        names = [str(path) for path in done.files]
        joined = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
        typer.echo(f"wrote {joined}")


# ------------------------------------------------------------------------------------- seeds

seeds_app = typer.Typer(
    name="seeds",
    help="Seed commitments of a season: draw, plan, reveal and verify the deal seeds.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(seeds_app, name="seeds")


def _seeds_refuse(command: str, status: int, message: str) -> NoReturn:
    typer.echo(f"auction-night seeds {command}: {message}", err=True)
    raise typer.Exit(code=status)


@seeds_app.command("new")
def seeds_new(
    season: str = typer.Option(..., "--season", help="The season id, for example s1."),
    decks: int | None = typer.Option(
        None, "--decks", min=1, max=999, help="Draw this many decks, named d01, d02 and so on."
    ),
    deck: list[str] | None = typer.Option(
        None, "--deck", help="Name a deck. Repeat it for each deck, instead of --decks."
    ),
    private_out: Path = typer.Option(
        ...,
        "--private-out",
        help="Where the private seed file goes. It must lie outside every git work tree, and it "
        "is never replaced. Suggested name: <season>.seeds-private.json.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="The public commitments file. Default: seasons/<season>/commitments.json.",
    ),
    created_utc: str | None = typer.Option(
        None,
        "--created-utc",
        help="The time stamp for the files, like 2026-11-01T12:00:00Z. Default: now.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace an earlier commitments file (never the private file)."
    ),
) -> None:
    """Draw the secret seeds of a season and the commitments to publish.

    Each deck gets a seed of 256 random bits from the secrets module of Python.

    The seeds go to the private file, which only you can read (mode 600) and which is written
    only if it does not exist yet and lies outside every git work tree.

    The SHA-256 commitment of each seed goes to the commitments file. Publish that file before
    the first night of the season.

    Keep the private file secret until the season is over. `seeds env` reads it to give a night
    its seed, and `seeds reveal` reads it to open the commitments.
    """
    from auction_night import seeds

    if (decks is None) == (not deck):
        _seeds_refuse("new", 2, "give exactly one of --decks and --deck")
    try:
        stamp = seeds.parse_stamp(created_utc) if created_utc is not None else seeds.now_stamp()
        ids = seeds.deck_ids(decks) if decks is not None else list(deck or [])
        private, public = seeds.new_season(season, ids, created_utc=stamp)
        target = out if out is not None else Path("seasons") / season / seeds.COMMITMENTS_FILE
        written = seeds.write_new_season(
            private, public, private_out=private_out, out=target, overwrite=overwrite
        )
    except seeds.SeedsError as exc:
        _seeds_refuse("new", 2, str(exc))
    typer.echo(f"season {season}: {len(ids)} decks ({', '.join(ids)})")
    typer.echo(f"wrote {target}: publish it before the first night")
    typer.echo(f"wrote {written} (mode 600): keep it secret until the season is over")


@seeds_app.command("plan")
def seeds_plan(
    template: Path = typer.Option(
        ..., "--template", "-t", help="The season template: a night configuration (YAML)."
    ),
    commitments: Path = typer.Option(
        ..., "--commitments", "-c", help="The published commitments file of the season."
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Directory for the configurations. Default: configs next to the commitments file.",
    ),
    deck: list[str] | None = typer.Option(
        None, "--deck", help="Plan only this deck. Repeat it for more. Default: every deck."
    ),
    rotations: int | None = typer.Option(
        None,
        "--rotations",
        min=1,
        max=8,
        help="Plan only the first rotations of each deck. Default: one per model seat.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace a configuration that exists and differs."
    ),
) -> None:
    """Write the configuration of every night of a rotation season.

    The template is an ordinary night configuration. For each deck and rotation the plan changes
    its night id, its season section and the players of the model seats, which shift by one seat
    in every rotation (the one at the first model seat goes round to the last), so that over the
    rotations every player sits at every seat once.

    Each configuration names the environment variable that holds the seed of its deck
    (night.seed_env) and carries the commitment of the deck. The seed is never written into it,
    and the plan never reads the private file.

    Play a night with `eval "$(auction-night seeds env --private FILE --deck d01)"` and then
    `auction-night run`.
    """
    from auction_night import seeds

    try:
        published = seeds.read_commitments(commitments)
        planned, notes = seeds.plan_season(template, published, decks=deck, rotations=rotations)
        target = out_dir if out_dir is not None else commitments.parent / "configs"
        written = seeds.write_planned(target, planned, overwrite=overwrite)
    except seeds.SeedsError as exc:
        _seeds_refuse("plan", 2, str(exc))
    typer.echo(f"season {published.season}: {len(planned)} nights in {target}")
    for night in planned:
        typer.echo(
            f"  {night.night}  deck {night.deck}  rotation {night.rotation}  "
            f"seed from {night.seed_env}"
        )
    typer.echo(f"wrote {len(written)} files, {len(planned) - len(written)} were already there")
    for note in notes:
        typer.echo(f"note: {note}", err=True)


@seeds_app.command("env")
def seeds_env(
    private: Path = typer.Option(..., "--private", help="The private seed file."),
    deck: list[str] | None = typer.Option(
        None, "--deck", help="The deck whose seed to export. Repeat it for more."
    ),
) -> None:
    """Print the export line that gives a night the seed of its deck.

    The line sets the variable that night.seed_env of the planned configuration names. Run it
    in the shell that runs the night: `eval "$(auction-night seeds env --private FILE --deck d01)"`.

    This command prints a secret, so it asks for each deck by name.
    """
    from auction_night import seeds

    if not deck:
        _seeds_refuse("env", 2, "name at least one deck with --deck")
    try:
        lines = seeds.export_lines(seeds.read_private(private), deck)
    except seeds.SeedsError as exc:
        _seeds_refuse("env", 2, str(exc))
    for line in lines:
        typer.echo(line)


@seeds_app.command("reveal")
def seeds_reveal(
    private: Path = typer.Option(..., "--private", help="The private seed file."),
    commitments: Path = typer.Option(
        ..., "--commitments", "-c", help="The published commitments file of the season."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="The seeds file to write. Default: seeds.json next to the commitments file.",
    ),
    revealed_utc: str | None = typer.Option(
        None,
        "--revealed-utc",
        help="The time stamp for the file, like 2026-11-01T12:00:00Z. Default: now.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an earlier seeds file."),
) -> None:
    """Open the commitments of a season with the seeds in the private file.

    Every seed must open the commitment published for its deck, and every committed deck must
    have a seed. If one does not, nothing is written and the exit status is 1.

    Otherwise the seeds are written to the seeds file, which is public. Publish it only after
    the last night of the season.
    """
    from auction_night import seeds

    try:
        stamp = seeds.parse_stamp(revealed_utc) if revealed_utc is not None else seeds.now_stamp()
        published = seeds.read_commitments(commitments)
        secret = seeds.read_private(private)
        target = out if out is not None else commitments.parent / seeds.REVEAL_FILE
        if target.exists() and not overwrite:
            raise seeds.SeedsError(f"{target} already exists (use --overwrite to replace it)")
        opened = seeds.reveal_season(secret, published, revealed_utc=stamp)
        seeds.write_public(target, opened, overwrite=overwrite)
    except seeds.SeedsMismatch as exc:
        _seeds_refuse("reveal", 1, str(exc))
    except seeds.SeedsError as exc:
        _seeds_refuse("reveal", 2, str(exc))
    typer.echo(f"season {published.season}: all {len(opened.decks)} seeds open their commitments")
    typer.echo(f"wrote {target}: publish it only after the last night of the season")


@seeds_app.command("verify")
def seeds_verify(
    nights: list[Path] | None = typer.Argument(
        None,
        metavar="[NIGHT]...",
        help="Run directories (events.jsonl, config.yaml) or replay files, or directories "
        "that hold them.",
    ),
    commitments: Path = typer.Option(
        ..., "--commitments", "-c", help="The commitments file published before the season."
    ),
    revealed: Path | None = typer.Option(
        None, "--seeds", "-s", help="The seeds file published after the season."
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Count a check that cannot be made for lack of a file as a failure. Needs --seeds.",
    ),
) -> None:
    """Check the commitments of a season against the seeds and the nights.

    With --seeds, every seed must open the commitment published for its deck.

    Every night given must carry the commitment published for its deck, in its event log
    (seed_commit), its configuration (night.deal_seed_commitment) and its replay
    (manifest.seed_commitment), whichever of them it has.

    With --seeds, the cards of each night are drawn again from the seed of its deck with the open
    dealer and compared with the private cards in the log, and with the reveal in the replay.

    Exit status 0: every check passed. 1: a check failed. 2: a file cannot be used.
    """
    from auction_night import seeds

    if strict and revealed is None:
        _seeds_refuse("verify", 2, "--strict needs --seeds")
    try:
        published = seeds.read_commitments(commitments)
        opened = seeds.read_revealed(revealed) if revealed is not None else None
        report = seeds.verify(published, opened, seeds.discover_nights(nights or []), strict=strict)
    except seeds.SeedsError as exc:
        _seeds_refuse("verify", 2, str(exc))
    for line in report.lines():
        typer.echo(line)
    if report.failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
