"""Tests for deployment configuration loading and validation."""

import pytest
from pathlib import Path  # noqa: F401

from src.deploy.config import (  # noqa: F401
    DeployConfig,
    DeploySettings,
    StepConfig,
    WorkflowConfig,
    load_config,
    validate_config,
)
from src.deploy.exceptions import ConfigurationError, ValidationError


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_valid_config(self, tmp_path):
        """Test loading a valid configuration file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
pipedream_base_url: "https://pipedream.com"
workflows:
  test_workflow:
    id: "p_test123"
    name: "Test Workflow"
    steps:
      - step_name: "test_step"
        script_path: "src/steps/test.py"
settings:
  headless: true
""")

        config = load_config(str(config_file))
        assert config.version == "1.0"
        assert len(config.workflows) == 1
        assert "test_workflow" in config.workflows

    def test_load_missing_file(self):
        """Test loading a non-existent file raises error."""
        with pytest.raises(ConfigurationError, match="not found"):
            load_config("/nonexistent/path.yaml")

    def test_load_invalid_yaml(self, tmp_path):
        """Test loading invalid YAML raises error."""
        config_file = tmp_path / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [")

        with pytest.raises(ConfigurationError, match="Invalid YAML"):
            load_config(str(config_file))


class TestStepConfig:
    """Tests for StepConfig validation."""

    def test_valid_step(self, tmp_path):
        """Test validation of a valid step configuration."""
        # Create a valid script file
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        script_file = script_dir / "test.py"
        script_file.write_text('def handler(pd): pass')

        step = StepConfig(
            step_name="test_step",
            script_path="src/steps/test.py",
        )
        step.validate(tmp_path)  # Should not raise

    def test_missing_script(self, tmp_path):
        """Test validation fails for missing script."""
        step = StepConfig(
            step_name="test_step",
            script_path="src/steps/missing.py",
        )
        with pytest.raises(ValidationError, match="not found"):
            step.validate(tmp_path)

    def test_invalid_python_syntax(self, tmp_path):
        """Test validation fails for invalid Python syntax."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        script_file = script_dir / "bad.py"
        script_file.write_text('def handler( invalid syntax')

        step = StepConfig(
            step_name="test_step",
            script_path="src/steps/bad.py",
        )
        with pytest.raises(ValidationError, match="Syntax error"):
            step.validate(tmp_path)

    def test_missing_handler_function(self, tmp_path):
        """Test validation fails when handler function is missing."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        script_file = script_dir / "no_handler.py"
        script_file.write_text('def main(): pass')

        step = StepConfig(
            step_name="test_step",
            script_path="src/steps/no_handler.py",
        )
        with pytest.raises(ValidationError, match="handler"):
            step.validate(tmp_path)


class TestWorkflowConfig:
    """Tests for WorkflowConfig validation."""

    def test_valid_workflow(self, tmp_path):
        """Test validation of a valid workflow configuration."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        (script_dir / "test.py").write_text('def handler(pd): pass')

        workflow = WorkflowConfig(
            id="p_test123",
            name="Test Workflow",
            steps=[
                StepConfig(step_name="test", script_path="src/steps/test.py")
            ],
        )
        workflow.validate(tmp_path)  # Should not raise

    def test_missing_workflow_id(self, tmp_path):
        """Test validation fails for missing workflow ID."""
        workflow = WorkflowConfig(
            id="",
            name="Test Workflow",
            steps=[],
        )
        with pytest.raises(ValidationError, match="missing an ID"):
            workflow.validate(tmp_path)

    def test_invalid_workflow_id_format(self, tmp_path):
        """Test validation fails for invalid workflow ID format."""
        workflow = WorkflowConfig(
            id="invalid_id",
            name="Test Workflow",
            steps=[StepConfig(step_name="test", script_path="test.py")],
        )
        with pytest.raises(ValidationError, match="should contain 'p_'"):
            workflow.validate(tmp_path)


class TestDeployConfig:
    """Tests for DeployConfig."""

    def test_get_workflow(self):
        """Test getting a workflow by key."""
        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test": WorkflowConfig(
                    id="p_test",
                    name="Test",
                    steps=[],
                )
            },
        )
        workflow = config.get_workflow("test")
        assert workflow.id == "p_test"

    def test_get_missing_workflow(self):
        """Test getting a non-existent workflow raises error."""
        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
        )
        with pytest.raises(ConfigurationError, match="not found"):
            config.get_workflow("missing")


class TestStepConfigEdgeCases:
    """Additional edge case tests for StepConfig."""

    def test_empty_step_name(self, tmp_path):
        """Test validation fails for empty step name."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        (script_dir / "test.py").write_text('def handler(pd): pass')

        step = StepConfig(
            step_name="",  # Empty step name
            script_path="src/steps/test.py",
        )
        with pytest.raises(ValidationError, match="cannot be empty"):
            step.validate(tmp_path)


class TestWorkflowConfigEdgeCases:
    """Additional edge case tests for WorkflowConfig."""

    def test_workflow_no_steps(self, tmp_path):
        """Test validation fails when workflow has no steps."""
        workflow = WorkflowConfig(
            id="p_test123",
            name="Empty Workflow",
            steps=[],  # No steps
        )
        with pytest.raises(ValidationError, match="no steps defined"):
            workflow.validate(tmp_path)


class TestDeployConfigValidation:
    """Tests for DeployConfig.validate method."""

    def test_validate_no_workflows(self, tmp_path):
        """Test validation fails when no workflows defined."""
        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
        )
        with pytest.raises(ValidationError, match="No workflows defined"):
            config.validate(tmp_path)

    def test_validate_workflow_error_propagates(self, tmp_path):
        """Test validation error from workflow includes workflow key."""
        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "bad_workflow": WorkflowConfig(
                    id="",  # Invalid - missing ID
                    name="Bad",
                    steps=[],
                )
            },
        )
        with pytest.raises(ValidationError, match="bad_workflow"):
            config.validate(tmp_path)

    def test_validate_success(self, tmp_path):
        """Test successful validation."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        (script_dir / "test.py").write_text('def handler(pd): pass')

        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test_workflow": WorkflowConfig(
                    id="p_test123",
                    name="Test",
                    steps=[
                        StepConfig(step_name="test", script_path="src/steps/test.py")
                    ],
                )
            },
        )
        config.validate(tmp_path)  # Should not raise


class TestLoadConfigEdgeCases:
    """Additional edge case tests for load_config."""

    def test_load_empty_config(self, tmp_path):
        """Test loading an empty configuration file raises error."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        with pytest.raises(ConfigurationError, match="Empty configuration"):
            load_config(str(config_file))

    def test_load_config_with_settings(self, tmp_path):
        """Test loading config with custom settings."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "2.0"
pipedream_base_url: "https://custom.pipedream.com"
pipedream_username: "testuser"
pipedream_project_id: "proj_123"
workflows:
  my_workflow:
    id: "my-workflow-p_abc123"
    name: "My Workflow"
    steps:
      - step_name: "step1"
        script_path: "src/step1.py"
        description: "First step"
settings:
  step_timeout: 120
  max_retries: 5
  retry_delay_seconds: 10.0
  autosave_wait: 5.0
  headless: false
  screenshot_on_failure: false
  screenshot_path: "/custom/path"
  viewport:
    width: 1280
    height: 720
""")

        config = load_config(str(config_file))
        assert config.version == "2.0"
        assert config.pipedream_base_url == "https://custom.pipedream.com"
        assert config.pipedream_username == "testuser"
        assert config.pipedream_project_id == "proj_123"
        assert config.settings.step_timeout == 120
        assert config.settings.max_retries == 5
        assert config.settings.retry_delay_seconds == 10.0
        assert config.settings.autosave_wait == 5.0
        assert config.settings.headless is False
        assert config.settings.screenshot_on_failure is False
        assert config.settings.screenshot_path == "/custom/path"
        assert config.settings.viewport_width == 1280
        assert config.settings.viewport_height == 720


class TestEnvironmentVariableSubstitution:
    """Tests for environment variable substitution in config."""

    def test_env_var_with_value_set(self, tmp_path, monkeypatch):
        """Test environment variable substitution when var is set."""
        monkeypatch.setenv("TEST_WORKFLOW_ID", "p_env123")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
workflows:
  test:
    id: "${TEST_WORKFLOW_ID}"
    name: "Test"
    steps: []
""")

        config = load_config(str(config_file))
        assert config.workflows["test"].id == "p_env123"

    def test_env_var_with_default(self, tmp_path, monkeypatch):
        """Test environment variable substitution with default value."""
        # Make sure the var is NOT set
        monkeypatch.delenv("UNSET_VAR", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
workflows:
  test:
    id: "${UNSET_VAR:-p_default123}"
    name: "Test"
    steps: []
""")

        config = load_config(str(config_file))
        assert config.workflows["test"].id == "p_default123"

    def test_env_var_not_set_no_default(self, tmp_path, monkeypatch):
        """Test error when env var not set and no default provided."""
        monkeypatch.delenv("REQUIRED_VAR", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
workflows:
  test:
    id: "${REQUIRED_VAR}"
    name: "Test"
    steps: []
""")

        with pytest.raises(ConfigurationError, match="REQUIRED_VAR.*not set"):
            load_config(str(config_file))

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        """Test that set env var overrides default value."""
        monkeypatch.setenv("OVERRIDE_VAR", "p_overridden")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
version: "1.0"
workflows:
  test:
    id: "${OVERRIDE_VAR:-p_default}"
    name: "Test"
    steps: []
""")

        config = load_config(str(config_file))
        assert config.workflows["test"].id == "p_overridden"


class TestValidateConfigFunction:
    """Tests for the validate_config standalone function."""

    def test_validate_config_success(self, tmp_path):
        """Test validate_config returns True on success."""
        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        (script_dir / "test.py").write_text('def handler(pd): pass')

        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test": WorkflowConfig(
                    id="p_test",
                    name="Test",
                    steps=[
                        StepConfig(step_name="test", script_path="src/steps/test.py")
                    ],
                )
            },
        )

        result = validate_config(config, str(tmp_path))
        assert result is True

    def test_validate_config_with_default_path(self, tmp_path, monkeypatch):
        """Test validate_config uses cwd when no base_path provided."""
        # Change to tmp_path
        monkeypatch.chdir(tmp_path)

        script_dir = tmp_path / "src" / "steps"
        script_dir.mkdir(parents=True)
        (script_dir / "test.py").write_text('def handler(pd): pass')

        config = DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test": WorkflowConfig(
                    id="p_test",
                    name="Test",
                    steps=[
                        StepConfig(step_name="test", script_path="src/steps/test.py")
                    ],
                )
            },
        )

        result = validate_config(config)  # No base_path
        assert result is True
