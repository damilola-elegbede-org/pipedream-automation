"""Configuration loading and validation for Pipedream deployment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .exceptions import ConfigurationError, ValidationError


# Pattern to match ${VAR_NAME} or ${VAR_NAME:-default}
ENV_VAR_PATTERN = re.compile(r'\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}')


@dataclass
class StepConfig:
    """Configuration for a single workflow step."""

    step_name: str
    script_path: str
    description: str = ""

    def validate(self, base_path: Path) -> None:
        """Validate the step configuration."""
        if not self.step_name:
            raise ValidationError("Step name cannot be empty")

        script_file = base_path / self.script_path
        if not script_file.exists():
            raise ValidationError(f"Script not found: {self.script_path}")

        # Validate Python syntax
        try:
            with open(script_file) as f:
                code = f.read()
            compile(code, str(script_file), "exec")
        except SyntaxError as e:
            raise ValidationError(f"Syntax error in {self.script_path}: {e}")

        # Check for handler function
        if "def handler(" not in code:
            raise ValidationError(
                f"Script {self.script_path} must define a 'handler' function"
            )


@dataclass
class WorkflowConfig:
    """Configuration for a single Pipedream workflow."""

    id: str
    name: str
    steps: list[StepConfig]

    def validate(self, base_path: Path) -> None:
        """Validate the workflow configuration."""
        if not self.id:
            raise ValidationError(f"Workflow '{self.name}' is missing an ID")

        # Workflow ID can be either just "p_xxx" or full slug "name-p_xxx"
        if "p_" not in self.id:
            raise ValidationError(
                f"Workflow ID '{self.id}' should contain 'p_' (e.g., 'p_abc123' or 'my-workflow-p_abc123')"
            )

        if not self.steps:
            raise ValidationError(f"Workflow '{self.name}' has no steps defined")

        for step in self.steps:
            step.validate(base_path)


@dataclass
class DeploySettings:
    """Deployment behavior settings."""

    step_timeout: int = 60
    max_retries: int = 3
    retry_delay_seconds: float = 5.0
    autosave_wait: float = 3.0
    headless: bool = True
    screenshot_on_failure: bool = True
    screenshot_path: str = ".tmp/screenshots"
    viewport_width: int = 1920
    viewport_height: int = 1080


@dataclass
class DeployConfig:
    """Complete deployment configuration."""

    version: str
    pipedream_base_url: str
    workflows: dict[str, WorkflowConfig]
    settings: DeploySettings = field(default_factory=DeploySettings)
    pipedream_username: str = ""
    pipedream_project_id: str = ""

    def get_workflow(self, key: str) -> WorkflowConfig:
        """Get a workflow by its key."""
        if key not in self.workflows:
            available = ", ".join(self.workflows.keys())
            raise ConfigurationError(
                f"Workflow '{key}' not found. Available: {available}"
            )
        return self.workflows[key]

    def validate(self, base_path: Path) -> None:
        """Validate the entire configuration."""
        if not self.workflows:
            raise ValidationError("No workflows defined in configuration")

        for key, workflow in self.workflows.items():
            try:
                workflow.validate(base_path)
            except ValidationError as e:
                raise ValidationError(f"Workflow '{key}': {e}")


def _substitute_env_vars(value: Any) -> Any:
    """
    Recursively substitute environment variables in configuration values.

    Supports patterns:
        ${VAR_NAME} - Required variable, raises error if not set
        ${VAR_NAME:-default} - Optional variable with default value

    Args:
        value: The value to process (string, dict, list, or other)

    Returns:
        Value with environment variables substituted
    """
    if isinstance(value, str):
        def replace_match(match: re.Match) -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)

            if env_value is not None:
                return env_value
            elif default is not None:
                return default
            else:
                raise ConfigurationError(
                    f"Environment variable '{var_name}' is not set and has no default. "
                    f"Set it with: export {var_name}=<value>"
                )

        return ENV_VAR_PATTERN.sub(replace_match, value)

    elif isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]

    else:
        return value


def load_config(config_path: str) -> DeployConfig:
    """
    Load and parse the deployment configuration file.

    Environment variables in the format ${VAR_NAME} or ${VAR_NAME:-default}
    are substituted during loading.

    Args:
        config_path: Path to the YAML configuration file

    Returns:
        Parsed DeployConfig object
    """
    config_file = Path(config_path)
    if not config_file.exists():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    try:
        with open(config_file) as f:
            raw_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {e}")

    if not raw_data:
        raise ConfigurationError(f"Empty configuration file: {config_path}")

    # Substitute environment variables
    data = _substitute_env_vars(raw_data)

    # Parse workflows
    workflows = {}
    for key, wf_data in data.get("workflows", {}).items():
        steps = [
            StepConfig(
                step_name=s.get("step_name", ""),
                script_path=s.get("script_path", ""),
                description=s.get("description", ""),
            )
            for s in wf_data.get("steps", [])
        ]
        workflows[key] = WorkflowConfig(
            id=wf_data.get("id", ""),
            name=wf_data.get("name", key),
            steps=steps,
        )

    # Parse settings
    settings_data = data.get("settings", {})
    viewport = settings_data.get("viewport", {})
    settings = DeploySettings(
        step_timeout=settings_data.get("step_timeout", 60),
        max_retries=settings_data.get("max_retries", 3),
        retry_delay_seconds=settings_data.get("retry_delay_seconds", 5.0),
        autosave_wait=settings_data.get("autosave_wait", 3.0),
        headless=settings_data.get("headless", True),
        screenshot_on_failure=settings_data.get("screenshot_on_failure", True),
        screenshot_path=settings_data.get("screenshot_path", ".tmp/screenshots"),
        viewport_width=viewport.get("width", 1920),
        viewport_height=viewport.get("height", 1080),
    )

    return DeployConfig(
        version=data.get("version", "1.0"),
        pipedream_base_url=data.get("pipedream_base_url", "https://pipedream.com"),
        workflows=workflows,
        settings=settings,
        pipedream_username=data.get("pipedream_username", ""),
        pipedream_project_id=data.get("pipedream_project_id", ""),
    )


def validate_config(config: DeployConfig, base_path: Optional[str] = None) -> bool:
    """
    Validate the configuration and all referenced scripts.

    Args:
        config: The configuration to validate
        base_path: Base path for resolving relative paths (defaults to cwd)

    Returns:
        True if valid

    Raises:
        ValidationError: If validation fails
    """
    path = Path(base_path) if base_path else Path.cwd()
    config.validate(path)
    return True


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Validate Pipedream config")
    parser.add_argument("--validate", required=True, help="Config file to validate")
    args = parser.parse_args()

    try:
        config = load_config(args.validate)
        validate_config(config)
        print("Configuration is valid!")
        print(f"  Version: {config.version}")
        print(f"  Workflows: {len(config.workflows)}")
        for key, wf in config.workflows.items():
            print(f"    - {key}: {len(wf.steps)} steps")
        sys.exit(0)
    except (ConfigurationError, ValidationError) as e:
        print(f"Validation failed: {e}", file=sys.stderr)
        sys.exit(1)
