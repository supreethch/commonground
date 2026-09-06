"""Services: the layer that knows about both the database and the engine.

The engine is deliberately ignorant of persistence (docs/architecture.md), so
something has to load rows, build matrices, call the ranker and write the result
back. That translation lives here rather than in the routers, so an endpoint
stays a thin thing that validates input and returns JSON.
"""
