"""The show layer: fact cards, the economics lesson, the prediction card and the offline host.

Modules:

* ``numbers_check``  numbers in a text: finding, normalising, checking them against a card
* ``pledges``        structured pledges, judged by program from the event log
* ``cards``          the structured data of one lot
* ``candidates``     highlight candidates of a lot (unscored)
* ``lesson``         the economics lesson of the night, chosen by a fixed priority
* ``predict``        the pre-show prediction card and its settlement after the night
* ``render``         the Chinese text of a lot card, and its headline
* ``factcard``       fact cards of a finished night: data, text and the numbers on them
* ``host``           the offline host: prompts, number check, rewrite, fallback, cassette replay
* ``judge_input``    the record the judges read: public facts only, seats and lots from events
* ``judges``         the two judges: prompts, reading and checking their answers, retry, cassette
* ``truth``          the hole cards of a finished night, read from its log
* ``claims``         claims about one's own cards: the same claim, and when it is false
* ``screening``      the bid statistic for collusion and the threshold that rule bots set
* ``verdicts``       broken pledges, false claims and the collusion verdict, as contract data
* ``judge_board``    how well each judge guessed the missions, and how often the two agree
* ``awards``         the ten awards for the model seats, the bots as a reference, seat metrics
* ``highlights``     the moments of the night, scored 0 to 10, with the events to jump to
* ``replay_export``  the replay of a night: assembly, checks against the contract, the file
* ``content_scan``   the gate before publishing: word lists and an optional classifier
* ``showrun``        calling the models of the show layer after a night, and recording their calls

Entry points, in the order a night uses them::

    cards = build_fact_cards(state)                 # factcard: one card per lot, one per night
    host = make_host(config, cassette=..., client=...)   # host: mock, or a model on the cassette
    output = run_host(cards, host)                  # host: commentary, events, replay block
    state, events = append_events(state, output.events)   # engine: into the log (audience only)

    judges = make_judges(config, cassette=..., clients=...)   # judges: mock, or models
    judging = run_judging(state, judges)            # verdicts: extraction, checks, every finding
    state, events = append_events(state, judging.events)   # judge_extract, judge_verdict

    awards = compute_awards(cards, judging.verdicts)          # awards: models, bots, metrics
    moments = compute_highlights(cards, state.events, judging.verdicts)   # highlights

``output.block`` is the ``host`` part of the replay. The settled predictions are in
``cards.night.data.predictions`` and the lesson in ``cards.night.data.lesson``.
``judging.pledges``, ``judging.judges``, ``judging.verdicts`` and ``judging.accuracy`` are the
replay parts ``pledges``, ``judges``, ``verdicts`` and ``judge_accuracy``.

``replay_export.show_parts(config, state, cards)`` runs the judges, the awards and the highlights
in one call and returns them as the parts of the replay (with the judges that gave no usable
answer, ``judges_absent``, and the statistic columns of the scores). ``export_directory`` calls it,
so an exported night carries all of them.
"""
