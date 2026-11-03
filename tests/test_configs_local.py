"""configs/night_local.yaml: a night on an open model behind a local OpenAI-compatible server,
and configs/night_local_memory.yaml, the same night with the memory section turned on."""

from pathlib import Path

from auction_night.contracts import load_config

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
CONFIG = CONFIGS / "night_local.yaml"
MEMORY = CONFIGS / "night_local_memory.yaml"


def test_one_local_model_takes_all_six_model_seats() -> None:
    config = load_config(CONFIG)
    seats = [seat for seat in config.seats if seat.kind == "llm"]
    assert len(seats) == 6 and {seat.endpoint for seat in seats} == {"local"}
    assert {seat.model for seat in seats} == {"qwen3-4b"}
    assert all(seat.thinking is not None and seat.thinking.type == "disabled" for seat in seats)


def test_the_server_and_the_seed_come_from_the_environment_and_nothing_is_priced() -> None:
    config = load_config(CONFIG)
    assert config.night.seed is None and config.night.seed_env == "AUCTION_NIGHT_SEED_LOCAL"
    endpoint = config.endpoints["local"]
    assert endpoint.kind == "openai_compatible"
    assert endpoint.base_url_env == "LOCAL_LLM_BASE_URL" and endpoint.api_key_env is None
    assert config.limits.max_cost_per_night_usd is None and config.limits.prices_file is None


def test_the_show_needs_no_second_model() -> None:
    config = load_config(CONFIG)
    assert config.show.host_offline is not None and config.show.host_offline.kind == "mock"
    assert [judge.kind for judge in config.show.judges] == ["mock", "mock"]
    assert config.show.content_scan.classifier is None


def test_the_memory_night_is_the_local_night_with_that_one_setting_turned_on() -> None:
    plain, memory = load_config(CONFIG), load_config(MEMORY)
    assert plain.night.memory == "off" and memory.night.memory == "plan_pledges"
    only_memory = memory.night.model_copy(update={"memory": "off"})
    assert only_memory == plain.night
    assert memory.model_copy(update={"night": only_memory}) == plain
    assert memory.night.seed_env == "AUCTION_NIGHT_SEED_LOCAL"
