"""Provider adapters and the abstraction they share (ARCHITECTURE.md §7).

`base.py` holds the pure pieces — the call contract, model→provider routing, and
the normalized result shape. Concrete adapters (`openai.py`, `anthropic.py`) that
call the SDKs live alongside it and are covered by integration tests.
"""
