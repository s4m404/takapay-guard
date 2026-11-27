"""takapay-guard: check where a tool-using agent's sensitive arguments came from.

Before an agent runs a sensitive tool call (send money, send email, delete a
file), the guard checks each important argument: a payee, amount, address or
code that appears only in untrusted text (a tool result, a web page, a
retrieved document) and never in the user's own request is blocked, or the
user is asked. No model reads the text, so it works the same in Bangla,
Banglish and English.
"""

from .engine import Decision, Guard
from .wrapper import GuardedTools, run_openai_loop
from .policy import Policy, PolicyError, builtin_policies, load_builtin, load_policy

__version__ = "0.1.0.dev0"

__all__ = ["Decision", "Guard", "GuardedTools", "run_openai_loop", "Policy", "PolicyError", "builtin_policies", "load_builtin", "load_policy", "__version__"]
