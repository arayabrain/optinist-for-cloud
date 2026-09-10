"""Provenance resolution: a git checkout state -> the recorded fields.

Runs the real infrastructure/scripts/git_ref_info.sh against throwaway
repositories built per test, rather than asserting on a transcript. The
regression it guards is narrow and easy to reintroduce: `git rev-parse
--abbrev-ref HEAD` reports the literal string "HEAD" for a detached HEAD, so
every deployment made from a tag recorded a branch called HEAD and lost the tag.

It also pins the field names, because three separate readers agree on them by
convention alone — the Dockerfile writes /app/BUILD_INFO, studio/app/version.py
reads it back, and compute.tf stamps the terraform side onto the ECS cluster.

The rule that turns these fields into a readable ref is covered by
studio/tests/app/test_version_build_info.py.
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

# Same layout handling as test_compute_config.py: this file sits at
# studio/tests/infrastructure/, so the project root is four levels up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "infrastructure" / "scripts"
GIT_REF_INFO = SCRIPTS_DIR / "git_ref_info.sh"
TF_BUILD_INFO = SCRIPTS_DIR / "terraform_build_info.sh"
ECR_BUILD_PUSH = SCRIPTS_DIR / "ecr_build_push.sh"
DOCKERFILE = PROJECT_ROOT / "studio" / "config" / "docker" / "Dockerfile"
COMPUTE_TF = PROJECT_ROOT / "infrastructure" / "terraform" / "compute.tf"

# The keys that travel from the Dockerfile through BUILD_INFO into version.py.
BUILD_INFO_KEYS = ("git_commit", "git_branch", "git_tag", "build_timestamp")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to build the fixtures"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def resolve(repo_dir: Path) -> dict:
    """Source the real script and return the three variables it sets."""
    script = (
        f". {shlex.quote(str(GIT_REF_INFO))}\n"
        f"resolve_git_ref_info {shlex.quote(str(repo_dir))}\n"
        'printf "%s\\n%s\\n%s\\n" '
        '"$GIT_INFO_COMMIT" "$GIT_INFO_BRANCH" "$GIT_INFO_TAG"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    commit, branch, tag = result.stdout.split("\n")[:3]
    return {"commit": commit, "branch": branch, "tag": tag}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a branch, tags, and a commit carrying several tags.

    The scripts under test are committed before the first tag, so they survive
    every checkout the tests make — including the detached ones.
    """
    r = tmp_path / "fixture"
    scripts = r / "infrastructure" / "scripts"
    scripts.mkdir(parents=True)
    for script in (GIT_REF_INFO, TF_BUILD_INFO):
        shutil.copy(script, scripts / script.name)

    _git(r, "init", "-q", "-b", "main-line")
    _git(r, "config", "user.email", "tester@example.com")
    _git(r, "config", "user.name", "Tester")

    def commit(text: str) -> None:
        (r / "f.txt").write_text(text)
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", text)

    commit("c1")
    commit("c2")
    _git(r, "tag", "v0.9.0")
    commit("c3")
    # Deliberately out of lexical order, and with an annotated tag alongside:
    # v1.0.10 sorts below v1.0.9 as a string, and `git describe` would prefer
    # the annotated one over either.
    _git(r, "tag", "v1.0.9")
    _git(r, "tag", "v1.0.10")
    _git(r, "tag", "-a", "ann-tag", "-m", "annotated")
    commit("c4")
    _git(r, "branch", "-q", "release", "v1.0.10")
    return r


class TestResolution:
    """Which fields each checkout state produces."""

    def test_branch_checkout(self, repo):
        info = resolve(repo)
        assert info["branch"] == "main-line"
        assert info["tag"] == ""

    def test_tag_checkout_records_the_tag_and_no_branch(self, repo):
        # The reported defect. Before the fix this produced branch == "HEAD"
        # and no tag at all.
        _git(repo, "checkout", "-q", "v0.9.0")
        info = resolve(repo)
        assert info["branch"] == ""
        assert info["tag"] == "v0.9.0"
        assert info["commit"] == _git(repo, "rev-parse", "HEAD")

    def test_branch_whose_head_carries_a_tag_records_both(self, repo):
        _git(repo, "checkout", "-q", "release")
        info = resolve(repo)
        assert info["branch"] == "release"
        assert info["tag"] == "v1.0.10"

    def test_several_tags_on_one_commit_pick_the_highest_version(self, repo):
        # `--sort=-v:refname` makes this deterministic. Lexical order would
        # give v1.0.9, and `git describe --tags` would give ann-tag.
        _git(repo, "checkout", "-q", "v1.0.10")
        assert resolve(repo)["tag"] == "v1.0.10"

    def test_detached_at_an_untagged_commit(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", sha)
        info = resolve(repo)
        assert info["branch"] == ""
        assert info["tag"] == ""
        assert info["commit"] == sha

    def test_shallow_tag_clone_still_finds_the_tag(self, repo, tmp_path):
        # This is why there is no `git describe` fallback: cloning at a tag
        # creates the ref for it even under --no-tags, so the primary path
        # always answers.
        shallow = tmp_path / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--no-tags",
                "-b",
                "v0.9.0",
                f"file://{repo}",
                str(shallow),
            ],
            capture_output=True,
            check=True,
        )
        info = resolve(shallow)
        assert info["branch"] == ""
        assert info["tag"] == "v0.9.0"

    def test_outside_a_git_worktree(self, tmp_path):
        info = resolve(tmp_path)
        assert info == {"commit": "unknown", "branch": "", "tag": ""}


class TestTerraformBuildInfo:
    """What data.external.tf_build_info receives at apply time."""

    def _run(self, scripts_dir: Path, cwd: Path) -> dict:
        result = subprocess.run(
            ["bash", str(scripts_dir / "terraform_build_info.sh")],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            check=True,
        )
        return json.loads(result.stdout)

    def test_emits_the_tag_from_a_tag_checkout(self, repo, tmp_path):
        _git(repo, "checkout", "-q", "v0.9.0")
        out = self._run(repo / "infrastructure" / "scripts", tmp_path)
        assert out["git_tag"] == "v0.9.0"
        assert out["git_branch"] == ""
        assert out["git_dirty"] == "false"

    def test_output_does_not_depend_on_the_working_directory(self, repo, tmp_path):
        # The script resolves against its own location, so an apply run from
        # anywhere reports the infrastructure/ revision rather than the cwd's.
        scripts = repo / "infrastructure" / "scripts"
        assert self._run(scripts, tmp_path) == self._run(scripts, repo)

    def test_reports_a_dirty_worktree(self, repo, tmp_path):
        (repo / "f.txt").write_text("uncommitted")
        out = self._run(repo / "infrastructure" / "scripts", tmp_path)
        assert out["git_dirty"] == "true"


class TestFieldNamesAgree:
    """The writers and readers share these strings by convention only."""

    def test_dockerfile_writes_every_build_info_key(self):
        assert DOCKERFILE.exists(), f"Dockerfile not found at {DOCKERFILE}"
        line = next(
            line
            for line in DOCKERFILE.read_text().splitlines()
            if line.startswith("RUN python3") and "BUILD_INFO" in line
        )
        # Spelled out rather than generated, so a repo-wide grep for a field
        # name reaches the Dockerfile that writes it.
        for key in BUILD_INFO_KEYS:
            assert f"'{key}'" in line, f"{key} missing from the BUILD_INFO line"

    def test_version_py_reads_every_key_the_dockerfile_writes(self):
        source = (PROJECT_ROOT / "studio" / "app" / "version.py").read_text()
        for key in BUILD_INFO_KEYS:
            assert f'"{key}"' in source, f"{key} not read by version.py"

    def test_terraform_build_info_emits_the_tag_field(self):
        assert "git_tag" in TF_BUILD_INFO.read_text()

    def test_compute_tf_stamps_the_tag_onto_the_cluster(self):
        tf = COMPUTE_TF.read_text()
        assert "data.external.tf_build_info.result.git_tag" in tf
        # Empty values become "-" rather than an empty ECS tag.
        assert 'coalesce(data.external.tf_build_info.result.git_tag, "-")' in tf


class TestImagePathIsWired:
    """The build side of the fix, which no behavioural test can reach.

    Everything above runs the terraform path end to end, but the reported
    defect was on the image path: /app/BUILD_INFO recording git_branch "HEAD".
    That path only executes inside `docker build`, so reverting
    ecr_build_push.sh to `git rev-parse --abbrev-ref HEAD` and deleting the
    --build-arg left the whole suite green. These read the script instead.
    """

    def test_the_branch_comes_from_the_shared_resolver(self):
        script = ECR_BUILD_PUSH.read_text()
        assert 'git_ref_info.sh"' in script, "no longer sources the shared script"
        assert 'GIT_BRANCH="$GIT_INFO_BRANCH"' in script
        assert 'GIT_TAG="$GIT_INFO_TAG"' in script

    def test_no_script_resolves_a_branch_with_abbrev_ref(self):
        # The defect itself: --abbrev-ref reports the literal string "HEAD" for
        # the detached HEAD a tag checkout produces. Comments are stripped
        # first — all three scripts name the old command in prose, explaining
        # why it is not used.
        for script in (ECR_BUILD_PUSH, GIT_REF_INFO, TF_BUILD_INFO):
            code = "\n".join(
                line
                for line in script.read_text().splitlines()
                if not line.lstrip().startswith("#")
            )
            assert "--abbrev-ref" not in code, script.name

    def test_every_build_info_field_is_passed_into_the_build(self):
        # The ARG names are the BUILD_INFO keys upper-cased, which is what ties
        # the shell, the Dockerfile and version.py to one set of strings.
        script = ECR_BUILD_PUSH.read_text()
        dockerfile = DOCKERFILE.read_text()
        for key in BUILD_INFO_KEYS:
            arg = key.upper()
            assert f"--build-arg {arg}=" in script, f"{arg} not passed by the script"
            assert f"ARG {arg}" in dockerfile, f"{arg} not declared by the Dockerfile"
