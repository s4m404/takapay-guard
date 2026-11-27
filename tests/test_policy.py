from pathlib import Path

import pytest

from takapay_guard import PolicyError, builtin_policies, load_builtin, load_policy

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

MINIMAL = """
version: 1
name: t
tools:
  send_money:
    arguments:
      to: {type: phone, trusted_from: [user_request]}
"""


def test_builtin_policies_load():
    assert builtin_policies() == ["takapay-provenance", "takapay-provenance-amount", "takapay-provenance-consistent"]
    for name in builtin_policies():
        p = load_builtin(name)
        assert p.name == name
        assert p.sensitive_tools == {"send_money", "pay_bill", "send_sms"}
    assert load_builtin("takapay-provenance").sources == ()
    assert load_builtin("takapay-provenance-consistent").consistency_ignore_above == 100_000_000


def test_example_policies_load():
    for path in EXAMPLES.glob("*.yaml"):
        load_policy(path)


def test_load_from_text_and_file(tmp_path):
    p = load_policy(MINIMAL)
    assert p.tools["send_money"].arguments[0].action == "block"  # the default
    f = tmp_path / "p.yaml"
    f.write_text(MINIMAL, encoding="utf-8")
    assert load_policy(f) == p
    assert load_policy(str(f)) == p


def test_action_inherits_from_policy_then_tool():
    p = load_policy(
        MINIMAL.replace("name: t", "name: t\ndefault_action: ask").replace(
            "    arguments:", "    action: allow\n    arguments:"
        )
    )
    assert p.tools["send_money"].arguments[0].action == "allow"
    assert load_policy(MINIMAL.replace("name: t", "name: t\ndefault_action: ask")).tools["send_money"].arguments[0].action == "ask"


@pytest.mark.parametrize(
    "change, error",
    [
        (("version: 1", "version: 2"), "version must be 1"),
        (("name: t", "nme: t"), "unknown key"),
        (("type: phone", "type: iban"), "type: must be one of"),
        (("trusted_from: [user_request]", "trusted_from: [web]"), "unknown source"),
        (("trusted_from: [user_request]", "trusted_from: ['list:']"), "unknown source"),
        (("trusted_from: [user_request]", "trusted_fro: [user_request]"), "unknown key"),
        (("trusted_from: [user_request]", "trusted_from: []"), "nothing is checked"),
        (("trusted_from: [user_request]", "trusted_from: [user_request], action: deny"), "action must be one of"),
        (("trusted_from: [user_request]", "no_untrusted_codes: true"), "needs a top-level 'codes'"),
        (("trusted_from: [user_request]", "trusted_from: [payee_sources]"), "only to amount"),
        (("type: phone", "type: text"), "only no_untrusted_codes"),
        (("trusted_from: [user_request]", "trusted_from: [user_request], consistent: yes please"), "true or false"),
    ],
)
def test_bad_policies_fail_loudly(change, error):
    with pytest.raises(PolicyError, match=error):
        load_policy(MINIMAL.replace(*change))


def test_amount_rules_need_payee_and_sources():
    amount = MINIMAL + "      amount: {type: amount, trusted_from: [payee_sources]}\n"
    with pytest.raises(PolicyError, match="need 'payee'"):
        load_policy(amount)
    with pytest.raises(PolicyError, match="not an argument"):
        load_policy(amount.replace("payee_sources]", "payee_sources], payee: recipient"))
    with pytest.raises(PolicyError, match="'sources'"):
        load_policy(amount.replace("payee_sources]", "payee_sources], payee: to"))
    ok = amount.replace("payee_sources]", "payee_sources], payee: to") + "sources:\n  - {tools: [read_sms], payee_field: from, amount_field: text}\n"
    assert load_policy(ok).sources[0].tools == ("read_sms",)
    with pytest.raises(PolicyError, match="go together"):
        load_policy(ok.replace("payee_field: from", "payee_name_field: biller"))


def test_bad_yaml_and_regex():
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy("version: 1\nname: [unclosed\n")
    with pytest.raises(PolicyError, match="regular expression"):
        load_policy(MINIMAL + "codes: {pattern: '(', context: otp}\n")
    with pytest.raises(PolicyError, match="no built-in policy"):
        load_builtin("nope")
