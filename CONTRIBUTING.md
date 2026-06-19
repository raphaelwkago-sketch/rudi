# Contributing to Rudi

Thanks for your interest in Rudi. Contributions are welcome — bug reports, benchmarks, fixes, and ideas.

## Contributor License Agreement (CLA)

By submitting a pull request, you agree that:

1. You are the original author of your contribution, or have the right to submit it.
2. You grant the project maintainer a perpetual, irrevocable license to use, modify, and **relicense** your contribution, including under commercial terms.

This is what lets Rudi stay AGPL-3.0 for the community while offering a separate commercial license to organizations that need one. It's the same dual-licensing model used by MongoDB, Grafana, and others. Your contribution stays open source forever; this agreement just keeps the commercial option on the table for the project.

If you're not comfortable with this, open an issue instead — design discussion and bug reports don't require the CLA.

## How to contribute

1. Open an issue describing the change before large work, so we can agree on direction.
2. Fork, branch, and keep PRs focused on one thing.
3. If you touch the graph or fold logic, run `python benchmark_long_haiku.py` and include the token curve + callback results in your PR.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, and the relevant log lines.
