"""The model layer: what is sent to a model and how the answer is read.

Modules:

* ``capabilities``  what an endpoint and a model accept, and the checks that follow from it
* ``request``       exact request payloads for the Anthropic and OpenAI SDKs, and the request hash
* ``render``        system prompt and observation of a seat, rendered from the event log
* ``sanitize``      cleaning of model-written text before it becomes an event or a prompt line
* ``parse``         the parsing ladder that turns a reply into one validated action
* ``jsonish``       the forgiving reader for JSON-like objects that the ladder uses
* ``amounts``       amounts as models write them: units, currency words, Chinese numerals
* ``counters``      repair, clamp and invalid-input counters of the ladder
* ``client``        thin wrappers around the vendor SDKs and the normalised reply they return
* ``cassette``      every attempt of a night, recorded once and replayed byte for byte
* ``accounting``    prices, the cost of a call, the cost gate and the estimate
* ``orchestrate``   the phases of a night: concurrency, retries, timeouts, the cassette and the gate
* ``driver``        the night state machine, played by models and rule bots
* ``mock``          scripted seats behind the client interface, with faults, for tests and CI
* ``human``         a person at a seat: numbers and keys, the same words as a model, a record
* ``terminal``      the keyboard with a time limit and the rich screen of a person at a seat
* ``personas``      the personas that a seat of a variety night can take
* ``nightrun``      running a configured night, recording it and replaying it from its cassette

Only the SDK wrappers of ``client`` can make a network call, and only when a night is run with
``llm`` seats. The request builders return keyword arguments for the SDK calls and leave the calls
themselves to the caller. A replay and a night of ``mock`` seats never touch the network.
A person at a seat is shown the text a model at that seat would be given and answers with numbers
and keys; the answers are recorded next to the cassette, so a replay asks nobody.
"""
