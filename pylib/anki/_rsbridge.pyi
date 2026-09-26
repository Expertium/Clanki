from typing import Any, Callable, Union

class Backend:
    @classmethod
    def command(cls, service: int, method: int, data: bytes) -> bytes: ...
    def db_command(self, data: bytes) -> bytes: ...

def buildhash() -> str: ...
def open_backend(data: bytes) -> Backend: ...
def initialize_logging(log_file: Union[str, None]) -> Backend: ...
def syncserver() -> None: ...
def rwkv_stats_graph_scores_request(
    search: str,
    retrievabilities: dict[int, float | None],
    target_retentions: dict[int, float],
    intervening_reviews: dict[int, int],
    curve_due_card_ids: frozenset[int] | set[int],
    curve_retrievabilities: dict[int, float],
) -> bytes: ...
def rwkv_replay_inputs_from_rows(request: bytes) -> bytes: ...
def stored_curve_recalls(
    ids: list[int],
    packed: bytes,
    card_ids: list[int],
    elapsed_seconds: list[int | None],
    decay_rates: tuple[float, ...],
) -> list[float] | None: ...

class RwkvReviewInputRows:
    loaded_cards: int
    cards_with_supported_state: int
    disabled_config_cards: int
    deck_configs: int
    searched_cards: int
    def __init__(self, data: bytes) -> None: ...
    def __len__(self) -> int: ...
    def review_inputs(
        self,
        input_type: type,
        identity_type: type,
        stable_preset_id: Callable[[str], int],
        review_states: dict[str, int],
        *,
        batch_size_override: int | None,
        default_batch_size: int,
        min_batch_size: int,
        max_batch_size: int,
        default_target_retention: float,
    ) -> dict[int, list[tuple[int, Any]]]: ...
