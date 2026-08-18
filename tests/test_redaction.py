from agent_workspace.redaction import redact_text, redact_value


def test_redacts_tokens_credentials_and_session_keys() -> None:
    source = (
        "authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        f"token={'ghp_' + 'A' * 26} "
        + ":".join(("agent", "sample", "dashboard", "opaque-value"))
    )
    rendered = redact_text(source)
    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "ghp_" not in rendered
    assert "opaque-value" not in rendered
    assert rendered.count("REDACTED") >= 3


def test_redacts_sensitive_mapping_keys_recursively() -> None:
    rendered = redact_value(
        {"safe": "visible", "nested": {"password": "do-not-show", "count": 2}}
    )
    assert rendered["safe"] == "visible"
    assert rendered["nested"]["password"] == "[REDACTED]"
    assert rendered["nested"]["count"] == 2


def test_redacts_url_userinfo_and_private_material() -> None:
    rendered = redact_text(
        "https://" + "user" + ":" + "pass" + "@example.invalid/ "
        + "-----BEGIN "
        + "PRIVATE KEY-----\nmaterial\n-----END "
        + "PRIVATE KEY-----"
    )
    assert "user:pass" not in rendered
    assert "material" not in rendered
