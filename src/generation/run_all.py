"""
Regenerate ALL synthetic data from scratch, in the correct order.

Usage:  python -m src.generation.run_all

Every step is seeded, so the output is identical on every run and every machine
(given the same config files). Each step depends on the outputs of the ones before it.
"""

import time

from src.generation import (corruption, employees, master, movements, recruitment,
                            reference_data, snapshots)

STEPS = [
    ("Reference data (departments, offices)", reference_data.main),
    ("Employees at hire", employees.main),
    ("Movements (renewals, exits, promotions, transfers)", movements.main),
    ("Monthly workforce snapshots", snapshots.main),
    ("Employee master with supervisors", master.main),
    ("Recruitment linked to hires", recruitment.main),
    ("Office submissions with injected problems", corruption.main),
]


def main() -> None:
    start = time.time()
    for n, (name, step) in enumerate(STEPS, start=1):
        print(f"\n{'=' * 70}\nSTEP {n}/{len(STEPS)}: {name}\n{'=' * 70}")
        step()
    print(f"\nAll synthetic data regenerated in {time.time() - start:.0f} seconds.")


if __name__ == "__main__":
    main()