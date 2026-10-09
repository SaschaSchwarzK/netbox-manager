import subprocess

import pytest
from github import GithubException

from app.services import github_repo


@pytest.mark.parametrize(
    "path",
    [
        "device-types/Palo Alto/pa-220.yml",
        "device-types/München/网络 交换机.yml",
        "device-types/vendor/" + "x" * 400 + ".yml",
        "device-types/vendor/a~b^c:d?e*f[g\\h.yml",
    ],
)
def test_feature_branch_names_are_valid_git_refs(path):
    branch = github_repo._feature_branch_name(path)
    assert len(branch) <= 240
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True, capture_output=True)


def test_feature_branch_names_do_not_collide_when_flattened_paths_match():
    first = github_repo._feature_branch_name("device-types/a/b-c.yml")
    second = github_repo._feature_branch_name("device-types/a-b/c.yml")
    assert first != second


def test_feature_branch_preserves_type_prefixes():
    assert github_repo._feature_branch_name("device-types/A/a.yml").startswith("device-type/")
    assert github_repo._feature_branch_name("module-types/A/a.yml").startswith("module-type/")
    assert github_repo._feature_branch_name("rack-types/A/a.yml").startswith("rack-type/")


def test_ensure_branch_reraises_422_when_branch_does_not_exist():
    create_error = GithubException(422, {"message": "Validation Failed"}, None)
    missing_error = GithubException(404, {"message": "Not Found"}, None)

    class Repo:
        def create_git_ref(self, **kwargs):
            raise create_error

        def get_branch(self, branch):
            raise missing_error

    with pytest.raises(GithubException) as caught:
        github_repo._ensure_branch(Repo(), "device-type/test-12345678", "abc")
    assert caught.value is create_error


def test_ensure_branch_reuses_confirmed_existing_branch():
    class Repo:
        def create_git_ref(self, **kwargs):
            raise GithubException(422, {"message": "Validation Failed"}, None)

        def get_branch(self, branch):
            return object()

    github_repo._ensure_branch(Repo(), "device-type/test-12345678", "abc")
