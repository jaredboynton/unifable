#!/usr/bin/env python3
"""Evidence spec including frontier rejected_approach resolution."""

def resolve_frontier(task):
    if task.get("outcome") == "rejected_approach":
        task["status"] = "rejected_approach"
