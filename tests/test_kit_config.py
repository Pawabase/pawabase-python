"""Where the CLI finds its settings, in what order, and what it refuses."""

import pytest

from pawabase import config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_FILE", tmp_path / "home" / "config.json")


def load(tmp_path, **kw):
    return config.load(cwd=tmp_path, env=kw.pop("env", {}), **kw)


def test_defaults_when_nothing_is_set(tmp_path):
    settings = load(tmp_path)
    assert (settings.url, settings.api_key) == (None, None)
    assert (settings.environment, settings.branch, settings.functions) == ("development", "main", "functions")


def test_each_source_overrides_the_one_before(tmp_path):
    config.write_global({"url": "https://global.test", "api_key": "pb_sk_global_key_000000"})
    (tmp_path / "pawabase.toml").write_text('url = "https://file.test"\nenvironment = "staging"\n')
    assert (load(tmp_path).url, load(tmp_path).environment) == ("https://file.test", "staging")
    assert load(tmp_path).api_key == "pb_sk_global_key_000000"
    (tmp_path / ".env").write_text("PAWABASE_API_KEY=pb_sk_dotenv_key_000000\nPAWABASE_ENVIRONMENT=qa\n")
    assert (load(tmp_path).api_key, load(tmp_path).environment) == ("pb_sk_dotenv_key_000000", "qa")
    envs = {"PAWABASE_ENVIRONMENT": "from-env", "PAWABASE_API_KEY": "pb_sk_env_key_0000000000"}
    assert (load(tmp_path, env=envs).environment, load(tmp_path, env=envs).api_key) == ("from-env", "pb_sk_env_key_0000000000")
    flagged = load(tmp_path, env=envs, environment="prod")
    assert (flagged.environment, flagged.sources["environment"]) == ("prod", "flag")


def test_the_project_file_is_found_from_a_subfolder(tmp_path):
    (tmp_path / "pawabase.toml").write_text('environment = "up-here"\nfunctions = "src/fn"\n')
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    settings = config.load(cwd=deep, env={})
    assert settings.environment == "up-here" and settings.root == tmp_path.resolve() and settings.functions_dir == (tmp_path / "src/fn").resolve()


def test_a_key_in_the_project_file_is_refused_not_used(tmp_path):
    (tmp_path / "pawabase.toml").write_text('environment = "p"\napi_key = "pb_sk_oops"\n')
    with pytest.raises(config.ConfigError, match=r"\.env"):
        load(tmp_path)


def test_the_key_never_appears_in_a_repr_or_in_the_project_file_that_is_written(tmp_path):
    settings = load(tmp_path, api_key="pb_sk_very_secret_value_123")
    assert "very_secret" not in repr(settings) and "very_secret" not in str(settings.describe())
    path = config.write_project_file(tmp_path, {"api_key": "pb_sk_very_secret_value_123", "environment": "development"})
    assert "pb_sk" not in path.read_text() and 'environment = "development"' in path.read_text()


def test_missing_settings_say_how_to_supply_each(tmp_path):
    with pytest.raises(config.ConfigError) as raised:
        load(tmp_path).require("url", "api_key")
    text = str(raised.value)
    assert "PAWABASE_URL" in text and "PAWABASE_API_KEY" in text and "pawabase login" in text


def test_dotenv_parsing_is_forgiving_and_never_executes_anything():
    parsed = config.parse_dotenv('# a comment\nexport A=1\nB="two words"\nC=\'single\'\nD=plain # trailing comment\nE=\n bad line\n$(rm -rf /)=1\nF=a=b\n')
    assert parsed == {"A": "1", "B": "two words", "C": "single", "D": "plain", "E": "", "F": "a=b"}


def test_branch_names_are_checked_and_git_branches_are_made_valid(tmp_path):
    assert config.slugify_branch("Feature/Login Page") == "feature-login-page"
    assert config.slugify_branch("123-fix") == "b-123-fix"
    assert config.slugify_branch("///") == "main"
    with pytest.raises(config.ConfigError):
        load(tmp_path, branch="Bad Branch!")
    with pytest.raises(config.ConfigError):
        load(tmp_path, url="ftp://nope")


def test_the_git_branch_is_used_only_when_asked(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "Feature/X"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "first"], cwd=tmp_path, check=True)
    assert load(tmp_path).branch == "main"
    assert load(tmp_path, use_git_branch=True).branch == "feature-x"
    assert load(tmp_path, use_git_branch=True, branch="chosen").branch == "chosen"
    info = config.git_commit(tmp_path)
    assert len(info["commit"]) == 40 and info["dirty"] is False and info["message"] == "first"
