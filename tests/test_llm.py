"""OpenRouter disc decisions. No network."""

from __future__ import annotations

import json

import pytest

from ripknife import llm


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _chat(action: str) -> dict[str, object]:
    return {
        "choices": [
            {"message": {"content": json.dumps({"action": action})}},
        ]
    }


def _opener(payload: dict[str, object], captured: list[object]):
    def open_req(req, timeout=None):
        captured.append(req)
        assert timeout == llm.HTTP_TIMEOUT_SEC
        return _Resp(json.dumps(payload).encode("utf-8"))

    return open_req


def test_requests_use_openrouter_cheap_model_and_env_key() -> None:
    captured: list[object] = []
    env = {llm.ENV_API_KEY: "test-openrouter-key"}
    action = llm.choose_gap_action(
        "bytes still missing",
        env=env,
        opener=_opener(_chat("accept_gap"), captured),
    )
    assert action == "accept_gap"
    assert len(captured) == 1
    req = captured[0]
    assert req.get_full_url() == llm.OPENROUTER_CHAT_URL
    assert req.get_header("Authorization") == "Bearer test-openrouter-key"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == llm.DEFAULT_MODEL
    assert body["model"] == "deepseek/deepseek-v4.1-flash"
    assert body["reasoning"] == {"effort": "none"}
    assert "test-openrouter-key" not in req.data.decode("utf-8")


def test_all_three_calls_use_the_same_model() -> None:
    env = {llm.ENV_API_KEY: "test-openrouter-key"}
    calls = (
        (llm.classify_disc_failure, "bad_sectors", "read error at 9GB"),
        (llm.choose_gap_action, "another_drive", "budget spent"),
        (llm.choose_verify_action, "keep", "duration miss"),
    )
    for fn, action, text in calls:
        captured: list[object] = []
        assert fn(text, env=env, opener=_opener(_chat(action), captured)) == action
        body = json.loads(captured[0].data.decode("utf-8"))
        assert body["model"] == "deepseek/deepseek-v4.1-flash"
        enum = body["response_format"]["json_schema"]["schema"]["properties"]["action"]["enum"]
        assert action in enum


def test_per_call_model_beats_the_global_override() -> None:
    captured: list[object] = []
    env = {
        llm.ENV_API_KEY: "test-openrouter-key",
        llm.ENV_MODEL: "test/global",
        llm.ENV_GAP_MODEL: "test/gap-model",
    }
    action = llm.choose_gap_action(
        "bytes still missing",
        env=env,
        opener=_opener(_chat("accept_gap"), captured),
    )
    assert action == "accept_gap"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["model"] == "test/gap-model"


def test_global_override_applies_when_the_call_model_is_unset() -> None:
    captured: list[object] = []
    env = {
        llm.ENV_API_KEY: "test-openrouter-key",
        llm.ENV_MODEL: "test/global",
    }
    action = llm.classify_disc_failure(
        "read error",
        env=env,
        opener=_opener(_chat("tooling"), captured),
    )
    assert action == "tooling"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["model"] == "test/global"


def test_action_outside_the_list_stops() -> None:
    captured: list[object] = []
    action = llm.choose_verify_action(
        "verify failed",
        env={llm.ENV_API_KEY: "test-openrouter-key"},
        opener=_opener(_chat("copy_to_nas"), captured),
    )
    assert action == llm.STOP
    assert captured


def test_missing_key_stops_without_a_request() -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("missing key must not call OpenRouter or start a rip")

    assert llm.classify_disc_failure("L-EC uncorrectable", env={}, opener=boom) == llm.STOP
    assert llm.choose_gap_action("hole remains", env={}, opener=boom) == llm.STOP
    assert llm.choose_verify_action("gap", env={"OPENROUTER_API_KEY": "  "}, opener=boom) == llm.STOP


def test_placeholder_label_sends_jacket_and_a_real_title_does_not() -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    captured: list[object] = []
    env = {llm.ENV_API_KEY: "test-openrouter-key"}
    opener = _opener(_chat("wrong_title"), captured)
    assert (
        llm.classify_disc_failure(
            "generic label",
            volume_label="UNKOWN",
            jacket=jpeg,
            env=env,
            opener=opener,
        )
        == "wrong_title"
    )
    content = json.loads(captured[0].data.decode("utf-8"))["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    captured.clear()
    assert (
        llm.classify_disc_failure(
            "named disc",
            volume_label="Example Movie",
            jacket=jpeg,
            env=env,
            opener=opener,
        )
        == "wrong_title"
    )
    content = json.loads(captured[0].data.decode("utf-8"))["messages"][0]["content"]
    assert isinstance(content, str)
    assert "image_url" not in content


@pytest.mark.parametrize("label", ["UNKNOWN", "UNKOWN", ""])
def test_placeholder_labels_need_jacket(label: str) -> None:
    assert llm.label_needs_jacket(label)


def test_real_title_does_not_need_jacket() -> None:
    assert not llm.label_needs_jacket("Example Movie")
