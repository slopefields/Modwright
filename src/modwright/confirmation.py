"""Putting a question to the person, rather than trusting the agent to have asked.

Every argument a tool receives comes from an agent, and an agent that has seen
one Lethal Company project in a directory will supply "Lethal Company" for the
next one without anybody having said so. That is not hypothetical: it is what
happened, and it is what this module exists to stop. Hints in a response cannot
prevent it, because a hint is advice and advice is skippable.

MCP elicitation is the one channel that is not skippable in the same way. It
suspends the tool call and puts a structured question to the client, which is
expected to put it to the person -- so the answer arrives from outside the
agent's own context.

Two limits, both worth stating plainly rather than discovering later:

- A client is not obliged to implement elicitation. When one has not declared
  the capability there is no way to ask, and the caller proceeds unconfirmed
  and says so, rather than implying an approval that never happened.
- Where it is implemented, the spec still permits an agent client to answer on
  the user's behalf. This raises the floor from "the agent may skip asking" to
  "the client must deliberately answer"; it is not proof that a human typed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, create_model

from modwright.errors import NotConfirmedError

#: Offered alongside the discovered profiles, and deliberately the default.
#: Choosing it leaves `deploy_root` unset, which is the state `deploy_mod`
#: already refuses to guess past -- so asking early never becomes a way of
#: answering early on the user's behalf.
DECIDE_LATER = "decide later"

#: The other non-profile answer: the loader inside the game folder.
GAME_FOLDER = "the game folder itself"


@dataclass
class Confirmation:
    """What the person answered, once the client has been asked."""

    mod_name: str
    #: The loader tree they chose, or None for "decide later" -- which leaves
    #: the project with no target and the refusal in `deploy_mod` intact.
    deploy_root: str | None


def can_ask(ctx: Any) -> bool:
    """Whether this client can be asked anything at all.

    False for a client that never declared the elicitation capability, and for
    a direct call with no request behind it -- a test, or another tool reusing
    the function. Neither is a failure; both mean the question goes unasked.
    """
    if ctx is None:
        return False
    try:
        capabilities = ctx.client_capabilities
    except ValueError:
        return False  # No active request, so no client to ask.
    if not capabilities or not capabilities.elicitation:
        return False

    # Form and URL elicitation are separate capabilities, and this question is
    # a form. A client that declared only the URL kind would fail the call
    # rather than answer it. Declaring neither is the older "supported" shape
    # and is taken at its word.
    elicitation = capabilities.elicitation
    return not (elicitation.url and not elicitation.form)


async def confirm_new_project(
    ctx: Any,
    *,
    game_name: str,
    install_root: Path,
    project_path: Path,
    mod_name: str,
    profiles: list[Any],
) -> Confirmation | None:
    """Ask the person to confirm what a new project is being created against.

    Returns None when the client cannot be asked, so the caller can report
    that it proceeded on the agent's word alone. Raises rather than returning
    a "no" -- a project the user did not agree to must not be written, and the
    agent's next move (go and ask, then call again) is the same either way.
    """
    if not can_ask(ctx):
        return None

    choices = _deploy_choices(profiles, install_root)
    result = await ctx.elicit(
        message=(
            f"Create the mod project '{mod_name}' at {project_path}?\n"
            f"Game detected at {install_root}: {game_name}."
        ),
        schema=_schema(game_name, mod_name, choices),
    )

    if result.action != "accept":
        raise NotConfirmedError(
            f"Creating '{mod_name}' for {game_name} was not confirmed, so "
            "nothing was written.",
            hints=[
                "Ask the user which game this mod is for and what it should "
                "be called, then call scaffold_mod_project again with their "
                "answer.",
            ],
        )

    answer = result.data
    if not answer.game_is_correct:
        raise NotConfirmedError(
            f"The user says {game_name} is not the game this mod is for, so "
            "nothing was written.",
            hints=[
                "Ask which game it is and where it is installed, then pass "
                "that path as install_root. The game is detected from the "
                "install, so a different game means a different path.",
                "Do not infer the game from other projects on this machine.",
            ],
        )

    return Confirmation(
        mod_name=(answer.mod_name or "").strip() or mod_name,
        deploy_root=choices.get(getattr(answer, "deploy_target", DECIDE_LATER)),
    )


def _deploy_choices(profiles: list[Any], install_root: Path) -> dict[str, str | None]:
    """Label every place this mod could be installed, in the user's terms.

    Labels carry the manager and the mod count because a profile name on its
    own ("dev", "test") does not distinguish two managers' profiles from each
    other, and the count is the part that says which one is the quiet profile
    worth testing in.
    """
    choices: dict[str, str | None] = {}
    for profile in profiles:
        label = f"{profile.name} ({profile.manager}, {profile.mod_count} mods)"
        if label in choices:
            label = f"{label} at {profile.path}"
        choices[label] = str(profile.path)
    choices[GAME_FOLDER] = str(install_root)
    choices[DECIDE_LATER] = None
    return choices


def _schema(game_name: str, mod_name: str, choices: dict[str, str | None]) -> type:
    """Build the question, leaving out anything there is no choice about.

    The deploy target is asked about only when a mod manager is in play. With
    no profiles the game folder is the only place a mod can go, and a question
    with one real answer trains people to click past questions.
    """
    fields: dict[str, Any] = {
        "game_is_correct": (
            bool,
            Field(
                description=(
                    f"Is {game_name} the game you want to mod? Answer no if "
                    "this is the wrong game -- nothing will be created."
                ),
            ),
        ),
        "mod_name": (
            str,
            Field(
                default=mod_name,
                description="Name of the mod. Must be a valid C# identifier.",
            ),
        ),
    }

    if len(choices) > 2:  # More than the game folder and "decide later".
        labels = tuple(choices)
        fields["deploy_target"] = (
            Literal[labels],  # type: ignore[valid-type]
            Field(
                default=DECIDE_LATER,
                description=(
                    "Where should this mod be installed for testing? Only one "
                    "profile is active per launch, so a mod deployed into the "
                    "wrong one loads nothing. Pick the profile you will "
                    "actually launch."
                ),
            ),
        )

    return create_model("NewProjectConfirmation", **fields)
