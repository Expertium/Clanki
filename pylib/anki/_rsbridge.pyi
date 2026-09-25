from collections.abc import Set as AbstractSet
from typing import Union

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
    curve_due_card_ids: AbstractSet[int],
    curve_retrievabilities: dict[int, float],
) -> bytes: ...
def stored_curve_recalls(
    ids: list[int],
    packed: bytes,
    card_ids: list[int],
    elapsed_seconds: list[int | None],
    decay_rates: tuple[float, ...],
) -> list[float] | None: ...
