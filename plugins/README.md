# Elren external plugins

Place a Python file in this directory and subclass `deepdesk.plugins.ToolPlugin`.
Every public subclass with a no-argument constructor is discovered at startup.

Security note: plugins run in the local Elren process and therefore have the same OS permissions.
Only install code you trust.

See `example_time.py.disabled` for the smallest complete example. Rename it to `example_time.py`
to enable it.
