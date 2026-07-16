import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.providers import (
    CONFIG_PATH_ENV_VAR,
    LEGACY_DEFAULT_CONFIG_NAME,
    OPERATOR_CONFIG_DIRNAME,
    ProviderConfigError,
    load_providers_config,
    provider_config_format,
    resolve_providers_config_source,
    validate_providers_document,
)

VALID_PROVIDERS_DOCUMENT = {
    "providers": {
        "local": {
            "kind": "openai-compatible",
            "base_url": "http://127.0.0.1:8765/v1",
            "api_key_env": "LOCAL_KEY",
            "default_model": "local-coder",
            "allow_insecure_http": True,
        }
    }
}


class ProviderConfigFileTests(unittest.TestCase):
    def test_load_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps(VALID_PROVIDERS_DOCUMENT) + "\n")
            providers = load_providers_config(path)
            self.assertEqual(providers["local"].default_model, "local-coder")

    def test_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json")
            with self.assertRaises(ProviderConfigError):
                load_providers_config(path)

    def test_json_content_sniffed_without_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text(json.dumps(VALID_PROVIDERS_DOCUMENT))
            self.assertEqual(provider_config_format(path), "json")
            providers = load_providers_config(path)
            self.assertEqual(providers["local"].default_model, "local-coder")


class YamlProviderConfigTests(unittest.TestCase):
    def test_load_from_yaml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "providers:\n"
                "  local:\n"
                "    kind: openai-compatible\n"
                "    base_url: http://127.0.0.1:8765/v1\n"
                "    api_key_env: LOCAL_KEY\n"
                "    default_model: local-coder\n"
                "    allow_insecure_http: true\n"
            )
            self.assertEqual(provider_config_format(path), "yaml")
            providers = load_providers_config(path)
            self.assertEqual(providers["local"].default_model, "local-coder")
            self.assertEqual(providers["local"].allow_insecure_http, True)

    def test_yaml_yml_extension_also_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text(
                "providers:\n"
                "  local:\n"
                "    kind: openai-compatible\n"
                "    base_url: https://example.test/v1\n"
                "    api_key_env: LOCAL_KEY\n"
                "    default_model: remote-coder\n"
            )
            providers = load_providers_config(path)
            self.assertEqual(providers["local"].default_model, "remote-coder")

    def test_invalid_yaml_syntax_raises_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("providers:\n  local: [unterminated\n")
            with self.assertRaises(ProviderConfigError) as ctx:
                load_providers_config(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_empty_yaml_document_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("\n")
            with self.assertRaises(ProviderConfigError):
                load_providers_config(path)

    def test_unsafe_yaml_tag_is_rejected_not_executed(self):
        """A safe loader must refuse arbitrary Python object construction/tags.

        !!python/object/apply is the classic PyYAML unsafe-load RCE vector
        (arbitrary callable invocation during construction). safe_load must
        raise rather than construct or call anything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "providers: !!python/object/apply:os.system [\"true\"]\n"
            )
            with self.assertRaises(ProviderConfigError) as ctx:
                load_providers_config(path)
            # Actionable: names the offending file, not just a bare traceback.
            self.assertIn(str(path), str(ctx.exception))

    def test_unsafe_yaml_arbitrary_tag_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("providers: !!python/name:builtins.eval\n")
            with self.assertRaises(ProviderConfigError):
                load_providers_config(path)

    def test_yaml_document_still_schema_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("providers: {}\n")
            with self.assertRaises(ProviderConfigError):
                load_providers_config(path)


class ValidateProvidersDocumentTests(unittest.TestCase):
    def test_validate_from_parsed_dict(self):
        providers = validate_providers_document(VALID_PROVIDERS_DOCUMENT)
        self.assertEqual(providers["local"].default_model, "local-coder")

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(ProviderConfigError) as ctx:
            validate_providers_document({**VALID_PROVIDERS_DOCUMENT, "_comment": "hi"})
        self.assertIn("_comment", str(ctx.exception))

    def test_unknown_provider_entry_key_rejected(self):
        doc = json.loads(json.dumps(VALID_PROVIDERS_DOCUMENT))
        doc["providers"]["local"]["notes"] = "extra"
        with self.assertRaises(ProviderConfigError) as ctx:
            validate_providers_document(doc)
        self.assertIn("notes", str(ctx.exception))

    def test_literal_secret_field_rejected_with_actionable_message(self):
        doc = json.loads(json.dumps(VALID_PROVIDERS_DOCUMENT))
        doc["providers"]["local"]["api_key"] = "sk-not-allowed-here"
        with self.assertRaises(ProviderConfigError) as ctx:
            validate_providers_document(doc)
        message = str(ctx.exception)
        self.assertIn("api_key_env", message)
        self.assertNotIn("sk-not-allowed-here", message)

    def test_ca_bundle_inline_certificate_content_rejected(self):
        doc = json.loads(json.dumps(VALID_PROVIDERS_DOCUMENT))
        doc["providers"]["local"]["ca_bundle"] = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----"
        with self.assertRaises(ProviderConfigError) as ctx:
            validate_providers_document(doc)
        self.assertIn("ca_bundle", str(ctx.exception))

    def test_ca_bundle_path_string_accepted(self):
        doc = json.loads(json.dumps(VALID_PROVIDERS_DOCUMENT))
        doc["providers"]["local"]["ca_bundle"] = "/etc/ssl/certs/ca-certificates.crt"
        providers = validate_providers_document(doc)
        self.assertEqual(providers["local"].ca_bundle, "/etc/ssl/certs/ca-certificates.crt")


class LoadProvidersConfigPathContextTests(unittest.TestCase):
    def test_schema_validation_error_includes_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("providers:\n  Bad-Name:\n    kind: openai-compatible\n")
            with self.assertRaises(ProviderConfigError) as ctx:
                load_providers_config(path)
            self.assertIn(str(path), str(ctx.exception))


class ResolveProvidersConfigSourceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.repo_root = self.tmp / "repo"
        self.user_home = self.tmp / "home"
        self.repo_root.mkdir()
        self.user_home.mkdir()

    def _resolve(self, *, explicit=None, environ=None):
        return resolve_providers_config_source(
            explicit=explicit,
            environ=environ or {},
            repo_root=self.repo_root,
            user_home=self.user_home,
        )

    def test_not_configured_when_nothing_present(self):
        resolved = self._resolve()
        self.assertIsNone(resolved.path)
        self.assertEqual(resolved.origin, "not_configured")

    def test_explicit_wins_over_everything(self):
        (self.repo_root / OPERATOR_CONFIG_DIRNAME).mkdir()
        (self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.yaml").write_text("providers: {}\n")
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        explicit_path = self.tmp / "explicit.json"
        resolved = self._resolve(
            explicit=explicit_path,
            environ={CONFIG_PATH_ENV_VAR: str(self.tmp / "env.json")},
        )
        self.assertEqual(resolved.path, explicit_path)
        self.assertEqual(resolved.origin, "explicit")

    def test_env_var_wins_over_repo_and_user_and_legacy(self):
        (self.repo_root / OPERATOR_CONFIG_DIRNAME).mkdir()
        (self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.yaml").write_text("providers: {}\n")
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        env_path = self.tmp / "env-config.yaml"
        resolved = self._resolve(environ={CONFIG_PATH_ENV_VAR: str(env_path)})
        self.assertEqual(resolved.path, env_path)
        self.assertEqual(resolved.origin, "env")

    def test_repo_local_wins_over_user_level_and_legacy(self):
        (self.repo_root / OPERATOR_CONFIG_DIRNAME).mkdir()
        repo_config = self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.yaml"
        repo_config.write_text("providers: {}\n")
        (self.user_home / OPERATOR_CONFIG_DIRNAME).mkdir()
        (self.user_home / OPERATOR_CONFIG_DIRNAME / "config.yaml").write_text("providers: {}\n")
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        resolved = self._resolve()
        self.assertEqual(resolved.path, repo_config)
        self.assertEqual(resolved.origin, "repo_local")

    def test_repo_local_checks_yaml_yml_json_in_order(self):
        (self.repo_root / OPERATOR_CONFIG_DIRNAME).mkdir()
        yml_config = self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.yml"
        yml_config.write_text("providers: {}\n")
        json_config = self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.json"
        json_config.write_text("{}")
        resolved = self._resolve()
        self.assertEqual(resolved.path, yml_config)
        self.assertEqual(resolved.origin, "repo_local")

    def test_user_level_wins_over_legacy_default(self):
        (self.user_home / OPERATOR_CONFIG_DIRNAME).mkdir()
        user_config = self.user_home / OPERATOR_CONFIG_DIRNAME / "config.yaml"
        user_config.write_text("providers: {}\n")
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        resolved = self._resolve()
        self.assertEqual(resolved.path, user_config)
        self.assertEqual(resolved.origin, "user_level")

    def test_legacy_default_discovery_as_last_resort(self):
        legacy = self.repo_root / LEGACY_DEFAULT_CONFIG_NAME
        legacy.write_text("{}")
        resolved = self._resolve()
        self.assertEqual(resolved.path, legacy)
        self.assertEqual(resolved.origin, "legacy_default")

    def test_missing_explicit_source_is_not_silently_replaced(self):
        """A configured (explicit) source that doesn't exist must fail truthfully,
        not fall through to a lower-precedence file that does exist."""
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        missing_explicit = self.tmp / "does-not-exist.yaml"
        resolved = self._resolve(explicit=missing_explicit)
        self.assertEqual(resolved.path, missing_explicit)
        self.assertEqual(resolved.origin, "explicit")
        with self.assertRaises(ProviderConfigError):
            load_providers_config(resolved.path)

    def test_missing_env_source_is_not_silently_replaced(self):
        (self.repo_root / LEGACY_DEFAULT_CONFIG_NAME).write_text("{}")
        missing_env_path = self.tmp / "also-does-not-exist.yaml"
        resolved = self._resolve(environ={CONFIG_PATH_ENV_VAR: str(missing_env_path)})
        self.assertEqual(resolved.path, missing_env_path)
        self.assertEqual(resolved.origin, "env")
        with self.assertRaises(ProviderConfigError):
            load_providers_config(resolved.path)

    def test_empty_env_var_value_is_treated_as_unset(self):
        (self.repo_root / OPERATOR_CONFIG_DIRNAME).mkdir()
        repo_config = self.repo_root / OPERATOR_CONFIG_DIRNAME / "config.yaml"
        repo_config.write_text("providers: {}\n")
        resolved = self._resolve(environ={CONFIG_PATH_ENV_VAR: ""})
        self.assertEqual(resolved.path, repo_config)
        self.assertEqual(resolved.origin, "repo_local")


if __name__ == "__main__":
    unittest.main()
