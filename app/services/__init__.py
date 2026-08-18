"""Business logic that is not tied to one HTTP endpoint.

Anything in here must be callable from a router, a test, or a management command without
touching FastAPI. Endpoints stay thin; the rules live where more than one caller can reach
them, which is the whole point of CLAUDE.md §5.1's insistence on *one* rate lookup helper.
"""
