import re
from pathlib import Path

from app.config import Settings


def test_env_example_documents_every_nbm_setting():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    documented = set(re.findall(r"(?m)^#?(NBM_[A-Z0-9_]+)=", example))
    expected = {
        f"NBM_{name.upper()}"
        for name, field in Settings.model_fields.items()
        if field.validation_alias != "AUTHENTICATION_DISABLED"
    }
    assert expected - documented == set()
