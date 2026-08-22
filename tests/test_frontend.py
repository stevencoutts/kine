"""Small presentation invariants for the single-file Helm UI."""
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "helm" / "frontend" / "index.html").read_text()


def test_visible_control_labels_use_title_case():
    lower_case_labels = (
        ">First run<",
        ">Sign in<",
        ">Finish setup<",
        ">Check now<",
        ">Run leak test<",
        ">Restart tunnel group<",
        ">Update check<",
        ">Config backup<",
    )
    for label in lower_case_labels:
        assert label not in FRONTEND
