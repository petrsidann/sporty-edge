# --------------------------------------------------------------------------- #
# Console / diagnostics helpers
# --------------------------------------------------------------------------- #

def postmortem_main() -> None:
    """python -m postmortem -- run the ledger post-mortem tables."""
    from postmortem import main as pm

    pm()


def calibration_main() -> None:
    """python -m utils.calibration -- stated-vs-actual calibration report."""
    from utils.calibration import main as cal

    cal()


def strength_engine_main() -> None:
    """python -m models.strength_engine -- ratings from data/history.csv."""
    from models.strength_engine import main as sm

    sm()