"""The auction engine: pure functions over integers, no I/O and no clock.

Modules:

* ``rng``         independent integer streams derived with hashlib from the night seed
* ``deal``        every private card of a night, drawn from those streams
* ``formats``     the six auction formats: settlement and step functions
* ``rules_text``  the Chinese rule texts and payment examples, generated from the mechanism
                  definitions and the settlement functions
* ``night``       the state machine of a whole night: ``start`` deals and opens, ``step`` moves on
* ``muted``       the actions that the cost gate skipped, read off a log
* ``missions``    the secret missions, decided from the hammers after the last lot
* ``scoring``     surplus, mission bonus and total of every seat, kept apart
"""
