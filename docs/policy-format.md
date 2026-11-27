# Policy format (version 1)

A policy is a YAML file. It says which tools are sensitive, which of their
arguments must be trusted, where a trusted value may come from, and what to
do when a check fails. Loading a policy checks every key: a typo is an
error, never a silently missing check.

The three TakaPay policies in `src/takapay_guard/policies/` are full
examples; `examples/email-agent.yaml` is a small one from another domain.

## Top level

| key | required | meaning |
|---|---|---|
| `version` | yes | always `1` |
| `name` | yes | a short name |
| `description` | no | free text |
| `default_action` | no | `block` (default), `ask` or `allow` |
| `untrusted_tools` | no | tools whose results contain third-party text (SMS, invoices, web pages, documents). Only their results are recorded as untrusted. |
| `codes` | no | how to find secret codes in untrusted text (needed by `no_untrusted_codes`) |
| `sources` | no | where a payee's own amounts are found (needed by `payee_sources` and `consistent`) |
| `consistency` | no | settings of the consistency check |
| `tools` | yes | the sensitive tools and their argument rules |

A tool that is not listed under `tools` is always allowed.

## Actions

- `block`: do not run the call. The decision's `reason` (and `advice`, if
  set) can be shown to the agent.
- `ask`: do not run the call until the user confirms it.
- `allow`: run the call, but the decision still names the failed check
  (`rule`, `argument`): monitor mode, for trying a policy out.

The action is taken from the argument, else its tool, else `default_action`.

## Arguments

```yaml
tools:
  send_money:
    action: block            # optional
    arguments:               # checked in this order; the first failure decides
      to:
        type: phone
        trusted_from: [user_request, "list:contacts"]
        message: "recipient {value} is not a saved contact and was not named by the user"
      amount:
        type: amount
        trusted_from: [user_request, payee_sources]
        payee: to
        consistent: true
```

| key | meaning |
|---|---|
| `type` | `phone` (Bangladeshi numbers; Bangla digits, spaces, dashes and +88 are folded), `account` (an account ID, compared after removing spaces and dashes), `email` (lower case), `amount` (a number, rounded to 2 decimals), `text` (free text, only for `no_untrusted_codes`) |
| `trusted_from` | where a trusted value may come from (any one is enough): `user_request` (the value appears in the user's own words), `list:<name>` (a trusted list the host passes to `Guard.start`, such as saved contacts or registered billers), `payee_sources` (amounts only: the payee itself gave this amount, see `sources`) |
| `payee` | for `payee_sources` and `consistent`: the argument that names the payee |
| `consistent` | amounts only: block when the payee's own messages give different amounts (see below) |
| `no_untrusted_codes` | the value must not contain a code found in untrusted text (an OTP leak) |
| `action` | overrides the tool's action |
| `message` | the reason when the check fails. Fields: `{argument}`, `{value}`, `{payee}`; `{value:g}` for amounts |
| `advice` | what the agent should do next; empty means the host's default |
| `invalid_message` | amounts only: the reason when the value is not a number |
| `consistent_message`, `consistent_advice` | the same for the consistency check; `{others}` lists up to three other amounts |

A value of the user's request is trusted only if it appears there. A value
that appears nowhere (an invented number) fails the same way as one that
appears only in untrusted text; the decision's `seen_in` tells them apart
(the untrusted tools whose results contained the value, or empty).

## Codes

```yaml
codes:
  pattern: '\b\d{4,8}\b'          # what a code looks like
  context: 'otp|\bcode\b|\bpin\b|password|ওটিপি|কোড|পিন|পাসওয়ার্ড'
```

For each item of an untrusted result (each SMS, each invoice), if its
normalised text (lower case, ASCII digits) matches `context`, every match
of `pattern` is a code. An argument with `no_untrusted_codes` fails when it
contains a code, also when the code is written with spaces, dashes or
Bangla digits ("482 913", "৪৮২-৯১৩").

## Sources (payee amounts)

```yaml
sources:
  - tools: [read_sms]
    payee_field: from              # the payee is the SMS sender
    amount_field: text             # every number in the text is a possible amount
    id_fields: [id, text]          # what identifies one SMS (for the consistency check)
  - tools: [list_invoices, read_invoice]
    payee_field: account
    payee_name_field: biller       # if there is no account, look the biller's name up
    payee_name_list: billers       # in this trusted list (name -> account)
    amount_field: amount
```

Each JSON object in a result of these tools adds its amounts to its payee.
Dates ("10 October", "১০ তারিখ") are not amounts. Any number in the field
counts, so the rule cannot tell a total from a share in the same message.

## Consistency

With `consistent: true`, a payment is blocked when the payee's own messages
or invoices disagree: one gives the amount being paid, another gives other
amounts and not this one. An amount the user gave is always accepted (when
`user_request` is in `trusted_from`). The host can pass items the agent has
not read with `Guard.start(background={"read_sms": inbox, ...})`, so a
forged newest message cannot hide the real one.

```yaml
consistency:
  ignore_above: 100000000   # larger numbers are phone or account numbers, not amounts
```
