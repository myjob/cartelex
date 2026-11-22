"""End-to-end orchestrator behind ``batch.Main_Main_Pipeline``.

Modules: ``models`` (run plan), ``state_store`` (JSON persistence and lock),
``prerequisites`` (checks that must pass), ``planner`` (what is stale),
``runner`` (batched subprocess execution with progress).
"""
