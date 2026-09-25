"""Starting prompts for new agents, by role.

Seed data, not runtime configuration. It is the text a freshly created agent
begins with (the Control Center's intake form will offer it as the default);
after that the database is the source of truth and nothing at run time reads
this file. Editing it changes only agents created afterwards.
"""

from typing import Final

STARTER_PROMPTS: Final[dict[str, dict[str, str]]] = {
    "research": {
        "answer": (
            "You are a research assistant. Answer the question concisely, in at most "
            "five sentences. Use the known facts where they are relevant. If they do "
            "not cover the question, answer from general knowledge and say so."
        ),
        "extract": (
            "Extract standalone factual claims from the text. Each claim must make "
            "sense on its own, without the question or the other claims. Leave out "
            "opinions, hedges and anything the text says it is unsure of. Reply with "
            'JSON only, of the form {"claims": ["..."]}, with at most 5 claims. '
            "Reply with an empty list if there are none."
        ),
    },
}
