"""Asking the user before a project is created.

The failure being tested for is not a crash. It is a project that builds,
deploys and runs perfectly -- for a game the user never named and a profile
they never picked, because an agent supplied both from whatever project it
happened to see last. So most of these assert on what was NOT written.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modwright import server
from modwright.confirmation import DECIDE_LATER, GAME_FOLDER
from modwright.project_config import ProjectConfig


class FakeClient:
    """A client that declares elicitation and answers the question.

    `answer` is either the raw answer dict or a callable taking the rendered
    JSON schema, so a test can pick an option out of the generated enum
    without restating how its labels are built.
    """

    def __init__(
        self, answer=None, *, action="accept", elicitation=True, form=True, url=False
    ):
        self._answer = answer
        self._action = action
        self._elicitation = elicitation
        self._form = form
        self._url = url
        self.asked = []

    @property
    def client_capabilities(self):
        if not self._elicitation:
            return SimpleNamespace(elicitation=None)
        return SimpleNamespace(
            elicitation=SimpleNamespace(
                form=SimpleNamespace() if self._form else None,
                url=SimpleNamespace() if self._url else None,
            )
        )

    async def elicit(self, message, schema):
        from mcp.server.elicitation import render_elicitation_schema

        rendered = render_elicitation_schema(schema)
        self.asked.append(SimpleNamespace(message=message, schema=rendered))
        if self._action != "accept":
            return SimpleNamespace(action=self._action, data=None)
        answer = self._answer(rendered) if callable(self._answer) else self._answer
        return SimpleNamespace(
            action="accept", data=schema.model_validate(answer or {})
        )


@pytest.fixture()
def scaffold(fake_game, tmp_path, monkeypatch):
    """Scaffold through the tool, with profile discovery under test control."""

    def _scaffold(client=None, *, profiles=(), mod_name="MyMod", game="Game"):
        monkeypatch.setattr(
            "modwright.profiles.manager_data_dirs",
            lambda: [tmp_path / "r2modmanPlus-local"] if profiles else [],
        )
        project = tmp_path / "proj"
        result = asyncio.run(
            server.scaffold_mod_project(
                str(fake_game(game)), str(project), mod_name, client
            )
        )
        return result, project

    return _scaffold


def _accept(**overrides):
    return {"game_is_correct": True, **overrides}


class TestAskingBeforeAnythingIsWritten:
    def test_the_question_names_the_detected_game_and_the_project(self, scaffold):
        """The user cannot confirm a game they were never shown."""
        client = FakeClient(_accept())
        _, project = scaffold(client)

        asked = client.asked[0]
        assert "Game" in asked.message
        assert str(project) in asked.message
        assert "Game" in asked.schema["properties"]["game_is_correct"]["description"]

    def test_the_wrong_game_writes_nothing(self, scaffold):
        result, project = scaffold(FakeClient({"game_is_correct": False}))

        assert result["success"] is False
        assert result["code"] == "not_confirmed"
        assert not project.exists()

    def test_declining_writes_nothing(self, scaffold):
        result, project = scaffold(FakeClient(action="decline"))

        assert result["code"] == "not_confirmed"
        assert not project.exists()

    def test_cancelling_writes_nothing(self, scaffold):
        result, project = scaffold(FakeClient(action="cancel"))

        assert result["code"] == "not_confirmed"
        assert not project.exists()

    def test_the_users_name_beats_the_agents(self, scaffold):
        """The argument is a proposal. The answer is the decision."""
        result, project = scaffold(
            FakeClient(_accept(mod_name="WhatIActuallyWanted")), mod_name="AgentGuess"
        )

        assert result["mod_name"] == "WhatIActuallyWanted"
        assert (project / "WhatIActuallyWanted.csproj").exists()
        assert ProjectConfig.load(project).mod_name == "WhatIActuallyWanted"

    def test_an_accepted_project_is_marked_confirmed(self, scaffold):
        result, _ = scaffold(FakeClient(_accept()))

        assert result["success"] is True
        assert result["confirmed"] is True


class TestAClientThatCannotBeAsked:
    """Elicitation is optional in the spec. Refusing to scaffold without it
    would make ModWright unusable on those clients, so it proceeds -- but it
    must never let silence read as approval."""

    def test_no_elicitation_support_still_scaffolds(self, scaffold):
        result, project = scaffold(FakeClient(elicitation=False))

        assert result["success"] is True
        assert (project / "MyMod.csproj").exists()

    def test_but_says_the_details_were_never_confirmed(self, scaffold):
        result, _ = scaffold(FakeClient(elicitation=False))

        assert result["confirmed"] is False
        assert any("unverified" in hint for hint in result["hints"])

    def test_no_client_at_all_is_the_same_as_no_support(self, scaffold):
        result, _ = scaffold(None)

        assert result["success"] is True
        assert result["confirmed"] is False

    def test_url_only_elicitation_cannot_render_a_form(self, scaffold):
        """Two separate capabilities. Asking a URL-only client for a form
        would fail the call rather than produce an answer."""
        client = FakeClient(_accept(), form=False, url=True)
        result, _ = scaffold(client)

        assert client.asked == []
        assert result["confirmed"] is False

    def test_declaring_neither_kind_is_taken_at_its_word(self, scaffold):
        """The older shape of the declaration, and still in use."""
        client = FakeClient(_accept(), form=False, url=False)
        result, _ = scaffold(client)

        assert result["confirmed"] is True


class TestTheDeployTargetIsPartOfTheQuestion:
    """Where a mod installs is the other thing an agent cannot answer. It is
    asked here because this is the moment the user is already deciding things
    about the project -- not so that scaffolding can decide it for them."""

    def test_profiles_are_offered_by_name(self, scaffold, fake_profile):
        fake_profile("dev", game_folder="Game")
        fake_profile("busy", game_folder="Game")
        client = FakeClient(_accept())
        scaffold(client, profiles=True)

        offered = client.asked[0].schema["properties"]["deploy_target"]["enum"]
        assert any(label.startswith("dev ") for label in offered)
        assert any(label.startswith("busy ") for label in offered)
        assert GAME_FOLDER in offered
        assert DECIDE_LATER in offered

    def test_choosing_a_profile_sets_the_target(
        self, scaffold, fake_profile, tmp_path
    ):
        profile = fake_profile("dev", game_folder="Game")
        result, project = scaffold(
            FakeClient(
                lambda schema: _accept(
                    deploy_target=next(
                        label
                        for label in schema["properties"]["deploy_target"]["enum"]
                        if label.startswith("dev ")
                    )
                )
            ),
            profiles=True,
        )

        assert result["deploy_root"] == str(profile)
        assert ProjectConfig.load(project).deploy_root == str(profile)
        assert "deploy_target_required" not in result

    def test_deciding_later_leaves_the_existing_refusal_in_place(
        self, scaffold, fake_profile
    ):
        """The default answer must not be a silent choice. An unanswered
        target stays unset, which is the state `deploy_mod` refuses to guess
        past -- the guard this question is meant to support, not replace."""
        fake_profile("dev", game_folder="Game")
        result, project = scaffold(
            FakeClient(_accept(deploy_target=DECIDE_LATER)), profiles=True
        )

        assert result["deploy_root"] is None
        assert result["deploy_target_required"] is True
        assert ProjectConfig.load(project).deploy_root is None

    def test_the_default_is_deciding_later(self, scaffold, fake_profile):
        fake_profile("dev", game_folder="Game")
        client = FakeClient(_accept())
        result, _ = scaffold(client, profiles=True)

        target = client.asked[0].schema["properties"]["deploy_target"]
        assert target["default"] == DECIDE_LATER
        assert result["deploy_root"] is None

    def test_the_game_folder_can_be_chosen_explicitly(
        self, scaffold, fake_profile, tmp_path
    ):
        """Distinct from deciding later, even though both end up in the game
        folder: chosen, it stops `deploy_mod` asking again."""
        fake_profile("dev", game_folder="Game")
        result, _ = scaffold(
            FakeClient(_accept(deploy_target=GAME_FOLDER)), profiles=True
        )

        assert result["deploy_root"] == str(tmp_path / "Game")
        assert "deploy_target_required" not in result

    def test_no_profiles_means_no_deploy_question(self, scaffold):
        """With no manager installed the game folder is the only place a mod
        can go, and a question with one answer teaches people to skip
        questions."""
        client = FakeClient(_accept())
        scaffold(client)

        assert "deploy_target" not in client.asked[0].schema["properties"]

    def test_a_profile_with_no_loader_is_not_offered(self, scaffold, fake_profile):
        """Offering one would be offering a target that cannot be deployed
        into. It is reported by `list_mod_profiles` with what it needs, which
        is the right place for it -- this list is a set of usable answers."""
        fake_profile("dev", game_folder="Game")
        empty = fake_profile("fresh", game_folder="Game", core=False, plugins=False)
        (empty / "BepInEx").rmdir()

        client = FakeClient(_accept())
        scaffold(client, profiles=True)

        offered = client.asked[0].schema["properties"]["deploy_target"]["enum"]
        assert not any(label.startswith("fresh ") for label in offered)

    def test_a_target_that_breaks_before_the_answer_writes_nothing(
        self, scaffold, fake_profile
    ):
        """Between the question and the answer the user can delete the very
        profile they were choosing from. Validating before scaffolding means
        that fails with nothing on disk, rather than leaving a project
        pointing somewhere that no longer exists."""
        import shutil

        profile = fake_profile("dev", game_folder="Game")

        def answer(schema):
            label = next(
                label
                for label in schema["properties"]["deploy_target"]["enum"]
                if label.startswith("dev ")
            )
            shutil.rmtree(profile)
            return _accept(deploy_target=label)

        result, project = scaffold(FakeClient(answer), profiles=True)

        assert result["code"] == "invalid_deploy_root"
        assert not project.exists()
